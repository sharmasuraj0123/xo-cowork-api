"""
Hermes sessions capability.

Hermes owns its messages in per-profile ``state.db`` (no JSONL file). Records
are fetched in the openclaw shape so the shared ``convert_messages`` handles
them unchanged. Hermes sessions have no session-level "directory" concept, so
``set_session_directory`` is a recorded no-op (kept non-404 so the FE workspace
picker doesn't fail on an open hermes chat).
"""
from __future__ import annotations

from services.cowork_agent.hermes_state_db import find_hermes_profile, load_hermes_session_records
from services.cowork_agent.messages import convert_messages


def owns_session(session_id: str) -> bool:
    """True if some hermes profile's state.db contains this session."""
    return find_hermes_profile(session_id) is not None


def get_messages(session_id: str) -> list:
    """Return converted messages for a hermes session from state.db."""
    return convert_messages(session_id, load_hermes_session_records(session_id))


def set_session_directory(session_id: str, directory: str) -> dict | None:
    """No-op directory set for hermes (recorded, not applied); None if not ours."""
    if find_hermes_profile(session_id) is not None:
        return {
            "ok": True,
            "session_id": session_id,
            "directory": directory,
            "backend": "hermes",
            "applied": False,
        }
    return None
