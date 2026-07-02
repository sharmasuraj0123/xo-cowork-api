"""
Claude-code providers-status adapter.

Anthropic / OpenAI keys are read from the running process environment —
claude_code has no dedicated ``.env`` file the way openclaw and hermes do; the
CLI inherits whatever cowork-api was launched with.

OpenRouter is different: it is configured by merging an ``env`` block into the
CLI's own ``settings.json`` (see ``routers/cowork_agent/config.py`` + the
manifest's ``providers.openrouter.settings_env``), which is where the key lives.
So its presence is read back from that settings file, not from the process env.
"""

from __future__ import annotations

import os
from typing import Any

from services.cowork_agent.providers_status_lib import build_providers_status
from services.cowork_agent.registry.agent_registry import get_agent
from services.cowork_agent.registry.agent_settings import read_settings_env


def _openrouter_configured() -> bool:
    """True iff settings.json routes to OpenRouter with a non-empty auth token."""
    env = read_settings_env(get_agent("claude_code").config_file)
    base = (env.get("ANTHROPIC_BASE_URL") or "").lower()
    token = (env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    return "openrouter" in base and bool(token)


async def get_providers_status() -> dict[str, Any]:
    return await build_providers_status(
        "claude_code",
        anthropic_key_present=lambda: bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()),
        openai_key_present=lambda: bool((os.environ.get("OPENAI_API_KEY") or "").strip()),
        openrouter_key_present=_openrouter_configured,
    )


__all__ = ["get_providers_status"]
