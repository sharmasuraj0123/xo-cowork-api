"""
Antigravity (agy) sessions capability.

agy tees its sessions into xo-projects (``.xo/sessions/sessionslist.json``, tagged
``backend:"antigravity"``), so the generic project-tied scan applies —
``USES_PROJECT_SESSIONS = True``. The native message store is the per-conversation
transcript ``brain/<nativeSessionId>/.system_generated/logs/transcript_full.jsonl``
(keyed by conversation uuid, not by an encoded cwd path like claude_code).

The listing hooks (``enrich_project_session`` / ``resolve_native_file`` /
``list_native_sessions``) and read hooks (``owns_session`` / ``get_messages`` /
``set_session_directory``) are what ``engine/sessions_io`` calls instead of
branching on the backend name.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.adapters.antigravity import transcript as _t
from services.cowork_agent.adapters.antigravity.paths import transcript_path
from services.cowork_agent.engine.sessions_io import find_session_file, _resolve_index_path
from services.cowork_agent.project_layout import xo_projects_root

USES_PROJECT_SESSIONS = True


# ── Listing hooks ─────────────────────────────────────────────────────────────


def resolve_native_file(meta: dict, session_id: str) -> Path | None:
    """Locate the agy transcript for a project-tied session (by conversation uuid)."""
    native = (meta or {}).get("nativeSessionId") or ""
    if not native:
        return None
    path = transcript_path(native)
    return path if path.exists() else None


def enrich_project_session(meta: dict, key: str, default_agent: str):
    """Return ``(time_created, title, effective_agent)`` by reading the transcript.

    ``time_created`` = first step's ``created_at``; ``title`` = first user prompt
    (truncated). Either override may be None (caller keeps its defaults)."""
    time_created = None
    title = None
    native = (meta or {}).get("nativeSessionId") or ""
    if native:
        steps = _t.read_steps(native)
        if steps:
            time_created = _t.created_at_iso(steps[0])
            for turn in _t.iter_turns(steps):
                if turn["role"] == "user" and turn["text"]:
                    title = turn["text"][:80]
                    break
    return time_created, title, default_agent


def list_native_sessions() -> list[dict]:
    """agy has no non-project native session store — all sessions are project-tee'd."""
    return []


# ── Read hooks ────────────────────────────────────────────────────────────────


def owns_session(session_id: str) -> bool:
    """agy sessions are detected via the sessionslist ``backend`` tag, not a scan."""
    return False


def _convert(session_id: str, path: Path) -> list[dict]:
    """Convert an agy ``transcript_full.jsonl`` into xo-cowork MessageResponse
    dicts (same shape ``engine/messages.convert_messages`` produces)."""
    steps: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    steps.append(obj)
    except OSError:
        return []

    messages: list[dict] = []
    counter = 0
    for turn in _t.iter_turns(steps):
        counter += 1
        mid = f"{session_id}_m{counter}"
        ts_ms = turn.get("ts_ms")
        ts = (
            datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
            if ts_ms else datetime.now(timezone.utc).isoformat()
        )
        if turn["role"] == "user":
            messages.append({
                "id": mid, "session_id": session_id, "time_created": ts,
                "data": {"role": "user"},
                "parts": [{
                    "id": f"{mid}_p0", "message_id": mid, "session_id": session_id,
                    "time_created": ts, "data": {"type": "text", "text": turn["text"]},
                }],
            })
        else:
            parts = []
            if turn["text"]:
                parts.append({
                    "id": f"{mid}_p{len(parts)}", "message_id": mid,
                    "session_id": session_id, "time_created": ts,
                    "data": {"type": "text", "text": turn["text"]},
                })
            for tool_name in turn.get("tool_names", []):
                parts.append({
                    "id": f"{mid}_p{len(parts)}", "message_id": mid,
                    "session_id": session_id, "time_created": ts,
                    "data": {
                        "type": "tool", "tool": tool_name, "call_id": "",
                        "state": {
                            "status": "completed", "input": {}, "output": None,
                            "metadata": None, "title": tool_name,
                            "time_start": ts, "time_end": ts, "time_compacted": None,
                        },
                    },
                })
            messages.append({
                "id": mid, "session_id": session_id, "time_created": ts,
                "data": {
                    "role": "assistant", "model_id": None, "provider_id": None,
                    "cost": None, "tokens": None, "finish": "stop", "error": None,
                },
                "parts": parts,
            })
    return messages


def get_messages(session_id: str) -> list:
    """Converted messages for an agy session (empty if no transcript)."""
    path = find_session_file(session_id)
    if not path:
        return []
    return _convert(session_id, path)


# ── Directory update ──────────────────────────────────────────────────────────


def _persist_session_directory(session_id: str, directory: str) -> bool:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    def _try_index(index_path: Path) -> bool:
        if not index_path.exists():
            return False
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception:
            return False
        for meta in index_data.values():
            if not isinstance(meta, dict) or meta.get("sessionId") != session_id:
                continue
            history = meta.get("directoryHistory") or []
            history.append({"directory": directory, "selectedAt": now_ms})
            meta["directoryHistory"] = history[-200:]
            meta["directory"] = directory
            meta["updatedAt"] = now_ms
            index_path.write_text(
                json.dumps(index_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return True
        return False

    projects_root = xo_projects_root()
    if projects_root.exists():
        for agent_dir in projects_root.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            idx_path = _resolve_index_path(agent_dir / ".xo" / "sessions")
            if idx_path and _try_index(idx_path):
                return True
    return False


def set_session_directory(session_id: str, directory: str) -> dict | None:
    if _persist_session_directory(session_id, directory):
        return {"ok": True, "session_id": session_id, "directory": directory}
    return None


__all__ = [
    "USES_PROJECT_SESSIONS", "resolve_native_file", "enrich_project_session",
    "list_native_sessions", "owns_session", "get_messages", "set_session_directory",
]
