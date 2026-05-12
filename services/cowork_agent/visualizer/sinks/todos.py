"""``todos.json`` sink — per-session todo list.

Mirrors the on-disk shape from docs/watcher-design.md §3.4. Keys are
**native session ids** (the runtime's per-jsonl UUID), not the
composite cowork keys — todos are emitted by the runtime so the
runtime's id is the natural index.

The sink consumes:

* :class:`events.TaskCreated` — appends a new todo (or upserts if a
  todo with the same id already exists, e.g. after a session rotate).
* :class:`events.TaskStatusChanged` — updates an existing todo's
  ``status``; ignored if no matching todo (we never observed the
  ``TaskCreate`` that should have introduced it — possible after a
  restart with stale offsets, or for sessions started outside the
  cowork-api).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    SessionFirstSeen,
    TaskCreated,
    TaskStatusChanged,
)
from services.cowork_agent.visualizer.reader import read_json


_TODOS_FILE = Path("todos.json")


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_session_entry(runtime: str, session_started_at: str | None) -> dict:
    return {
        "runtime": runtime,
        "source_file": None,
        "session_started_at": session_started_at,
        "todos": [],
    }


def apply(xo_dir: Path, events: Iterable[Event]) -> bool:
    """Apply task-family events to ``todos.json``. Returns ``True``
    if the file changed.

    Two writers coexist on this file: this sink AND the agent-facing
    ``POST/PATCH/DELETE /todos`` endpoints (see
    ``routers/cowork_agent/bff/visualizer.py``). The shared
    :func:`flock.locked` makes the read-modify-write atomic so neither
    writer clobbers the other.
    """
    events = list(events)
    if not events:
        return False

    path = xo_dir / _TODOS_FILE
    with locked(path):
        current = read_json(path) or {}
        sessions: dict = dict(current.get("sessions") or {})
        changed = False

        for ev in events:
            nsid = ev.native_session_id
            if not nsid:
                continue

            if isinstance(ev, SessionFirstSeen):
                if nsid not in sessions:
                    sessions[nsid] = _empty_session_entry(ev.runtime, ev.ts)
                    changed = True
                continue

            if not isinstance(ev, (TaskCreated, TaskStatusChanged)):
                continue

            entry = sessions.get(nsid)
            if entry is None or not isinstance(entry, dict):
                entry = _empty_session_entry(ev.runtime, None)
                sessions[nsid] = entry
                changed = True

            todos = entry.setdefault("todos", [])
            if not isinstance(todos, list):
                todos = []
                entry["todos"] = todos

            if isinstance(ev, TaskCreated):
                # Upsert by id — defensive against duplicate emissions
                # (e.g. offset replay after a rotation false-positive).
                existing = next((t for t in todos if t.get("id") == ev.task_id), None)
                if existing is None:
                    todos.append({
                        "id": ev.task_id,
                        "content": ev.content,
                        "status": "pending",
                        "description": ev.description,
                        "active_form": ev.active_form,
                    })
                    changed = True
                else:
                    refreshed = False
                    if not existing.get("content") and ev.content:
                        existing["content"] = ev.content
                        refreshed = True
                    if not existing.get("description") and ev.description:
                        existing["description"] = ev.description
                        refreshed = True
                    if not existing.get("active_form") and ev.active_form:
                        existing["active_form"] = ev.active_form
                        refreshed = True
                    changed = changed or refreshed

            elif isinstance(ev, TaskStatusChanged):
                existing = next((t for t in todos if t.get("id") == ev.task_id), None)
                if existing is None:
                    todos.append({
                        "id": ev.task_id,
                        "content": "",
                        "status": ev.status,
                        "description": None,
                        "active_form": None,
                    })
                    changed = True
                elif existing.get("status") != ev.status:
                    existing["status"] = ev.status
                    changed = True

        if not changed:
            return False

        write_json_atomic(path, {
            "schema": 1,
            "updated_at": _now_iso(),
            "sessions": sessions,
        })
        return True
