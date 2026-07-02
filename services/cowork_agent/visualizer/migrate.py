"""One-time, idempotent migration of the ``.xo/`` runtime tier to ``~/.xo/<pid>/``.

Projects created before the runtime-tier restructure have the old *flat* layout:
machine-local telemetry (``activity.json``, ``stats.json``, ``timeline*.jsonl``,
``sync.json``, and the whole ``sessions/`` index) lived inside ``<project>/.xo/``
next to the synced contract. The restructure moves that telemetry into the home
store ``~/.xo/<pid>/`` so it is physically outside every project tree and can
never be committed or synced (see docs/xo-runtime-tier-restructure.md).

This module performs that move **once, on watcher startup**, before the tick loop
begins (so it never races a sink writing the same bytes). It is **idempotent**:
a destination that already exists wins (the stale in-tree source is removed, not
overwritten), so re-running — or running a freshly-migrated machine — is a cheap
no-op.

It names no agent — pure infrastructure.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer import registry
from services.cowork_agent.visualizer.sinks import project_json
from services.cowork_agent.visualizer.workspace_index import list_project_ids

logger = logging.getLogger(__name__)

# Files that used to live flat in ``<project>/.xo/`` and are machine-local
# runtime. ``timeline*.jsonl`` (base + rotations) is matched by glob below.
# The synced contract (project.json / peers.json / todos.json) is NOT listed —
# it stays in the project tree.
_RUNTIME_FILES = ("activity.json", "stats.json", "sync.json", "remote-head.json")


def _relocate(src: Path, dst: Path) -> None:
    """Move ``src`` → ``dst``. Destination wins (source removed if dst exists)."""
    if not src.exists():
        return
    if dst.exists():
        # Already migrated (or watcher wrote fresh runtime) — drop the stale copy.
        if src.is_dir():
            shutil.rmtree(src, ignore_errors=True)
        else:
            src.unlink(missing_ok=True)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def _migrate_one(name: str) -> bool:
    """Migrate a single project. Returns ``True`` if anything moved."""
    synced = project_layout.xo_dir(name)
    if not synced.is_dir():
        return False

    # Ensure the project has a stable pid to key the runtime store by. The
    # identity fill is idempotent and no-ops once the pid is minted.
    try:
        project_json.fill_identity(synced, name)
    except Exception:
        logger.exception("migrate: identity fill failed for %s", name)
    meta = project_layout.load_project(name)
    pid = (meta or {}).get("pid")
    if not pid:
        # No identity yet (e.g. still _template) — let the watcher mint it on a
        # later tick; the next startup migration will pick it up.
        return False
    pid = str(pid)

    runtime = project_layout.runtime_dir(pid)
    moved = False

    # Flat runtime files.
    for fname in _RUNTIME_FILES:
        src = synced / fname
        if src.exists():
            _relocate(src, runtime / fname)
            moved = True
    # Timeline base + rotations.
    for src in sorted(synced.glob("timeline*.jsonl")):
        _relocate(src, runtime / src.name)
        moved = True

    # The sessions index directory moves wholesale.
    src_sessions = synced / "sessions"
    if src_sessions.is_dir():
        dst_sessions = project_layout.runtime_sessions_dir(pid)
        if dst_sessions.exists():
            # Merge file-by-file: destination wins, stale sources dropped.
            for src in src_sessions.iterdir():
                _relocate(src, dst_sessions / src.name)
            shutil.rmtree(src_sessions, ignore_errors=True)
        else:
            _relocate(src_sessions, dst_sessions)
        moved = True

    _narrow_gitignore(name)
    return moved


def _narrow_gitignore(name: str) -> None:
    """Stop a project ``.gitignore`` from ignoring all of ``.xo/``.

    Now that runtime lives outside the tree, ``<project>/.xo/`` holds only the
    synced contract — it must be trackable so an external commit layer can carry
    it. If an existing ``.gitignore`` blanket-ignores ``.xo/`` we drop only those
    exact lines (nothing else is touched). The bundled template ships no
    ``.gitignore``, so this is a no-op for newly-scaffolded projects.
    """
    gi = project_layout.project_dir(name) / ".gitignore"
    if not gi.is_file():
        return
    try:
        lines = gi.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    blanket = {".xo", ".xo/", "/.xo", "/.xo/"}
    kept = [ln for ln in lines if ln.strip() not in blanket]
    if len(kept) != len(lines):
        try:
            gi.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            logger.info("migrate: un-ignored .xo/ synced contract in %s/.gitignore", name)
        except OSError:
            logger.exception("migrate: failed to rewrite %s/.gitignore", name)


def migrate_runtime_layout() -> int:
    """Migrate every project on disk. Returns the count that moved data.

    Safe to call on every watcher startup — idempotent and cheap once migrated.
    """
    migrated = 0
    for name in list_project_ids():
        try:
            if _migrate_one(name):
                migrated += 1
        except Exception:
            logger.exception("migrate: failed for project %s", name)
    if migrated:
        logger.info("migrate: relocated runtime tier for %d project(s)", migrated)
    try:
        registry.build_registry()
    except Exception:
        logger.exception("migrate: registry rebuild failed (non-fatal)")
    return migrated
