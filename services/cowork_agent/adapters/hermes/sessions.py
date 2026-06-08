"""
Hermes sessions capability.

Hermes owns its messages in per-profile ``state.db`` (no JSONL file). Records
are fetched in the openclaw shape so the shared ``convert_messages`` handles
them unchanged. Hermes sessions have no session-level "directory" concept, so
``set_session_directory`` is a recorded no-op (kept non-404 so the FE workspace
picker doesn't fail on an open hermes chat).

Implements the full sessions contract uniformly with the other adapters. Hermes
does NOT tee into xo-projects (``USES_PROJECT_SESSIONS = False``), so the
project-tied hooks (``enrich_project_session`` / ``resolve_native_file``) are
never reached for hermes — they're defined to keep the surface identical.
``list_native_sessions`` returns the state.db rows that used to be special-cased
in ``sessions_io`` behind ``active_backend == "hermes"``.
"""
from __future__ import annotations

from pathlib import Path

from services.cowork_agent.hermes_state_db import (
    find_hermes_profile,
    list_hermes_sessions,
    load_hermes_session_records,
)
from services.cowork_agent.messages import convert_messages

# Hermes reads sessions from state.db, never from the xo-projects scan.
USES_PROJECT_SESSIONS = False


def enrich_project_session(meta: dict, key: str, default_agent: str):
    """Hermes never appears in the project-tied scan; identity enrichment."""
    return None, None, default_agent


def resolve_native_file(meta: dict, session_id: str) -> Path | None:
    """Hermes messages live in state.db, not a JSONL file."""
    return None


def list_native_sessions() -> list[dict]:
    """Full session rows from ~/.hermes/state.db + per-profile state.dbs."""
    return list_hermes_sessions()


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
