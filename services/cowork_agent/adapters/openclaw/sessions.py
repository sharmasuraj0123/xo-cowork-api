"""
OpenClaw sessions capability.

Ownership detection, message reading, and session-directory updates for
openclaw-backed sessions. Resolved generically by the session routes via
``load_capability('sessions', agent=<backend>)`` so no core router names a
backend.
"""
from __future__ import annotations

from services.cowork_agent.helpers import parse_jsonl
from services.cowork_agent.messages import convert_messages
from services.cowork_agent.sessions_io import find_session_file, update_session_directory
from services.cowork_agent.settings import AGENTS_DIR


def owns_session(session_id: str) -> bool:
    """True if this session is an openclaw native session (~/.openclaw/agents/<a>/sessions/<id>.jsonl)."""
    if AGENTS_DIR.exists():
        for agent_dir in AGENTS_DIR.iterdir():
            if agent_dir.is_dir() and (agent_dir / "sessions" / f"{session_id}.jsonl").exists():
                return True
    return False


def get_messages(session_id: str) -> list:
    """Return converted messages for an openclaw session (empty if no file)."""
    path = find_session_file(session_id)
    if not path:
        return []
    return convert_messages(session_id, parse_jsonl(path))


def set_session_directory(session_id: str, directory: str) -> dict | None:
    """Set the workspace directory for an openclaw session; None if not ours."""
    if update_session_directory(session_id, directory):
        return {"ok": True, "session_id": session_id, "directory": directory}
    return None
