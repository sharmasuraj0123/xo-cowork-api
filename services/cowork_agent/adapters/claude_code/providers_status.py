"""
Claude-code providers-status adapter.

Reads API keys from the running process environment — claude_code has no
dedicated ``.env`` file the way openclaw and hermes do; the CLI inherits
whatever cowork-api was launched with.
"""

from __future__ import annotations

import os
from typing import Any

from services.cowork_agent.providers_status_lib import build_providers_status


async def get_providers_status() -> dict[str, Any]:
    return await build_providers_status(
        "claude_code",
        anthropic_key_present=lambda: bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()),
        openai_key_present=lambda: bool((os.environ.get("OPENAI_API_KEY") or "").strip()),
    )


__all__ = ["get_providers_status"]
