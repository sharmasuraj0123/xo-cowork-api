"""
Claude Code models-status view.

Runs `claude auth status --json` and maps the result into the openclaw
envelope `{default, models:[{id, status}]}` — strictly the common schema,
no extras. The frontend can re-query the underlying CLI if it ever needs
the email / subscription tier; we don't surface them here so all three
agents (openclaw, hermes, claude_code) hand back a uniform shape.

`claude auth status --json` returns one of three shapes today:

OAuth (claude.ai login):
    {"loggedIn": true, "authMethod": "claude.ai",
     "apiProvider": "firstParty", "email": "…", "orgId": "…",
     "orgName": "…", "subscriptionType": "max"}

API key (ANTHROPIC_API_KEY env / similar):
    {"loggedIn": true, "authMethod": "claude.ai",
     "apiProvider": "firstParty", "apiKeySource": "ANTHROPIC_API_KEY",
     "email": null, "orgId": null, "orgName": null,
     "subscriptionType": null}

Logged out:
    {"loggedIn": false, "authMethod": "none",
     "apiProvider": "firstParty"}

The single derivation rule: `loggedIn` is the source of truth — true → "ok",
anything else → "error".
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Any, Optional

CLAUDE_BIN_ENV = "CLAUDE_CLI_PATH"
DEFAULT_BIN = "claude"
DEFAULT_TIMEOUT_SECONDS = 15.0

# The single model id we surface for claude_code. Mirrors the
# `<provider>/<model>` shape used by openclaw and hermes for symmetry.
_MODEL_ID = "claude_code/claude"


class ClaudeCodeStatusError(Exception):
    """CLI invocation/parse failure. `code` maps to an HTTP status in the router."""

    def __init__(self, message: str, *, code: str, detail: Optional[str] = None):
        super().__init__(message)
        self.code = code  # binary_not_found | timeout | execution_failed | invalid_output
        self.detail = detail


def _resolve_binary() -> str:
    configured = (os.getenv(CLAUDE_BIN_ENV, "") or "").strip()
    return configured or shutil.which(DEFAULT_BIN) or DEFAULT_BIN


def build_status_view(auth_payload: dict[str, Any]) -> dict[str, Any]:
    """Translate a parsed `claude auth status --json` dict into the common
    `{default, models}` envelope. Only `loggedIn` is consumed — everything
    else (email, subscription, api-key source) is intentionally dropped to
    keep the response shape identical to openclaw's."""
    logged_in = bool(auth_payload.get("loggedIn"))
    status = "ok" if logged_in else "error"
    return {
        "default": _MODEL_ID,
        "models": [{"id": _MODEL_ID, "status": status}],
    }


async def fetch_raw_status(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run `claude auth status --json` and return the parsed JSON dict."""
    binary = _resolve_binary()
    if os.path.isabs(binary) and not os.path.isfile(binary):
        raise ClaudeCodeStatusError(
            f"claude binary not found at {binary}",
            code="binary_not_found",
            detail=binary,
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "auth", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise ClaudeCodeStatusError(
            f"claude binary unavailable: {binary}",
            code="binary_not_found",
            detail=str(exc),
        )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        raise ClaudeCodeStatusError(f"claude timed out after {timeout}s", code="timeout")

    out = (stdout or b"").decode("utf-8", errors="replace").strip()
    err = (stderr or b"").decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        raise ClaudeCodeStatusError(
            f"claude exited with code {proc.returncode}",
            code="execution_failed",
            detail=err or out[:300] or None,
        )

    try:
        parsed = json.loads(out) if out else None
    except json.JSONDecodeError as exc:
        raise ClaudeCodeStatusError(
            "claude auth status returned invalid JSON",
            code="invalid_output",
            detail=str(exc),
        )

    if not isinstance(parsed, dict):
        raise ClaudeCodeStatusError(
            "claude auth status returned empty or non-object output",
            code="invalid_output",
        )
    return parsed


async def get_models_status(timeout: float | None = None) -> dict[str, Any]:
    """Fetch claude auth status and project it into the common envelope."""
    if timeout is None:
        payload = await fetch_raw_status()
    else:
        payload = await fetch_raw_status(timeout=timeout)
    return build_status_view(payload)


__all__ = ["ClaudeCodeStatusError", "build_status_view", "get_models_status"]
