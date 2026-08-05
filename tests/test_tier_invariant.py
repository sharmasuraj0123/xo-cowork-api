"""The tier invariant — what may live in a project's ``.xo/``, and what may not.

Commit ``7b5044f`` split per-project state into two tiers:

* **SYNCED** — ``<project>/.xo/``. Travels with the project: committed, tarred,
  restored onto another machine. Only the coordination contract belongs here.
* **RUNTIME** — ``~/.xo/<pid>/``, keyed by ``project.json:pid``, living
  physically **outside** every project tree. Machine-local telemetry: activity,
  stats, timeline, the session index.

The whole point of moving runtime out of the project tree is that share-safety
becomes a filesystem invariant instead of a ``.gitignore`` policy
(``services/cowork_agent/project_layout.py:26-27``). That invariant is only worth
anything if it is *checked*: a single caller hand-building
``<project>/.xo/sessions`` puts machine-local state back inside the shareable
tree, and nothing about the system's behaviour looks wrong until someone commits
it.

These three tests are the check, and they only mean something together:

1. :func:`test_project_xo_holds_only_the_synced_contract` — nothing extra in the
   synced tier. Passes trivially if the watcher writes nothing at all.
2. :func:`test_runtime_artefacts_land_in_the_runtime_tier` — the positive half.
   The artefacts really were produced, and they really did land under
   ``~/.xo/<pid>/``. This is what stops (1) from being vacuous.
3. :func:`test_workspace_aggregation_lives_in_the_runtime_home` — the same split
   one level up, for the cross-project workspace tier.

All three run against one real ``Watcher.tick()`` driven through the
``fakes.make_watcher`` seam, with every event type the sinks understand, so the
assertions cover the full fan-out (``visualizer/watcher.py:95-197``) rather than
a hand-picked sink.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fakes import (
    FakeSource,
    file_touched,
    make_watcher,
    message_observed,
    presence_row,
    session_first_seen,
    task_created,
    task_status_changed,
    tool_use_observed,
    usage_observed,
)

if TYPE_CHECKING:  # annotations only — `from __future__ import annotations` above
    # keeps these unevaluated at runtime, so conftest is never imported a second
    # time under its own module name (which would re-run its Layer 1 setup).
    from conftest import ScaffoldedProject, XoRoots

# The synced contract, in full. A project's ``.xo/`` may hold these four files
# and nothing else — no subdirectories at all.
#
# ``agent.json`` is deliberately IN the set: three adapters write it as a
# project-level record (``adapters/claude_code/agents.py:39``,
# ``adapters/codex/agents.py:39``, ``adapters/antigravity/agents.py:38``, all via
# ``project_layout.xo_dir``), and it is meant to travel with the project.
SYNCED_ALLOWED = {"project.json", "peers.json", "todos.json", "agent.json"}

# ``~/xo-projects/.xo/`` is NOT the workspace tier any more — that moved to
# ``~/.xo/workspace/`` (``project_layout.workspace_runtime_dir``). One file still
# legitimately lives at the old address: the workspace agent manifest, written
# and read by ``services/xo_manifest.py:79`` (``_xo_path``). It is user-facing
# configuration, not telemetry, so it was not part of the runtime move.
PROJECTS_ROOT_XO_ALLOWED = {"xo.json"}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _entries(root: Path) -> set[str]:
    """Every path under ``root``, relative and POSIX, directories marked ``/``.

    ``rglob``, not ``iterdir``: the artefact that moved out of the synced tier is
    a *subdirectory* (``sessions/``), and a shallow listing would report a
    re-appeared ``sessions/sessionslist.json`` as the single innocuous-looking
    entry ``sessions``. Relative POSIX paths keep a failure message readable —
    it names ``sessions/sessionslist.json``, not an absolute tmp_path.

    Directories carry a trailing ``/`` so that an *empty* stray directory is
    still reported (and can never accidentally match an allowed file name).
    """
    if not root.exists():
        return set()
    out: set[str] = set()
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        out.add(f"{rel}/" if path.is_dir() else rel)
    return out


def _every_event_type(project_id: str, session_id: str) -> list:
    """One of each event the sinks dispatch on, for a single session.

    Covers all five per-project sinks: ``project_json`` (any event makes the
    project appear in the tick's grouping), ``sessions_augment`` (messages,
    usage, tools), ``todos`` (task family), ``stats`` (everything), ``timeline``
    (session start, file touches, task family).
    """
    common = {"project_id": project_id, "session_id": session_id}
    return [
        session_first_seen(**common),
        message_observed(role="user", **common),
        message_observed(role="assistant", model="fake-model-1", **common),
        usage_observed(**common),
        tool_use_observed(tool="Edit", **common),
        file_touched(relative_path="src/app.py", created=True, **common),
        file_touched(relative_path="src/app.py", created=False, **common),
        task_created(task_id="1", content="Wire up the thing", **common),
        task_status_changed(task_id="1", status="completed", **common),
    ]


@dataclass(frozen=True)
class TickedWorkspace:
    """Two projects, one full tick, both tier roots pre-resolved."""

    roots: XoRoots
    projects: tuple[ScaffoldedProject, ...]


@pytest.fixture
def ticked_workspace(xo_roots: XoRoots, scaffolded_project) -> TickedWorkspace:
    """Scaffold two projects, drive one real tick, hand back the roots.

    Two projects rather than one: several sinks and the whole workspace tier
    iterate ``list_project_ids()``, and a cross-project leak (project A's
    telemetry landing in project B's tree) only shows up with more than one.

    Before the tick this also writes a ``sessionslist.json`` the way an adapter
    does — through ``project_layout.sessions_dir(name)``, the chokepoint. That is
    the single most important payload to place: it is the file the tier split
    moved, it is what ``sessions_augment`` reads to map native session ids to
    composite keys, and it is what the workspace ``sessionslist`` sink
    aggregates. Writing it through the chokepoint means test 1 fails loudly if
    the chokepoint ever starts resolving back into the project tree.
    """
    from services.cowork_agent import project_layout

    session_id = "sess-0001"
    projects = tuple(scaffolded_project(name) for name in ("demo", "beta"))

    events: list = []
    presence: list[dict] = []
    for proj in projects:
        sessions = project_layout.sessions_dir(proj.name)
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "sessionslist.json").write_text(
            json.dumps(
                {
                    f"{proj.name}__{session_id}": {
                        "sessionId": session_id,
                        "directory": str(proj.dir),
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        events.extend(_every_event_type(proj.name, session_id))
        presence.append(presence_row(session_id=session_id, project_id=proj.name))

    watcher = make_watcher(FakeSource(events=events, presence=presence))
    watcher.tick()

    return TickedWorkspace(roots=xo_roots, projects=projects)


# ── The invariant ────────────────────────────────────────────────────────────


def test_project_xo_holds_only_the_synced_contract(
    ticked_workspace: TickedWorkspace,
) -> None:
    """A project's ``.xo/`` never holds anything outside :data:`SYNCED_ALLOWED`.

    Pins the SYNCED half of the tier split (``project_layout.py:9-16``). The
    regression this exists to catch is a caller hand-building
    ``<project>/.xo/sessions`` instead of going through
    ``project_layout.sessions_dir`` — which both re-pollutes the shareable tree
    and reads a location nothing writes any more.

    Recursive on purpose: a stray ``sessions/`` shows up here as
    ``sessions/`` + ``sessions/sessionslist.json``, not as one bare entry.
    """
    for proj in ticked_workspace.projects:
        found = _entries(proj.xo)
        extra = found - SYNCED_ALLOWED
        assert found <= SYNCED_ALLOWED, (
            f"{proj.name}/.xo/ holds artefacts outside the synced contract: "
            f"{sorted(extra)}.\n"
            "Everything in <project>/.xo/ is committed and shipped to other "
            "machines. Machine-local telemetry belongs in the runtime tier — "
            "resolve it through project_layout.sessions_dir() / "
            "project_runtime_dir(), never by joining '.xo' by hand."
        )
        # The synced contract is also not allowed to *vanish*: the scaffolder
        # ships these three, and a sink that rewrote the directory instead of
        # merging into it would leave a strict subset behind.
        assert {"project.json", "peers.json", "todos.json"} <= found


def test_runtime_artefacts_land_in_the_runtime_tier(
    ticked_workspace: TickedWorkspace,
) -> None:
    """Every per-project runtime artefact is written under ``~/.xo/<pid>/``.

    The positive half of the invariant, and the reason
    :func:`test_project_xo_holds_only_the_synced_contract` is not vacuous: it
    proves the tick really produced telemetry, so an empty ``.xo/`` there means
    "correctly relocated", not "nothing ran".

    Pins ``watcher.py:120-179``, where the per-project sinks are handed
    ``project_runtime_dir(pid)`` while only ``todos`` and the identity fill get
    the synced ``xo_dir(pid)``.
    """
    runtime_root = ticked_workspace.roots.runtime

    for proj in ticked_workspace.projects:
        found = _entries(proj.runtime)
        expected = {
            "activity.json",              # sinks/activity.py:110
            "stats.json",                 # sinks/stats.py:322
            "timeline.jsonl",             # sinks/timeline.py (append_jsonl)
            "sessions/",                  # the directory that moved out
            "sessions/sessionslist.json",  # adapter-written, via sessions_dir()
            "sessions/sessions-augment.json",  # sinks/sessions_augment.py:200
        }
        assert expected <= found, (
            f"runtime tier for {proj.name} is missing "
            f"{sorted(expected - found)} under {proj.runtime}; found "
            f"{sorted(found)}"
        )

        # Keyed by pid, and physically outside the project tree — the two
        # properties that make share-safety a filesystem fact.
        assert proj.runtime.parent == runtime_root
        assert proj.runtime.name == proj.pid
        with pytest.raises(ValueError):
            proj.runtime.relative_to(ticked_workspace.roots.projects)

    # No cross-project bleed: each project's telemetry is in its own pid dir.
    pids = {proj.runtime.name for proj in ticked_workspace.projects}
    assert len(pids) == len(ticked_workspace.projects)

    # And the activity sink actually had something to say — otherwise
    # "activity.json exists" would be satisfied by an empty snapshot regardless
    # of which tier it was written to.
    activity = json.loads(
        (ticked_workspace.projects[0].runtime / "activity.json").read_text(
            encoding="utf-8"
        )
    )
    assert [s["session_id"] for s in activity["open_sessions"]] == ["sess-0001"]


def test_workspace_aggregation_lives_in_the_runtime_home(
    ticked_workspace: TickedWorkspace,
) -> None:
    """Cross-project aggregation is under ``~/.xo/workspace/``, not in the tree.

    The workspace tier used to be ``~/xo-projects/.xo/`` — i.e. inside the
    directory users share and back up. ``project_layout.workspace_runtime_dir``
    (``project_layout.py:193-199``) moved it into the runtime home.

    Asserted at **file** granularity, not directory: ``~/xo-projects/.xo/`` may
    legitimately exist and hold ``xo.json``, the workspace agent manifest owned
    by ``services/xo_manifest.py:79``. Only telemetry was supposed to move.
    """
    from services.cowork_agent.project_layout import workspace_runtime_dir

    workspace = workspace_runtime_dir()
    assert workspace.parent == ticked_workspace.roots.runtime

    found = _entries(workspace)
    expected = {
        "workspace.json",
        "stats.json",
        "activity.json",
        "timeline.jsonl",
        "sessions/sessionslist.json",
        "sessions/sessions-augment.json",
    }
    assert expected <= found, (
        f"workspace tier is missing {sorted(expected - found)} under "
        f"{workspace}; found {sorted(found)}"
    )

    stale = _entries(ticked_workspace.roots.projects / ".xo") - PROJECTS_ROOT_XO_ALLOWED
    assert not stale, (
        "workspace-tier telemetry reappeared inside the projects root at "
        f"~/xo-projects/.xo/: {sorted(stale)}. That directory is inside every "
        "user's shared/backed-up tree; aggregation belongs in "
        "project_layout.workspace_runtime_dir() (~/.xo/workspace/). Only "
        f"{sorted(PROJECTS_ROOT_XO_ALLOWED)} may live there."
    )
