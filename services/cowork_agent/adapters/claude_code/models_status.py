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

import json
from typing import Any

from services.cowork_agent.adapters.cli_status import (
    CliStatusError as ClaudeCodeStatusError,
    resolve_binary,
    run_cli,
)

CLAUDE_BIN_ENV = "CLAUDE_CLI_PATH"
DEFAULT_BIN = "claude"
DEFAULT_TIMEOUT_SECONDS = 15.0

# The single model id we surface for claude_code. Mirrors the
# `<provider>/<model>` shape used by openclaw and hermes for symmetry.
_MODEL_ID = "claude_code/claude"


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
    binary = resolve_binary(CLAUDE_BIN_ENV, DEFAULT_BIN)
    result = await run_cli(
        binary, ("auth", "status", "--json"), timeout=timeout, label="claude"
    )

    out = result.stdout

    if result.returncode != 0:
        raise ClaudeCodeStatusError(
            f"claude exited with code {result.returncode}",
            code="execution_failed",
            detail=result.stderr or out[:300] or None,
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
