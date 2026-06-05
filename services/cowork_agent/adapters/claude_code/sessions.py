"""
Claude Code sessions capability.

Message reading and session-directory updates for claude_code sessions.
Ownership is determined by the project ``sessionslist.json`` backend tag (read
generically in ``find_session_backend``), so ``owns_session`` returns False
here — there is no separate native-scan fallback for claude_code.
"""
from __future__ import annotations

from services.cowork_agent.helpers import parse_jsonl
from services.cowork_agent.messages import convert_native_claude_messages
from services.cowork_agent.sessions_io import find_session_file, update_claude_session_directory


def owns_session(session_id: str) -> bool:
    """claude_code sessions are project-tee'd and detected via the sessionslist tag."""
    return False


def get_messages(session_id: str) -> list:
    """Return converted messages for a claude_code session (empty if no file)."""
    path = find_session_file(session_id)
    if not path:
        return []
    return convert_native_claude_messages(session_id, parse_jsonl(path))


def set_session_directory(session_id: str, directory: str) -> dict | None:
    """Set the workspace directory for a claude_code session; None if not ours."""
    if update_claude_session_directory(session_id, directory):
        return {"ok": True, "session_id": session_id, "directory": directory}
    return None
