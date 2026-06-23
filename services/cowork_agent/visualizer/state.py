"""Watcher-owned state directory.

Adapter sources that need to persist their own cursors (e.g. a
SQLite-polling source tracking the last-seen row id per session)
should put their files under :func:`watcher_state_dir`, not construct
the path themselves. This keeps the watcher's filesystem layout an
implementation detail the adapter doesn't need to know.

The dir is also where ``ingest.jsonl_tail.OffsetStore`` keeps its
file offsets (``offsets.json``), so adapter cursor files and the
shared offset store live side by side — one ``~/.xo-cowork/watcher/``
to clean if you ever want to fully reset watcher state.
"""

from __future__ import annotations

from pathlib import Path


def watcher_state_dir() -> Path:
    """Return the directory where watcher infrastructure (offsets,
    per-source cursors, etc.) persists its state. Created on first
    access by the callers that write into it; not pre-created here so
    a read-only deployment doesn't unnecessarily mkdir."""
    return Path.home() / ".xo-cowork" / "watcher"
