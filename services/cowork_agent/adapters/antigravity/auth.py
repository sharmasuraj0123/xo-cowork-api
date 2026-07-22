"""
Antigravity (agy) login detection — file-based.

agy exposes **no** ``auth``/``login`` CLI subcommand (unlike claude_code's
``claude auth status --json``), so login state is read directly from the consumer
OAuth token file. This is the antigravity analogue of
``ClaudeCodeAdapter._has_usable_native_login``.

The token file is JSON:

    {"token": {"access_token": "…", "refresh_token": "…",
               "token_type": "Bearer", "expiry": "2026-07-15T18:43:37.5…Z"},
     "auth_method": "consumer"}

A login is *usable* when a ``refresh_token`` is present (agy self-refreshes the
short-lived access token), or the ``access_token`` has not yet expired.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from services.cowork_agent.adapters.antigravity.paths import TOKEN_PATH


def login_state() -> str:
    """Return one of ``"ok"`` | ``"expired"`` | ``"logged_out"`` | ``"invalid"``.

    Never raises — any read/parse problem maps to ``"logged_out"``/``"invalid"``.
    """
    if not TOKEN_PATH.is_file():
        return "logged_out"
    try:
        data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    tok = data.get("token") if isinstance(data, dict) else None
    if not isinstance(tok, dict):
        return "invalid"
    if tok.get("refresh_token"):
        return "ok"  # self-refreshing → usable regardless of access-token expiry
    access, expiry = tok.get("access_token"), tok.get("expiry")
    if not access:
        return "invalid"
    if isinstance(expiry, str):
        try:
            exp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        except ValueError:
            return "ok"  # token present, unparseable expiry — treat as usable
        return "ok" if exp > datetime.now(timezone.utc) else "expired"
    return "ok"  # access token present, no expiry recorded


def has_usable_login() -> bool:
    """True when the agy consumer OAuth token can drive a request right now."""
    return login_state() == "ok"


# Actionable message reused by the adapter's chat-time guard.
LOGIN_REQUIRED_MESSAGE = (
    "Antigravity (agy) is not logged in. There is no headless login command — "
    "run `agy` once in a terminal to complete the Google sign-in (browser OAuth), "
    "then retry. The token self-refreshes afterward."
)


__all__ = ["login_state", "has_usable_login", "LOGIN_REQUIRED_MESSAGE"]
