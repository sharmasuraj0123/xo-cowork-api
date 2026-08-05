"""``~/.xo/registry.json`` — the machine-local pid ↔ folder map.

Guards ``services/cowork_agent/visualizer/registry.py``. The registry is the
runtime tier's index: for every project that has a settled identity it records
where the folder is and where its runtime store lives. It is rebuilt from
scratch on every call (watcher tick + migration startup), so these tests are
about *what the rebuild includes* and *where it lands* — not about incremental
state.

Two properties are load-bearing beyond the obvious:

* the file must live in the RUNTIME root. A registry inside a project tree is a
  machine-local file inside a synced tree, i.e. exactly the leak the tier split
  exists to prevent.
* the "has a settled identity" predicate (``pid and not _template``) is written
  out **twice** — here and in ``project_layout.runtime_key`` — see the comment
  above :func:`test_a_template_project_is_skipped`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer import registry

# Module-level app imports are safe: pytest imports conftest.py (and its Layer 1
# environment rewrite) before it imports any test module.

_PID = "11111111-1111-4111-8111-111111111111"
_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _project(folder: str, **meta) -> Path:
    """Scaffold ``folder``, then force its ``project.json`` to exactly ``meta``.

    Writing the document wholesale (rather than patching the template) keeps
    each test's identity shape visible in one place — these tests are entirely
    about which shapes the registry accepts. The folder name and the metadata
    ``name`` are passed separately on purpose: they are *not* the same thing
    (see the restored-copy test below).
    """
    project_layout.scaffold_project(folder)
    path = project_layout.project_metadata_path(folder)
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return path


def _settled(name: str, pid: str = _PID) -> dict:
    """A project with a settled identity: a pid and no ``_template`` flag."""
    return {
        "schema": 1,
        "pid": pid,
        "name": name,
        "owner_user_id": "local",
        "created_at": "2026-01-01T00:00:00Z",
    }


def test_the_registry_lands_in_the_runtime_root_with_the_documented_shape(xo_roots):
    """registry.json is runtime tier, and one project renders one full entry.

    Pins ``registry.py:39-40`` (``_registry_path`` → ``xo_runtime_root()``) and
    the payload contract in ``registry.py:63-82``: every pid maps to a list of
    ``{folder_id, local_path}`` anchors plus the ``runtime_dir`` its telemetry
    is keyed to. Asserting the entry by equality (not key-by-key) is deliberate:
    a field silently added to the anchor is a schema change and should show up
    here.
    """
    _project("demo", **_settled("demo"))

    payload = registry.build_registry()

    path = xo_roots.runtime / "registry.json"
    assert path.is_file(), "registry.json must be written to the runtime root"
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    # Never in the synced tier — a machine-local index inside a project tree
    # would travel with a snapshot/restore and point at another machine's store.
    assert list(xo_roots.projects.rglob("registry.json")) == []

    assert payload["schema"] == registry.SCHEMA
    assert _ISO_Z.match(payload["updated_at"])
    assert payload["projects"] == {
        _PID: {
            "anchors": [
                {
                    "folder_id": "demo",
                    "local_path": str(xo_roots.projects / "demo"),
                }
            ],
            "runtime_dir": str(xo_roots.runtime / _PID),
        }
    }


def test_one_pid_in_two_folders_keeps_both_anchors(xo_roots):
    """The "same project restored twice" case must not clobber down to one.

    ``registry.py:74-76`` appends rather than replaces when a second folder
    reports a pid already seen. Rebuilding is also asserted to be stable: the
    registry is rewritten from scratch every tick, so a duplicate-anchor bug
    would show up as growth across calls.
    """
    _project("demo", **_settled("demo"))
    _project("demo-restored", **_settled("demo-restored"))

    first = registry.build_registry()
    entry = first["projects"][_PID]
    assert [a["folder_id"] for a in entry["anchors"]] == ["demo", "demo-restored"]
    assert [a["local_path"] for a in entry["anchors"]] == [
        str(xo_roots.projects / "demo"),
        str(xo_roots.projects / "demo-restored"),
    ]
    # One pid, one runtime store — both folders share the same telemetry dir.
    assert entry["runtime_dir"] == str(xo_roots.runtime / _PID)

    second = registry.build_registry()
    assert second["projects"] == first["projects"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "workspace_index.list_project_ids() keys on project.json's `name` field, "
        "not on the folder name, so a restored copy that kept the original name "
        "is invisible to discovery and never becomes a second anchor. Reported "
        "as a finding; the fix is not in this agent's file set."
    ),
)
def test_a_restored_copy_that_kept_its_original_name_still_anchors(xo_roots):
    """The realistic restore shape: same pid AND same ``name``, new folder.

    A restore drops a snapshot's ``.xo/`` into a new folder wholesale, so
    ``project.json:name`` still says ``demo`` while the folder is
    ``demo-restored``. ``workspace_index.list_project_ids()`` collects the
    *metadata* name (``workspace_index.py:31-33``), so the set collapses to
    ``{"demo"}`` and the restored folder is never visited at all — which is the
    exact case ``registry.py``'s multi-anchor branch was written for.
    """
    _project("demo", **_settled("demo"))
    _project("demo-restored", **_settled("demo"))  # metadata still says "demo"

    payload = registry.build_registry()

    assert [a["folder_id"] for a in payload["projects"][_PID]["anchors"]] == [
        "demo",
        "demo-restored",
    ]


# ── The duplicated identity predicate ────────────────────────────────────────
#
# "Settled identity" is `pid and not _template`, and it is spelled out twice
# with no shared helper: `registry.py:58` decides whether a project appears in
# the registry, `project_layout.runtime_key():224` decides where its runtime
# actually lands. If one drifts, runtime is written under one key while the
# registry advertises another — silently, because both files stay internally
# consistent. The next two tests are the only thing holding those two call
# sites together; keep them adjacent and fix them as a pair.


def test_a_template_project_is_skipped(xo_roots):
    """A pid that is still flagged ``_template`` is not an identity yet.

    The scaffolder writes a placeholder document; the watcher's identity fill
    clears ``_template`` when it settles the pid. Registering the placeholder
    would publish a pid that the runtime tier is not using (see the paired test
    below for the other half of that statement).
    """
    _project("demo", **{**_settled("demo"), "_template": True})

    payload = registry.build_registry()

    assert payload["projects"] == {}


def test_runtime_key_also_falls_back_while_a_project_is_template(xo_roots):
    """The other half of the predicate — same document, same verdict.

    ``project_layout.runtime_key`` must agree with ``registry.build_registry``
    that a ``_template`` document has no usable pid, and fall back to the folder
    name. If this passes while :func:`test_a_template_project_is_skipped` fails
    (or vice versa) the two copies of the predicate have drifted apart.
    """
    _project("demo", **{**_settled("demo"), "_template": True})

    assert project_layout.runtime_key("demo") == "demo"
    assert project_layout.project_runtime_dir("demo") == xo_roots.runtime / "demo"


def test_a_project_without_a_pid_is_skipped(xo_roots):
    """No pid means no runtime key, so there is nothing to index yet.

    ``registry.py:57-60`` skips it and leaves the mint to a later watcher tick,
    rather than inventing a key the runtime tier would not agree with.
    """
    _project("demo", schema=1, pid=None, name="demo")

    assert registry.build_registry()["projects"] == {}


def test_an_unscaffolded_directory_is_skipped(xo_roots):
    """A bare folder under the projects root is discovered but not registered.

    ``list_project_ids()`` deliberately includes directories with no ``.xo/``
    (the watcher decides scaffolding, not the lister), so ``build_registry``
    has to tolerate ``load_project() -> None`` — ``registry.py:54-56``.
    """
    _project("demo", **_settled("demo"))
    (xo_roots.projects / "just-a-folder").mkdir()
    (xo_roots.projects / "just-a-folder" / "notes.md").write_text("hi\n")

    payload = registry.build_registry()

    assert list(payload["projects"]) == [_PID]
    anchors = payload["projects"][_PID]["anchors"]
    assert [a["folder_id"] for a in anchors] == ["demo"]


def test_a_write_failure_is_non_fatal(xo_roots, monkeypatch):
    """The registry is an index, not a source of truth — a failed write is survivable.

    ``registry.py:83-86`` swallows the write error and still returns the
    payload, so a full disk cannot take down the watcher tick (or the startup
    migration) that calls it.
    """
    _project("demo", **_settled("demo"))

    def boom(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(registry, "write_json_atomic", boom)

    payload = registry.build_registry()

    assert list(payload["projects"]) == [_PID]
    assert not (xo_roots.runtime / "registry.json").exists()
