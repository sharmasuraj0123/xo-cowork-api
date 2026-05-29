"""
Backend-minted session identity for multi-tenant Composio.

For "many users on one backend" we need each browser request to carry *who*
it is without exposing raw XO access tokens to the client. The flow:

1. The XO platform performs the user's XO auth and hands the resulting XO
   access token to this backend (via the consume flow or POST /xo-auth/session).
2. :func:`mint` validates that token once against XO ``/get-user-id``, stores
   ``{user_id, xo_access_token}`` server-side, and returns a random **opaque
   session id**.
3. The browser holds only that opaque id and sends it as
   ``Authorization: Bearer <session_id>`` on every backend call. :func:`resolve`
   maps it back to the ``user_id`` — no per-request XO round-trip, and the real
   XO token never touches the client.

In-process store: a cache miss simply means the caller must re-mint. With a
single worker (the launch-script default) this is sufficient; see the
multi-worker note in ``.env.example`` if ``--workers > 1`` is ever used.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# Default lifetime of a minted session id. Bounded so a leaked opaque id can't
# be replayed forever; the platform re-mints when it expires.
_SESSION_TTL = float(os.getenv("XO_SESSION_TTL", str(12 * 60 * 60)))  # 12h


@dataclass
class _Entry:
    user_id: str
    xo_access_token: str
    expires_at: float  # time.monotonic() deadline


# session_id -> _Entry. Per-worker, in-memory.
_SESSIONS: dict[str, _Entry] = {}


def _prune(now: float) -> None:
    expired = [sid for sid, e in _SESSIONS.items() if e.expires_at <= now]
    for sid in expired:
        _SESSIONS.pop(sid, None)


def register(user_id: str, xo_access_token: str, ttl_seconds: Optional[float] = None) -> Optional[str]:
    """Store a session for an already-validated ``user_id`` and return its opaque id.

    For the consume flow, where XO has already resolved the user — no second
    ``/get-user-id`` round-trip needed.
    """
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
    """Validate an XO access token and return an opaque session id for it.

    Returns None if the token is missing or rejected by XO ``/get-user-id``.
    """
    if not xo_access_token:
        return None
    # Local import: composio_identity also imports this module's resolve() at
    # call time, so keep the dependency function-scoped to avoid a load cycle.
    from services.composio_identity import _validate_token

    user_id = await _validate_token(xo_access_token)
    if not user_id:
        return None
    return register(user_id, xo_access_token, ttl_seconds)


def resolve(session_id: str) -> Optional[str]:
    """Return the ``user_id`` for an opaque session id, or None if unknown/expired."""
    if not session_id:
        return None
    entry = _SESSIONS.get(session_id)
    if entry is None:
        return None
    if entry.expires_at <= time.monotonic():
        _SESSIONS.pop(session_id, None)
        return None
    return entry.user_id
