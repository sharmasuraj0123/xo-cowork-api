"""
Composio per-user identity — who is making this request.

This module answers that for the **management UI** only: REST under
``/api/connectors/composio/*``. The browser sends ``X-XO-Session:
<session_id>`` (or ``Authorization: Bearer <session_id>``);
:func:`get_composio_user` (a FastAPI dependency) resolves it to the real XO
``user_id``, or 401s.

The **agent runtime** needs nothing from this module. Its MCP config carries an
opaque per-user token that ``services/composio/service.py`` mints and
``mcp_proxy.py`` resolves — a lookup, not a signature, so no shared secret is
involved. Beneath that, isolation is Composio's own:
``sessions.create(user_id=...)`` binds a session to one user server-side.

Multi-tenancy is unconditional: there is no shared sentinel user and no
process-wide "instance user". Every Composio call carries a real, per-request
XO ``user_id`` or it is rejected. ``/api/connectors/composio/*`` needs a valid
session id — mint one with ``POST /xo-auth/session`` (platform hands over an XO
token), ``GET /xo-auth/session/self`` (backend mints for its own XO credential —
the local-UI bootstrap), or read ``session_id`` off the auth-consume response.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx
from fastapi import HTTPException, Request

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bearer → XO user_id (management UI path)
# ---------------------------------------------------------------------------

# token -> (user_id, expires_at_monotonic). Per-worker, no external store — a
# cache miss just re-validates against XO. Short TTL bounds staleness after a
# token is revoked upstream.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_TTL_SECONDS = float(os.getenv("COMPOSIO_IDENTITY_CACHE_TTL", "90"))


# Dedicated header for the session id. ``Authorization`` is already spoken for
# in remote-tunnel mode (the tunnel's own bearer), so a browser behind a tunnel
# has no way to also carry its XO identity there. This header is checked first
# and never collides.
_SESSION_HEADER = "x-xo-session"


def _extract_bearer(request: Request) -> Optional[str]:
    """Return the caller's identity token from ``X-XO-Session`` or ``Bearer``."""
    session_header = (request.headers.get(_SESSION_HEADER) or "").strip()
    if session_header:
        return session_header
    auth = request.headers.get("authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


async def _validate_token(token: str) -> Optional[str]:
    """Resolve a bearer token to an XO ``user_id``, or None if invalid.

    Reuses the httpx shape from routers/auth.py's ``/whoami``. Caches positive
    results for ``_TOKEN_TTL_SECONDS``.
    """
    now = time.monotonic()
    cached = _TOKEN_CACHE.get(token)
    if cached and cached[1] > now:
        return cached[0]

    # Local import avoids a circular dependency at module load.
    from routers.auth.auth import CHAT_API_BASE_URL, HTTP_TIMEOUT, XO_GET_USER_ID_PATH

    url = f"{CHAT_API_BASE_URL.rstrip('/')}{XO_GET_USER_ID_PATH}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        log.warning("composio_identity: token validation request failed: %s", exc)
        return None

    if resp.status_code != 200:
        log.info("composio_identity: token rejected by XO (status=%s)", resp.status_code)
        return None
    try:
        user_id = resp.json().get("user_id")
    except Exception:
        return None
    if not user_id:
        return None

    user_id = str(user_id)
    _TOKEN_CACHE[token] = (user_id, now + _TOKEN_TTL_SECONDS)
    return user_id


async def resolve_user_from_bearer(request: Request) -> Optional[str]:
    """Resolve the request's Bearer token to a ``user_id``.

    Two token shapes are accepted, in order:
    1. A backend-minted opaque **session id** → looked up in the in-process
       session store (no XO round-trip). This is the normal browser path.
    2. A raw **XO access token** → validated against XO ``/get-user-id``
       (fallback for direct/platform callers).

    Never raises — returns None when there's no token or neither shape resolves.
    Callers that must enforce identity (the UI dependency) turn None into a 401;
    callers with softer semantics (chat, /api/tools) degrade to "no Composio
    identity" rather than guessing a user.
    """
    token = _extract_bearer(request)
    if not token:
        return None
    # Local import: session_identity.mint() imports _validate_token from here,
    # so keep this function-scoped to avoid a module load cycle.
    from services.composio.session_identity import resolve as resolve_session
    uid = resolve_session(token)
    if uid:
        return uid
    return await _validate_token(token)


async def get_composio_user(request: Request) -> str:
    """FastAPI dependency for the Composio management endpoints.

    Requires a valid session id (``X-XO-Session``, or ``Authorization: Bearer``)
    and returns the real XO ``user_id`` behind it. Missing or invalid → 401.
    There is no fallback
    identity: a request that cannot say who it is cannot touch anyone's
    connections. Any ``body.user_id`` is ignored, so it can't impersonate.
    """
    if not _extract_bearer(request):
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing session identity. Send 'X-XO-Session: <session_id>' "
                "(or 'Authorization: Bearer <session_id>'). Mint one with "
                "POST /xo-auth/session, or GET /xo-auth/session/self when the "
                "backend holds the XO credential."
            ),
        )
    user_id = await resolve_user_from_bearer(request)
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired bearer token.",
        )
    return user_id

