"""One-time runtime-tier migration — ``<project>/.xo/`` → ``~/.xo/<pid>/``.

Guards ``services/cowork_agent/visualizer/migrate.py``. This runs once per
watcher startup against whatever is on disk, including projects that predate
the tier split, so its two properties are *never lose bytes* and *never do
anything the second time*.

Two deliberate choices run through the file:

* **Assert markers, not existence.** Every file the fixture materialises
  carries a unique marker string and is compared byte-for-byte after the move.
  A truncating or clobbering "move" passes an ``exists()`` check happily.
* **Snapshot ``(bytes, mtime_ns)``.** "Idempotent" here means the second run is
  not merely harmless but *inert* — no rewrite-with-identical-content churn.
  Commit ``9d12543`` reverted exactly that class of needless git churn, and
  ``.xo/`` is now a tracked, synced directory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer import migrate

# Module-level app imports are safe: pytest imports conftest.py (and its Layer 1
# environment rewrite) before it imports any test module.

# The pre-restructure flat layout, split by the code path that moves it.
_FLAT_FILES = ("activity.json", "stats.json", "sync.json", "remote-head.json")
_TIMELINE_FILES = (
    "timeline.jsonl",
    "timeline.20260101T000000Z.jsonl",
    "timeline.20260102T000000Z.jsonl",
)
_SESSION_FILES = ("sessionslist.json", "sessions-augment.json")
# What must stay behind in <project>/.xo/ — the synced contract.
_SYNCED_FILES = ("project.json", "todos.json", "peers.json")


@dataclass(frozen=True)
class FlatProject:
    """A project still in the pre-restructure layout, with its expected bytes."""

    name: str
    pid: str | None
    dir: Path
    xo: Path
    runtime: Path
    # {relative-path-under-.xo: exact bytes written} for the runtime tier only.
    runtime_bytes: dict[str, bytes]


@pytest.fixture
def flat_project(xo_roots):
    """Factory: materialise a full pre-restructure ``<project>/.xo/``.

    Everything the restructure relocates lives flat in the project's ``.xo/``:
    the four runtime JSON files, ``timeline.jsonl`` plus rotations, and the
    whole ``sessions/`` index — next to the synced contract, which must survive
    untouched. Each file gets a marker naming the project and the file, so a
    later byte comparison proves the *contents* moved, not just the paths.

    ``pid=None`` leaves the template's unfilled identity in place (``_template:
    true``, ``pid: null``) for the deferred-migration branch.
    """

    def _make(
        name: str = "demo",
        *,
        pid: str | None = "22222222-2222-4222-8222-222222222222",
        gitignore: str | None = None,
    ) -> FlatProject:
        project_layout.scaffold_project(name)
        xo = project_layout.xo_dir(name)

        meta_path = project_layout.project_metadata_path(name)
        if pid is None:
            meta = {"schema": 1, "_template": True, "pid": None, "name": None}
        else:
            meta = {
                "schema": 1,
                "pid": pid,
                "name": name,
                "owner_user_id": "local",
                "created_at": "2026-01-01T00:00:00Z",
            }
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

        runtime_bytes: dict[str, bytes] = {}

        def _write(rel: str, marker: str) -> None:
            target = xo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            blob = f'{{"marker": "{name}:{marker}"}}\n'.encode("utf-8")
            target.write_bytes(blob)
            runtime_bytes[rel] = blob

        for fname in _FLAT_FILES:
            _write(fname, fname)
        for fname in _TIMELINE_FILES:
            # JSONL in production; a one-line JSON object is a valid JSONL body
            # and keeps the marker comparison uniform.
            _write(fname, fname)
        for fname in _SESSION_FILES:
            _write(f"sessions/{fname}", fname)

        # The synced contract the migration must leave alone.
        (xo / "todos.json").write_bytes(f'{{"marker": "{name}:todos"}}\n'.encode())
        (xo / "peers.json").write_bytes(f'{{"marker": "{name}:peers"}}\n'.encode())

        pdir = project_layout.project_dir(name)
        if gitignore is not None:
            (pdir / ".gitignore").write_text(gitignore, encoding="utf-8")

        # Resolve the runtime destination from the pid directly: the point of
        # most of these tests is where the data lands, so deriving it through
        # the same helper the code uses would make some of them tautological.
        runtime = (
            xo_roots.runtime / pid if pid else project_layout.project_runtime_dir(name)
        )
        return FlatProject(
            name=name,
            pid=pid,
            dir=pdir,
            xo=xo,
            runtime=runtime,
            runtime_bytes=runtime_bytes,
        )

    return _make


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """``{relative path: (bytes, mtime_ns)}`` for every file under ``root``."""
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_a_fresh_migration_moves_every_runtime_file_byte_for_byte(flat_project):
    """Nothing is lost or truncated on the way to ``~/.xo/<pid>/``.

    Pins the whole of ``migrate._migrate_one``: the four flat files
    (``migrate.py:77-82``), the timeline glob (``:83-86``) and the ``sessions/``
    directory (``:88-99``) all land in the runtime store with identical bytes,
    the source copies are gone, and ``<project>/.xo/`` is left holding exactly
    the synced contract.
    """
    proj = flat_project()

    assert migrate.migrate_runtime_layout() == 1

    for rel, blob in proj.runtime_bytes.items():
        assert (proj.runtime / rel).read_bytes() == blob, f"{rel} did not survive"
        assert not (proj.xo / rel).exists(), f"{rel} was copied, not moved"
    assert not (proj.xo / "sessions").exists()

    # The synced tier is untouched — same three files, same bytes.
    assert sorted(p.name for p in proj.xo.iterdir()) == sorted(_SYNCED_FILES)
    assert (proj.xo / "todos.json").read_bytes() == b'{"marker": "demo:todos"}\n'
    assert (proj.xo / "peers.json").read_bytes() == b'{"marker": "demo:peers"}\n'


def test_every_timeline_rotation_moves(flat_project):
    """The glob has to take the rotations too, not just ``timeline.jsonl``.

    ``migrate.py:84`` globs ``timeline*.jsonl``. A rotation left behind would
    sit in the synced tree forever: nothing reads it there any more, and no
    later pass would move it because the base file is already gone.
    """
    proj = flat_project()

    migrate.migrate_runtime_layout()

    assert sorted(p.name for p in proj.runtime.glob("timeline*.jsonl")) == sorted(
        _TIMELINE_FILES
    )
    assert list(proj.xo.glob("timeline*.jsonl")) == []


def test_a_second_run_changes_nothing_at_all(flat_project):
    """Idempotent means inert, not merely harmless.

    ``migrate_runtime_layout`` runs on every watcher startup. The second run
    must return 0 and must not rewrite a single byte or bump a single mtime —
    ``.xo/`` is tracked and synced, so a same-content rewrite is real churn.

    ``~/.xo/registry.json`` is deliberately outside the snapshot: it lives at
    the runtime *root*, not in the project store, and ``build_registry`` stamps
    a fresh ``updated_at`` on every call by design.
    """
    proj = flat_project()
    assert migrate.migrate_runtime_layout() == 1

    before = (_snapshot(proj.dir), _snapshot(proj.runtime))

    assert migrate.migrate_runtime_layout() == 0

    assert (_snapshot(proj.dir), _snapshot(proj.runtime)) == before


def test_the_destination_wins_for_a_flat_runtime_file(flat_project):
    """A stale in-tree copy never overwrites live runtime data.

    ``migrate._relocate:42-48`` drops the source when the destination already
    exists. That is the crash-recovery case: the watcher may have written fresh
    telemetry to ``~/.xo/<pid>/`` before a startup migration ran, and the file
    left in the project tree is by then older than what replaced it.
    """
    proj = flat_project()
    dest_blob = b'{"marker": "written-by-the-watcher"}\n'
    proj.runtime.mkdir(parents=True, exist_ok=True)
    (proj.runtime / "activity.json").write_bytes(dest_blob)

    migrate.migrate_runtime_layout()

    assert (proj.runtime / "activity.json").read_bytes() == dest_blob
    assert not (proj.xo / "activity.json").exists()


def test_the_sessions_merge_keeps_the_destination_and_adopts_source_only_files(
    flat_project,
):
    """``sessions/`` merges file-by-file — a different branch from the flat move.

    ``migrate.py:92-96``: when the destination directory already exists the
    directory is *not* moved wholesale (that would need the destination gone).
    Each entry is relocated individually so the destination's own copy wins
    while source-only files are still adopted, and the emptied source directory
    is removed.
    """
    proj = flat_project()
    dest_blob = b'{"marker": "sessionslist-written-by-the-adapter"}\n'
    dst_sessions = proj.runtime / "sessions"
    dst_sessions.mkdir(parents=True)
    (dst_sessions / "sessionslist.json").write_bytes(dest_blob)

    source_only = b'{"marker": "source-only"}\n'
    (proj.xo / "sessions" / "sessions-index.legacy.json").write_bytes(source_only)

    migrate.migrate_runtime_layout()

    assert (dst_sessions / "sessionslist.json").read_bytes() == dest_blob
    assert (dst_sessions / "sessions-index.legacy.json").read_bytes() == source_only
    assert (dst_sessions / "sessions-augment.json").read_bytes() == (
        proj.runtime_bytes["sessions/sessions-augment.json"]
    )
    assert not (proj.xo / "sessions").exists()


def test_narrowing_a_gitignore_drops_only_the_blanket_forms(flat_project):
    """``.xo/`` must become trackable — but nothing else may be edited.

    ``migrate._narrow_gitignore:105-128`` removes the four exact blanket spellings
    (after ``strip()``) and leaves every other line, in order. ``.xo/*`` and
    ``**/.xo/`` in particular are NOT blanket ignores of the directory itself —
    a project may legitimately ignore the contents while tracking the folder,
    and re-including a file below an unignored directory only works if the
    directory itself was never ignored.
    """
    proj = flat_project(
        gitignore=(
            "node_modules/\n"
            ".xo\n"
            ".xo/\n"
            "/.xo\n"
            "/.xo/\n"
            "   .xo/   \n"  # padded — the check strips before comparing
            ".xo/*\n"
            "**/.xo/\n"
            "!.xo/project.json\n"
            "# keep this comment\n"
        )
    )

    migrate.migrate_runtime_layout()

    assert (proj.dir / ".gitignore").read_text(encoding="utf-8") == (
        "node_modules/\n"
        ".xo/*\n"
        "**/.xo/\n"
        "!.xo/project.json\n"
        "# keep this comment\n"
    )


def test_a_gitignore_without_a_blanket_line_is_not_touched_at_all(flat_project):
    """No blanket line means no write — same bytes AND same mtime.

    ``migrate.py:123`` only rewrites when the kept-line count actually dropped.
    Rewriting an unchanged ``.gitignore`` on every startup would show up as a
    dirty working tree in every project on the machine; commit ``9d12543``
    reverted the same class of churn on ``install_shared_deps.sh``.
    """
    original = "node_modules/\n*.log\n.env\n"
    proj = flat_project(gitignore=original)
    gi = proj.dir / ".gitignore"
    before = (gi.read_bytes(), gi.stat().st_mtime_ns)

    migrate.migrate_runtime_layout()

    assert (gi.read_bytes(), gi.stat().st_mtime_ns) == before


def test_a_project_without_a_pid_defers_instead_of_guessing_a_key(
    flat_project, monkeypatch, xo_roots
):
    """No identity yet ⇒ leave the data where it is; do not key it by folder name.

    ``migrate.py:68-71`` returns early rather than falling back to the
    folder-name runtime key: relocating under a transitional key would have to
    be undone once the real pid is minted. Note ``_migrate_one`` calls
    ``fill_identity`` itself (``:63``) and would normally mint the pid on the
    spot, so this branch is only reachable with the fill neutralised — that is
    the "identity fill failed" path (``:64-65``), not a hypothetical.
    """
    proj = flat_project(pid=None)
    monkeypatch.setattr(migrate.project_json, "fill_identity", lambda *a, **k: False)

    assert migrate.migrate_runtime_layout() == 0

    # Everything stayed put, byte for byte.
    for rel, blob in proj.runtime_bytes.items():
        assert (proj.xo / rel).read_bytes() == blob
    # And no runtime store was created for it under any key.
    assert [p.name for p in xo_roots.runtime.iterdir()] == ["registry.json"]
    meta = json.loads(project_layout.project_metadata_path("demo").read_text())
    assert meta["pid"] is None and meta["_template"] is True


def test_one_broken_project_does_not_abort_the_others(flat_project, xo_roots):
    """A per-project failure is contained — ``migrate.py:138-142``.

    The migration runs once at startup across every project on the machine, so
    one unusable project must not strand the rest in the old layout. The break
    used here is real rather than injected: the destination path is occupied by
    a regular file, so the first ``mkdir`` inside ``_relocate`` raises.
    Alphabetical ordering puts the broken project first, so the assertion that
    the other one migrated is an assertion that the loop *continued*.
    """
    bad = flat_project("aaa-bad")
    good = flat_project("zzz-good", pid="33333333-3333-4333-8333-333333333333")
    bad.runtime.parent.mkdir(parents=True, exist_ok=True)
    bad.runtime.write_bytes(b"not a directory\n")

    assert migrate.migrate_runtime_layout() == 1

    for rel, blob in good.runtime_bytes.items():
        assert (good.runtime / rel).read_bytes() == blob
    # The broken one kept its data rather than half-moving it.
    for rel, blob in bad.runtime_bytes.items():
        assert (bad.xo / rel).read_bytes() == blob


def test_the_migration_rebuilds_the_registry(flat_project, xo_roots):
    """Startup migration leaves a registry consistent with what it just moved.

    ``migrate.py:145-148``. The registry is the reverse map from pid to folder
    and runtime store; rebuilding it here is what stops it describing the
    pre-migration world until the first tick lands.
    """
    proj = flat_project()

    migrate.migrate_runtime_layout()

    payload = json.loads(
        (xo_roots.runtime / "registry.json").read_text(encoding="utf-8")
    )
    assert payload["projects"][proj.pid]["runtime_dir"] == str(proj.runtime)
    assert payload["projects"][proj.pid]["anchors"] == [
        {"folder_id": "demo", "local_path": str(proj.dir)}
    ]


def test_a_registry_failure_does_not_undo_the_migration(flat_project, monkeypatch):
    """The registry is an index; the move is the real work.

    ``migrate.py:145-148`` swallows the rebuild failure and still reports the
    projects it migrated, so a bad registry write cannot make the watcher retry
    a completed move on the next startup.
    """
    proj = flat_project()

    def boom():
        raise OSError("no space left on device")

    monkeypatch.setattr(migrate.registry, "build_registry", boom)

    assert migrate.migrate_runtime_layout() == 1
    for rel, blob in proj.runtime_bytes.items():
        assert (proj.runtime / rel).read_bytes() == blob
