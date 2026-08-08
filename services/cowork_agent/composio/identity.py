from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx
from fastapi import HTTPException, Request

log = logging.getLogger(__name__)


_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_TTL_SECONDS = float(os.getenv("COMPOSIO_IDENTITY_CACHE_TTL", "90"))


_SESSION_HEADER = "x-xo-session"


def _extract_bearer(request: Request) -> Optional[str]:
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
    now = time.monotonic()
    cached = _TOKEN_CACHE.get(token)
    if cached and cached[1] > now:
        return cached[0]

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
    token = _extract_bearer(request)
    if not token:
        return None
    from services.cowork_agent.composio.session_identity import resolve as resolve_session
    uid = resolve_session(token)
    if uid:
        return uid
    return await _validate_token(token)


async def get_composio_user(request: Request) -> str:
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
