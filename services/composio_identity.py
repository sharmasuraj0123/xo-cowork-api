"""
Composio per-user identity.

Two resolution paths feed the same notion of "which Composio user is this":

1. **Management UI** — REST under ``/api/connectors/composio/*``. The browser
   sends ``Authorization: Bearer <xo_token>``. :func:`get_composio_user` (a
   FastAPI dependency) validates it against XO's ``/get-user-id`` and returns
   the real ``user_id``.
2. **Agent runtime** — the loopback MCP proxy is called header-less, so the
   ``user_id`` is baked into the proxy URL at config-write time as an
   HMAC-signed token. :func:`sign_proxy_token` / :func:`verify_proxy_token`
   mint and check it; any worker verifies without a shared store.

Everything is gated by ``COMPOSIO_MULTI_TENANT`` (default off). Off → callers
fall back to the legacy ``default_user`` sentinel and nothing changes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Optional

import httpx
from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

# Matches the sentinel in composio_service / routers.cowork_agent.composio so a
# flag-off install keeps sharing one set of connected accounts.
_DEFAULT_USER_ID = "default_user"


def multi_tenant_enabled() -> bool:
    """True when per-user Composio isolation is switched on."""
    return os.getenv("COMPOSIO_MULTI_TENANT", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ---------------------------------------------------------------------------
# Bearer → XO user_id (management UI path)
# ---------------------------------------------------------------------------

# token -> (user_id, expires_at_monotonic). Per-worker, no external store — a
# cache miss just re-validates against XO. Short TTL bounds staleness after a
# token is revoked upstream.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_TTL_SECONDS = float(os.getenv("COMPOSIO_IDENTITY_CACHE_TTL", "90"))


def _extract_bearer(request: Request) -> Optional[str]:
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
    from routers.auth import CHAT_API_BASE_URL, HTTP_TIMEOUT, XO_GET_USER_ID_PATH

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
    callers with their own auth semantics (chat) fall back to legacy resolution.
    """
    token = _extract_bearer(request)
    if not token:
        return None
    # Local import: session_identity.mint() imports _validate_token from here,
    # so keep this function-scoped to avoid a module load cycle.
    from services.session_identity import resolve as resolve_session
    uid = resolve_session(token)
    if uid:
        return uid
    return await _validate_token(token)


async def get_composio_user(request: Request) -> Optional[str]:
    """FastAPI dependency for the Composio management endpoints.

    - Flag **off** → returns None, signalling the caller to use its legacy
      ``_resolve_user_id`` chain (body / state / auth_state / default_user).
      Nothing changes versus today.
    - Flag **on** → requires a valid ``Authorization: Bearer`` token. Missing or
      invalid → 401. The validated identity wins over any ``body.user_id``.
    """
    if not multi_tenant_enabled():
        return None
    if not _extract_bearer(request):
        raise HTTPException(
            status_code=401,
            detail="Missing bearer token (Composio multi-tenant mode is on).",
        )
    user_id = await resolve_user_from_bearer(request)
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired bearer token.",
        )
    return user_id


# ---------------------------------------------------------------------------
# Signed proxy token (agent-runtime path)
# ---------------------------------------------------------------------------
#
# The agent's MCP proxy call carries no headers, so the user is encoded into the
# proxy URL path as ``base64url(user_id).base64url(hmac_sha256)``. Stateless and
# signed: any worker verifies it with COMPOSIO_STATE_SECRET, and a local process
# can't forge a different user without the secret.


def _state_secret() -> Optional[str]:
    return os.getenv("COMPOSIO_STATE_SECRET", "").strip() or None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_proxy_token(user_id: str) -> str:
    """Mint a stateless, URL-safe proxy token encoding ``user_id``.

    Raises if COMPOSIO_STATE_SECRET is unset — in multi-tenant prod the secret
    is required and a missing one must fail loudly rather than silently drop to
    an unsigned, spoofable URL.
    """
    secret = _state_secret()
    if not secret:
        raise RuntimeError(
            "COMPOSIO_STATE_SECRET is not set; cannot sign Composio proxy tokens."
        )
    payload = _b64url(user_id.encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64url(sig)}"


def verify_proxy_token(token: str) -> Optional[str]:
    """Return the ``user_id`` carried by a proxy token, or None if invalid."""
    secret = _state_secret()
    if not secret or not token or "." not in token:
        return None
    payload_b64, _, sig_b64 = token.partition(".")
    try:
        expected = hmac.new(
            secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        got = _b64url_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, got):
        return None
    try:
        return _b64url_decode(payload_b64).decode("utf-8")
    except Exception:
        return None
