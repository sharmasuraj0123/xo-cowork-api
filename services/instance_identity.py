"""
Single-instance real-user identity.

This backend authenticates to XO with one global credential (``XO_API_KEY`` or
the consumed session token), which resolves to exactly one real XO user. Without
this module, Composio calls that don't carry an explicit user fall through to the
generic ``"default_user"`` sentinel, so the Composio dashboard shows everything
under ``default_user`` instead of the real account.

When ``XO_RESOLVE_INSTANCE_USER`` is on, :func:`prime_instance_user_id` resolves
that real user once at startup (reading the existing auth token, validating it
against XO ``/get-user-id``) and caches it here. :func:`instance_user_id` is then
a sync, no-network read that ``_resolve_user_id`` consults as its final fallback
in place of ``"default_user"``.

Deliberately self-contained: it only *reads* existing auth state via
``get_auth_token()`` and never writes ``auth_state`` or any other existing global.
The cache below is the only state this module owns.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# The only state this module owns. None until primed (or if disabled/unresolved).
_INSTANCE_USER_ID: Optional[str] = None


def enabled() -> bool:
    """True when instance-user resolution is switched on."""
    return os.getenv("XO_RESOLVE_INSTANCE_USER", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def instance_user_id() -> Optional[str]:
    """Sync, no-network read of the cached real instance user_id (or None)."""
    return _INSTANCE_USER_ID


async def prime_instance_user_id() -> Optional[str]:
    """Resolve the real instance user from the existing auth token and cache it.

    Best-effort and non-fatal: returns None (leaving the cache empty, so callers
    fall back to ``default_user``) when disabled, when there is no token, or when
    XO ``/get-user-id`` is unreachable/rejects. Does not touch ``auth_state``.
    """
    global _INSTANCE_USER_ID
    if not enabled():
        return None

    # Local imports: read-only reuse of existing auth config/helpers, kept
    # function-scoped to avoid import-time coupling.
    import httpx
    from routers.auth import (
        CHAT_API_BASE_URL,
        HTTP_TIMEOUT,
        XO_GET_USER_ID_PATH,
        get_auth_token,
    )

    token = get_auth_token()
    if not token:
        log.warning("instance_identity: no auth token available; staying on default_user")
        return None

    url = f"{CHAT_API_BASE_URL.rstrip('/')}{XO_GET_USER_ID_PATH}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
    except Exception as exc:
        log.warning("instance_identity: /get-user-id request failed: %s", exc)
        return None

    if resp.status_code != 200:
        log.warning("instance_identity: /get-user-id rejected token (status=%s)", resp.status_code)
        return None
    try:
        user_id = resp.json().get("user_id")
    except Exception:
        return None
    if not user_id:
        return None

    _INSTANCE_USER_ID = str(user_id)
    log.info("instance_identity: resolved instance user_id=%s", _INSTANCE_USER_ID)
    return _INSTANCE_USER_ID
