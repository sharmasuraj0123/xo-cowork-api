"""CRUD helpers over ``<project>/.xo/todos.json`` for the agent-facing
HTTP endpoints.

Coexists with :mod:`sinks.todos` (the watcher's writer) on the same
file. Both writers take :func:`flock.locked` before read-modify-write
so simultaneous calls never clobber each other (see
docs/watcher-design.md §3.7 for the same pattern).

The BFF route layer never imports this module directly — it goes
through ``services.cowork_agent.scopes.VisualizerScope`` (P3).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.reader import read_json


PROJECT_SESSION = "_project"          # default session_id when caller doesn't provide one
VALID_STATUSES = frozenset({
    "pending", "in_progress", "completed", "cancelled", "blocked",
})

# Sanitisation regex for runtime / session_id. Permissive enough for
# realistic adapter keys (e.g. ``claude:blackhole:web:abc123``,
# ``openclaw-main``, ``hermes_local``) but rejects path traversal.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_:\-\.]{1,200}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_session_entry(runtime: str) -> dict:
    return {
        "runtime": runtime,
        "source_file": None,
        "session_started_at": None,
        "todos": [],
    }


class TodosStoreError(Exception):
    """Base for all store failures. ``code`` is the BFF error code
    the route maps to ``detail.code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_safe_key(value: str, kind: str) -> None:
    if not isinstance(value, str) or not _SAFE_KEY_RE.match(value):
        raise TodosStoreError(
            "invalid_runtime" if kind == "runtime" else "invalid_session_id",
            f"{kind} must match [A-Za-z0-9_:\\-\\.] (1..200 chars).",
        )


def _validate_status(value: str) -> None:
    if value not in VALID_STATUSES:
        raise TodosStoreError(
            "invalid_status",
            f"status must be one of {sorted(VALID_STATUSES)}.",
        )


def _validate_content_length(value: str, *, field: str, limit: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TodosStoreError("invalid_value", f"{field} is required and must be non-empty.")
    if len(value) > limit:
        raise TodosStoreError("invalid_value", f"{field} exceeds {limit} chars.")


# ── CRUD ───────────────────────────────────────────────────────────────────


def create_todo(
    todos_path: Path,
    *,
    runtime: str,
    content: str,
    description: Optional[str] = None,
    active_form: Optional[str] = None,
    session_id: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    """Append a new todo. Returns the created todo dict.

    ``todo_id`` is server-generated (UUID v4 hex prefix, 8 chars) so
    callers don't have to coordinate. Collisions are extremely
    unlikely; on the off chance, we'd 500 (caller retries).
    """
    _validate_safe_key(runtime, "runtime")
    sid = session_id or PROJECT_SESSION
    _validate_safe_key(sid, "session_id")
    _validate_content_length(content, field="content", limit=1000)
    if description is not None:
        _validate_content_length(description, field="description", limit=4000)
    if active_form is not None:
        _validate_content_length(active_form, field="active_form", limit=1000)
    initial_status = status or "pending"
    _validate_status(initial_status)

    todo_id = uuid.uuid4().hex[:8]

    with locked(todos_path):
        current = read_json(todos_path) or {}
        sessions: dict = dict(current.get("sessions") or {})
        entry = sessions.get(sid)
        if entry is None or not isinstance(entry, dict):
            entry = _empty_session_entry(runtime)
            sessions[sid] = entry
        # Keep runtime in sync — useful when a session_id is later
        # reused by a different runtime, the latest wins.
        entry["runtime"] = runtime
        todos = entry.setdefault("todos", [])

        if any(t.get("id") == todo_id for t in todos):
            raise TodosStoreError(
                "scope_unavailable",
                f"todo id collision ({todo_id}); retry the call.",
            )

        new_todo = {
            "id": todo_id,
            "content": content,
            "status": initial_status,
            "description": description,
            "active_form": active_form,
        }
        todos.append(new_todo)

        write_json_atomic(todos_path, {
            "$schema": "./schema/todos.schema.json",
            "schema": 1,
            "updated_at": _now_iso(),
            "sessions": sessions,
        })
    return new_todo


def get_todo(todos_path: Path, todo_id: str) -> Optional[tuple[str, dict]]:
    """Return ``(session_id, todo_dict)`` or ``None`` if no match."""
    current = read_json(todos_path) or {}
    sessions = current.get("sessions") or {}
    if not isinstance(sessions, dict):
        return None
    for sid, entry in sessions.items():
        if not isinstance(entry, dict):
            continue
        for t in entry.get("todos") or []:
            if isinstance(t, dict) and t.get("id") == todo_id:
                return sid, t
    return None


def update_todo(
    todos_path: Path,
    todo_id: str,
    *,
    status: Optional[str] = None,
    content: Optional[str] = None,
    description: Optional[str] = None,
    active_form: Optional[str] = None,
) -> dict:
    """Update fields on an existing todo. Returns the updated dict.

    Raises ``TodosStoreError("todo_not_found", ...)`` if the id
    isn't present in any session.
    """
    if status is not None:
        _validate_status(status)
    if content is not None:
        _validate_content_length(content, field="content", limit=1000)
    if description is not None:
        _validate_content_length(description, field="description", limit=4000)
    if active_form is not None:
        _validate_content_length(active_form, field="active_form", limit=1000)

    with locked(todos_path):
        current = read_json(todos_path) or {}
        sessions: dict = dict(current.get("sessions") or {})
        found_entry: Optional[dict] = None
        found_todo: Optional[dict] = None
        for entry in sessions.values():
            if not isinstance(entry, dict):
                continue
            todos = entry.get("todos") or []
            for t in todos:
                if isinstance(t, dict) and t.get("id") == todo_id:
                    found_entry = entry
                    found_todo = t
                    break
            if found_todo is not None:
                break

        if found_todo is None:
            raise TodosStoreError("todo_not_found", "Todo not found.")

        if status is not None:
            found_todo["status"] = status
        if content is not None:
            found_todo["content"] = content
        if description is not None:
            found_todo["description"] = description
        if active_form is not None:
            found_todo["active_form"] = active_form

        write_json_atomic(todos_path, {
            "$schema": "./schema/todos.schema.json",
            "schema": 1,
            "updated_at": _now_iso(),
            "sessions": sessions,
        })
    return found_todo


def delete_todo(todos_path: Path, todo_id: str) -> bool:
    """Remove a todo by id. Returns ``True`` if it was present and
    removed, ``False`` if it wasn't present (idempotent).
    """
    with locked(todos_path):
        current = read_json(todos_path) or {}
        sessions: dict = dict(current.get("sessions") or {})
        removed = False
        for entry in sessions.values():
            if not isinstance(entry, dict):
                continue
            todos = entry.get("todos") or []
            new_todos = [t for t in todos if not (isinstance(t, dict) and t.get("id") == todo_id)]
            if len(new_todos) != len(todos):
                entry["todos"] = new_todos
                removed = True

        if not removed:
            return False

        write_json_atomic(todos_path, {
            "$schema": "./schema/todos.schema.json",
            "schema": 1,
            "updated_at": _now_iso(),
            "sessions": sessions,
        })
        return True
