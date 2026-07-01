"""
Claude-code providers-status adapter.

OpenRouter is reported from Claude Code's own ``settings.json`` (where the API
Keys "Save" flow writes it), so the tile reflects whether ``claude`` is actually
pointed at OpenRouter. Anthropic/OpenAI keys still come from the running process
environment — the CLI inherits whatever cowork-api was launched with.
"""

from __future__ import annotations

import os
from typing import Any

from services.cowork_agent.providers_status_lib import build_providers_status
from services.cowork_agent.openrouter_settings import read_openrouter_state


async def get_providers_status() -> dict[str, Any]:
    openrouter_on = bool(read_openrouter_state().get("connected"))
    return await build_providers_status(
        "claude_code",
        anthropic_key_present=lambda: bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()),
        openai_key_present=lambda: bool((os.environ.get("OPENAI_API_KEY") or "").strip()),
        openrouter_key_present=lambda: openrouter_on,
    )


__all__ = ["get_providers_status"]
