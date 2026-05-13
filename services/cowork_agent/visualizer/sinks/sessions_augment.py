"""``sessions/sessions-augment.json`` sink — watcher-owned per-session
counters.

Tracks the fields the runtime adapters don't compute:

* ``messageCount`` — total user + assistant messages observed
* ``toolCallCount`` — total tool_use events (any tool)
* ``taskCount`` — ``{total, completed, in_progress, pending,
  cancelled, blocked}`` derived from ``TaskCreated`` /
  ``TaskStatusChanged``
* ``firstActivity`` / ``lastActivity`` — epoch ms of the earliest /
  latest event observed
* ``ended_at`` — currently always null (filled in by Phase 3 when
  session-close detection lands)
* ``episode_refs`` — preserved verbatim; the
  :mod:`memory_episodic` watcher (Phase 3) writes to this field
  separately

Keys match :mod:`sessionslist` — composite cowork-key when an
adapter row exists, else native session id (the BFF merge naturally
ignores unmatched augment rows).

Read-modify-write per tick. The sink reads the prior augment file,
applies the new events, writes back atomically.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    FileTouched,
    MessageObserved,
    SessionFirstSeen,
    TaskCreated,
    TaskStatusChanged,
    ToolUseObserved,
)
from services.cowork_agent.visualizer.reader import read_json


_AUGMENT_FILE = Path("sessions/sessions-augment.json")
_SESSIONSLIST_FILE = Path("sessions/sessionslist.json")


def _iso_to_ms(ts: str) -> Optional[int]:
    """Best-effort ISO-8601 → epoch ms. Returns ``None`` on parse
    failure (the field then stays absent on the row)."""
    try:
        return int(
            datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
        )
    except (ValueError, AttributeError):
        return None


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_native_to_composite_map(sessionslist: Optional[dict]) -> dict[str, str]:
    """Map every adapter row's ``nativeSessionId`` to its composite
    outer key. Used so augment rows can use the same key shape the
    BFF merge expects.
    """
    out: dict[str, str] = {}
    if not isinstance(sessionslist, dict):
        return out
    for composite, row in sessionslist.items():
        if not isinstance(row, dict):
            continue
        native = row.get("nativeSessionId")
        if isinstance(native, str) and native:
            out[native] = composite
    return out


def _empty_row() -> dict:
    return {
        "messageCount":   0,
        "toolCallCount":  0,
        "taskCount":      {"total": 0, "completed": 0, "in_progress": 0,
                           "pending": 0, "cancelled": 0, "blocked": 0},
        "firstActivity":  None,
        "lastActivity":   None,
        "ended_at":       None,
        "episode_refs":   [],
    }


def _stamp_activity(row: dict, ts: str) -> None:
    ms = _iso_to_ms(ts)
    if ms is None:
        return
    if row.get("firstActivity") is None or ms < row["firstActivity"]:
        row["firstActivity"] = ms
    if row.get("lastActivity") is None or ms > row["lastActivity"]:
        row["lastActivity"] = ms


# Status transitions we track. Anything else is ignored.
_VALID_STATUSES = frozenset({
    "pending", "in_progress", "completed", "cancelled", "blocked",
})


def apply(xo_dir: Path, events: Iterable[Event]) -> bool:
    """Apply ``events`` to this project's augment file. Returns
    ``True`` if the file changed (so the workspace tier knows to
    re-aggregate).
    """
    events = list(events)
    if not events:
        return False

    augment_path = xo_dir / _AUGMENT_FILE
    sessionslist = read_json(xo_dir / _SESSIONSLIST_FILE)
    native_to_composite = _build_native_to_composite_map(sessionslist)

    current = read_json(augment_path) or {}
    sessions: dict = dict(current.get("sessions") or {})

    # Per-task last-known status, so a TaskCreated followed by
    # TaskStatusChanged correctly transitions counts.
    # Loaded lazily per (key, task_id) from the row.
    def _task_states(row: dict) -> dict[str, str]:
        st = row.setdefault("_task_states", {})
        if not isinstance(st, dict):
            st = {}
            row["_task_states"] = st
        return st

    changed = False

    for ev in events:
        nsid = ev.native_session_id
        if not nsid:
            continue
        key = native_to_composite.get(nsid, nsid)
        row = sessions.get(key)
        if row is None or not isinstance(row, dict):
            row = _empty_row()
            sessions[key] = row
            changed = True

        # Preserve adapter-written timing if it ever bleeds in (defensive).
        _stamp_activity(row, ev.ts)

        if isinstance(ev, MessageObserved):
            row["messageCount"] = int(row.get("messageCount", 0)) + 1
            changed = True
        elif isinstance(ev, ToolUseObserved):
            row["toolCallCount"] = int(row.get("toolCallCount", 0)) + 1
            changed = True
        elif isinstance(ev, TaskCreated):
            tc = row["taskCount"]
            tc["total"] += 1
            tc["pending"] = tc.get("pending", 0) + 1
            _task_states(row)[ev.task_id] = "pending"
            changed = True
        elif isinstance(ev, TaskStatusChanged):
            if ev.status not in _VALID_STATUSES:
                continue
            states = _task_states(row)
            prev = states.get(ev.task_id)
            tc = row["taskCount"]
            if prev and prev in tc:
                tc[prev] = max(0, tc[prev] - 1)
            tc[ev.status] = tc.get(ev.status, 0) + 1
            states[ev.task_id] = ev.status
            changed = True
        elif isinstance(ev, (FileTouched, SessionFirstSeen)):
            # Updates the timestamps but no counter — already stamped above.
            pass

    if not changed:
        return False

    # ``_task_states`` is private state — the prev-status map needed
    # to decrement ``taskCount`` correctly across restarts. It IS
    # persisted (we'd lose count integrity otherwise). The BFF
    # routes never serialise it: ``reader.merge_session_record``
    # treats it as a watcher-only field, and the Pydantic models on
    # the wire pick named fields rather than spreading the dict —
    # see ``routers/cowork_agent/bff/visualizer.py::_row_to_list_item``.
    write_json_atomic(augment_path, {
        "schema": 1,
        "updated_at": _now_iso(),
        "sessions": sessions,
    })
    return True
