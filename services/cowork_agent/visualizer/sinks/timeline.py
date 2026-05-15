"""``timeline.jsonl`` sink — append-only event log with rotation.

Translates the watcher's internal event taxonomy into the timeline
schema's vocabulary (docs/watcher-design.md §3.8):

* :class:`SessionFirstSeen`  → ``session.started``
* :class:`TaskCreated`       → ``todo.added``
* :class:`TaskStatusChanged` (``completed``) → ``todo.completed``
* :class:`FileTouched` (created) → ``file.created``
* :class:`FileTouched` (not created) → ``file.edited``

Other internal events (``MessageObserved``, ``UsageObserved``,
``ToolUseObserved``, non-``completed`` task status changes) don't
map to any timeline type and are silently dropped here. Those
events live on as counters in :mod:`sessions_augment` and aggregates
in :mod:`stats`.

Rotation: when ``timeline.jsonl`` exceeds 8 MB the sink renames it
to ``timeline.<UTC-iso>.jsonl`` and starts fresh. Older rotations
beyond 5 are deleted. The atomic rename means BFF readers either
see the old or the new file; never both half-written.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

from services.cowork_agent.visualizer.atomic_write import append_jsonl
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    FileTouched,
    SessionFirstSeen,
    TaskCreated,
    TaskStatusChanged,
)

logger = logging.getLogger(__name__)


_TIMELINE_FILE = Path("timeline.jsonl")
_ROTATE_BYTES = 8 * 1024 * 1024  # 8 MB
_MAX_ROTATIONS_KEEP = 5


def _emit_event(ev: Event) -> Optional[dict]:
    """Translate one internal event to the timeline-schema vocab.

    Returns ``None`` for events that don't correspond to a timeline
    type — caller skips them.
    """
    base = {
        "ts": ev.ts,
        "session_id": ev.native_session_id,
        "runtime": ev.runtime,
    }
    if isinstance(ev, SessionFirstSeen):
        return {**base, "type": "session.started"}
    if isinstance(ev, TaskCreated):
        return {
            **base,
            "type": "todo.added",
            "todo": {
                "id": ev.task_id,
                "content": ev.content,
                "status": "pending",
            },
        }
    if isinstance(ev, TaskStatusChanged):
        if ev.status == "completed":
            return {**base, "type": "todo.completed", "todo_id": ev.task_id}
        return None  # only completion is in the schema
    if isinstance(ev, FileTouched):
        return {
            **base,
            "type": "file.created" if ev.created else "file.edited",
            "path": ev.relative_path,
        }
    return None


def _rotate_if_needed(xo_dir: Path) -> None:
    path = xo_dir / _TIMELINE_FILE
    if not path.is_file():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < _ROTATE_BYTES:
        return

    # Atomic rename to a timestamped rotation.
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rotated = path.with_name(f"timeline.{stamp}.jsonl")
    try:
        path.rename(rotated)
    except OSError as exc:
        logger.warning("timeline rotate failed: %s", exc)
        return

    # Prune older rotations.
    rotations = sorted(path.parent.glob("timeline.*.jsonl"))
    for old in rotations[:-_MAX_ROTATIONS_KEEP]:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning("timeline rotation prune failed for %s: %s", old, exc)


def apply(xo_dir: Path, events: Iterable[Event]) -> list[dict]:
    """Append timeline events for this project's events.

    Returns the list of rendered lines actually appended (empty when
    no event mapped to a schema-vocab type, or when called with an
    empty input). The watcher main loop passes the returned list to
    the workspace timeline sink so the workspace ``timeline.jsonl``
    stays a multiplexed view without re-rendering.

    Rotation is checked **before** the write so a tick that pushes us
    over 8 MB starts the next tick on a fresh file.
    """
    _rotate_if_needed(xo_dir)

    lines: list[dict] = []
    for ev in events:
        rendered = _emit_event(ev)
        if rendered is not None:
            lines.append(rendered)

    if not lines:
        return []

    append_jsonl(xo_dir / _TIMELINE_FILE, lines)
    return lines
