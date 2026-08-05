"""The BFF read path: which on-disk tier each visualizer read resolves to.

``services/cowork_agent/scopes.py`` is the single place the BFF layer turns a
frontend "noun" into a filesystem handle, and since the runtime-tier split it
reads from **two different roots** (``_XoReader``, ``scopes.py:71-88``):

* ``_xo_root``     — the SYNCED contract in the project tree, ``<project>/.xo/``
* ``_runtime_root`` — machine-local telemetry, ``~/.xo/<pid>/``

Only ``todos.json`` comes from the synced root; stats, activity, timeline and
the two sessions files come from the runtime root.

**The technique these tests are built on.** Every tier test writes a DISTINCT
sentinel into *both* roots and asserts which one comes back. An existence check
("stats.json was read") passes even when both roots happen to point at the same
directory, so it cannot catch a read wired to the wrong tier — which is the
entire bug class the split introduced. Two files with the same name and
different contents is the only assertion shape that can.

See docs/xo-runtime-tier-restructure.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.cowork_agent import project_layout
from services.cowork_agent.scopes import (
    ScopeNotFound,
    SecretsScope,
    VisualizerScope,
    WorkspaceVisualizerScope,
    resolve_scope,
)

# ── Sentinel seeding ─────────────────────────────────────────────────────────

# Two timeline lines so the newest-first contract of read_timeline is testable
# at the same time as the tier it read from.
_TS_OLD = "2026-01-01T00:00:00Z"
_TS_NEW = "2026-01-01T00:01:00Z"


def _write_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _seed_tier(root: Path, tier: str) -> str:
    """Write one sentinel copy of every BFF-readable file under ``root``.

    ``tier`` is stamped into each document so a read can be traced back to the
    directory it came from. The sessionslist composite key is tier-stamped too
    (it is the row's true identity), and is returned so callers can assert on
    identity rather than only on content.

    The adapter row and the augment row deliberately carry *different* tier
    markers (``backend`` vs ``firstActivity``): those field sets are disjoint by
    contract (``visualizer/reader.py:36-45``), so stamping the same key into
    both would make ``merge_session_record`` fail closed instead of merging.
    """
    key = f"demo__{tier}"
    _write_json(root / "stats.json", {"tier": tier})
    _write_json(root / "activity.json", {"tier": tier})
    _write_json(
        root / "todos.json",
        {"tier": tier, "todos": [{"id": "t-1", "content": f"todo from {tier}"}]},
    )
    (root / "timeline.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (root / "timeline.jsonl").write_text(
        "\n".join(
            json.dumps({"type": "session.started", "ts": ts, "tier": tier})
            for ts in (_TS_OLD, _TS_NEW)
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(
        root / "sessions" / "sessionslist.json",
        {
            key: {
                "sessionId": f"sid-{tier}",
                "nativeSessionId": f"native-{tier}",
                "directory": f"/{tier}",
                "backend": tier,
                "updatedAt": 1767225600000,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        },
    )
    _write_json(
        root / "sessions" / "sessions-augment.json",
        {
            "schema": 1,
            "updated_at": _TS_NEW,
            "sessions": {key: {"messageCount": 3, "firstActivity": f"first-{tier}"}},
        },
    )
    return key


# ── Project scope: the tier split ────────────────────────────────────────────


def test_todos_is_read_from_the_synced_project_tree(scaffolded_project):
    """``todos.json`` must come from ``<project>/.xo/``, not the runtime home.

    Pins ``_XoReader.read_todos`` (``scopes.py:105-106``), the one reader wired
    to ``_xo_root``. todos is the A2A coordination surface and is part of the
    SYNCED contract — reading it from the machine-local runtime tier would make
    a restored/synced project silently show an empty (or another machine's)
    todo list.

    Both roots hold a ``todos.json``; only the synced one may win.
    """
    proj = scaffolded_project("demo")

    # Guard the guard: if these two ever resolved to the same directory the
    # sentinel technique would be vacuous and every tier test would pass.
    assert proj.xo != proj.runtime
    assert proj.dir not in proj.runtime.parents

    _seed_tier(proj.xo, "synced")
    _seed_tier(proj.runtime, "runtime")

    todos = VisualizerScope("demo").read_todos()
    assert todos is not None
    assert todos["tier"] == "synced"
    assert todos["todos"][0]["content"] == "todo from synced"


def test_telemetry_is_read_from_the_runtime_home(scaffolded_project):
    """stats / activity / timeline / sessions must come from ``~/.xo/<pid>/``.

    Pins the five ``_runtime_root`` readers (``scopes.py:102-134``). The watcher
    stopped writing these into the project tree at 7b5044f; a reader still
    pointed at ``<project>/.xo/`` would serve stale pre-split data forever
    without ever erroring — so the decoys below sit exactly where the old
    location was.

    Also pins the newest-first ordering of ``read_timeline`` and the three-handle
    lookup in ``read_one_session`` (``scopes.py:136-162``), both against rows
    that exist in *both* tiers.
    """
    proj = scaffolded_project("demo")
    synced_key = _seed_tier(proj.xo, "synced")
    runtime_key = _seed_tier(proj.runtime, "runtime")
    scope = VisualizerScope("demo")

    assert (scope.read_stats() or {})["tier"] == "runtime"
    assert (scope.read_activity() or {})["tier"] == "runtime"

    timeline = scope.read_timeline(limit=10)
    assert [ev["tier"] for ev in timeline] == ["runtime", "runtime"]
    # Newest first — the cursor contract the /timeline endpoint paginates on.
    assert [ev["ts"] for ev in timeline] == [_TS_NEW, _TS_OLD]

    sessions = scope.read_sessionslist()
    assert set(sessions) == {runtime_key}
    assert synced_key not in sessions
    # The merge stitched the runtime adapter row to the runtime augment row —
    # proving BOTH sessions files resolved under _runtime_root, not just one.
    assert sessions[runtime_key]["backend"] == "runtime"
    assert sessions[runtime_key]["firstActivity"] == "first-runtime"

    # All three handles resolve to the runtime row, and none of them can reach
    # the identically-shaped synced decoy.
    for handle in (runtime_key, "native-runtime", "sid-runtime"):
        found = scope.read_one_session(handle)
        assert found is not None, handle
        assert found[0] == runtime_key
    assert scope.read_one_session("native-synced") is None


def test_workspace_scope_reads_only_the_workspace_runtime_dir(xo_roots):
    """``WorkspaceVisualizerScope`` points BOTH roots at ``~/.xo/workspace/``.

    Pins ``scopes.py:233-237``. The workspace tier is pure machine-local
    aggregation with no synced contract, so even ``read_todos`` — the one
    reader that uses ``_xo_root`` — must resolve into the runtime home here.

    The decoy sits at ``<projects>/.xo/``, which is where the workspace tier
    lived *before* the split (``project_layout.workspace_runtime_dir``,
    ``project_layout.py:193-199``). A half-migrated scope that moved only the
    ``_runtime_root`` would read todos from there and pass an existence check.
    """
    _seed_tier(project_layout.workspace_runtime_dir(), "workspace")
    _seed_tier(xo_roots.projects / ".xo", "legacy")
    _write_json(
        project_layout.workspace_runtime_dir() / "workspace.json",
        {"tier": "workspace", "projects": ["demo"]},
    )
    _write_json(xo_roots.projects / ".xo" / "workspace.json", {"tier": "legacy"})

    scope = WorkspaceVisualizerScope()

    assert (scope.read_todos() or {})["tier"] == "workspace"
    assert (scope.read_stats() or {})["tier"] == "workspace"
    assert (scope.read_activity() or {})["tier"] == "workspace"
    assert (scope.read_workspace() or {})["tier"] == "workspace"
    assert [ev["tier"] for ev in scope.read_timeline(limit=10)] == [
        "workspace",
        "workspace",
    ]
    assert set(scope.read_sessionslist()) == {"demo__workspace"}


# ── Containment ──────────────────────────────────────────────────────────────


def test_traversing_project_id_stays_inside_both_roots(xo_roots):
    """``VisualizerScope("../../etc")`` may not read outside the two roots.

    Pins ``scopes.py:181-188``: the project id is collapsed through
    ``normalize_agent_id`` before any path is built, so ``"../../etc"`` becomes
    the leaf ``"etc"``. Asserted behaviourally — decoys are planted one level
    *above* each root (where a surviving ``..`` would land) and the in-root
    sentinels are the only ones that may be returned.
    """
    scope = VisualizerScope("../../etc")
    assert scope.project_id == "etc"

    # Inside: where a correctly-collapsed id resolves.
    _write_json(xo_roots.projects / "etc" / ".xo" / "todos.json", {"where": "inside"})
    _write_json(xo_roots.runtime / "etc" / "stats.json", {"where": "inside"})
    # Outside: one ".." up from each root — reachable only if the id escaped.
    _write_json(xo_roots.projects.parent / "etc" / ".xo" / "todos.json", {"where": "escaped"})
    _write_json(xo_roots.runtime.parent / "etc" / "stats.json", {"where": "escaped"})

    assert (scope.read_todos() or {})["where"] == "inside"
    assert (scope.read_stats() or {})["where"] == "inside"


# ── Empty state ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("scaffolded", [True, False], ids=["scaffolded", "missing"])
def test_empty_state_returns_empties_and_never_raises(scaffolded, scaffolded_project):
    """Every reader must degrade to ``None`` / ``[]`` / ``{}``, never raise.

    Pins the contract the BFF routes depend on to serve their zero-filled
    payloads: before the watcher's first tick nothing exists under
    ``~/.xo/<pid>/`` at all, and for an unknown project neither root exists.
    ``project_exists`` (``scopes.py:190-196``) is the *only* signal routes use
    to tell 404 ``project_not_found`` from a 200 empty-state body, so a reader
    that raised on a missing file would turn a legitimate empty state into a
    500 (``routers/cowork_agent/bff/visualizer.py:78-86``).

    The two ids also pin the asymmetry between the tiers at scaffold time:
    ``scaffold_project`` materialises the whole SYNCED contract from the bundled
    template — including an *empty* ``todos.json`` — while it deliberately
    creates nothing under the runtime home (``project_layout.py:288-294``).
    """
    if scaffolded:
        scaffolded_project("demo")
    scope = VisualizerScope("demo")

    assert scope.project_exists() is scaffolded

    # Runtime tier: the directory itself does not exist yet, either way.
    assert scope.read_stats() is None
    assert scope.read_activity() is None
    assert scope.read_timeline(limit=50) == []
    assert scope.read_sessionslist() == {}
    assert scope.read_one_session("anything") is None

    # Synced tier: present-but-empty once scaffolded, absent before that.
    if scaffolded:
        assert (scope.read_todos() or {})["sessions"] == {}
    else:
        assert scope.read_todos() is None


# ── Resolver ─────────────────────────────────────────────────────────────────


def test_resolve_scope_maps_names_to_handles_and_rejects_the_rest(xo_roots):
    """``resolve_scope`` is a closed set; an unknown name raises ScopeNotFound.

    Pins ``scopes.py:250-273`` — principle P3/P4: routes never build paths, so
    this resolver is the whole attack surface for "which location does the
    frontend get to reach". A silent ``None``/fallback for an unrecognised name
    would let a typo'd scope quietly resolve to the wrong tier.
    """
    assert resolve_scope("xo-projects") == xo_roots.projects
    assert isinstance(resolve_scope("secrets"), SecretsScope)
    assert isinstance(resolve_scope("xo-projects-visualizer", "demo"), VisualizerScope)
    assert isinstance(resolve_scope("xo-workspace-visualizer"), WorkspaceVisualizerScope)

    with pytest.raises(ScopeNotFound):
        resolve_scope("xo-projects-visualiser")  # plausible typo, not a scope
    with pytest.raises(ScopeNotFound):
        resolve_scope("")
    # The project scope's required argument is part of the contract: without it
    # there is no project to clamp to, so it must fail rather than default.
    with pytest.raises(ScopeNotFound):
        resolve_scope("xo-projects-visualizer")
