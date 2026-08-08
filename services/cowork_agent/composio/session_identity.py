from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

_SESSION_TTL = float(os.getenv("XO_SESSION_TTL", str(12 * 60 * 60)))


@dataclass
class _Entry:
    user_id: str
    xo_access_token: str
    expires_at: float


_SESSIONS: dict[str, _Entry] = {}


def _prune(now: float) -> None:
    expired = [sid for sid, e in _SESSIONS.items() if e.expires_at <= now]
    for sid in expired:
        _SESSIONS.pop(sid, None)


def register(user_id: str, xo_access_token: str, ttl_seconds: Optional[float] = None) -> Optional[str]:
    if not user_id:
        return None
    now = time.monotonic()
    _prune(now)
    session_id = secrets.token_urlsafe(32)
    _SESSIONS[session_id] = _Entry(
        user_id=str(user_id),
        xo_access_token=xo_access_token or "",
        expires_at=now + (ttl_seconds if ttl_seconds and ttl_seconds > 0 else _SESSION_TTL),
    )
    log.info("session_identity: registered session for user=%s", user_id)
    return session_id


async def mint(xo_access_token: str, ttl_seconds: Optional[float] = None) -> Optional[str]:
    if not xo_access_token:
        return None
    from services.cowork_agent.composio.identity import _validate_token

    user_id = await _validate_token(xo_access_token)
    if not user_id:
        return None
    return register(user_id, xo_access_token, ttl_seconds)


def resolve(session_id: str) -> Optional[str]:
    if not session_id:
        return None
    entry = _SESSIONS.get(session_id)
    if entry is None:
        return None
    if entry.expires_at <= time.monotonic():
        _SESSIONS.pop(session_id, None)
        return None
    return entry.user_id
