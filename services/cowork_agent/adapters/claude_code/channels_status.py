"""
Claude Code channels-status view.

claude_code has `channels.enabled = false` in the static defaults (cascade
rule zeros out telegram + slack). It has no channel sources to query — the
agent doesn't expose chat platforms — so this adapter simply returns the
empty openclaw-shaped envelope so xo.json's `channels.status` slot still
gets populated with a valid (if empty) payload.

`ClaudeCodeStatusError` is re-exported for symmetry with the other
adapters; the router catches the same union of per-agent error classes.
"""

from __future__ import annotations

from typing import Any

from services.cowork_agent.adapters.claude_code.models_status import (
    ClaudeCodeStatusError,
)

_EMPTY_VIEW: dict[str, Any] = {"channels": []}


def build_status_view(_: Any = None) -> dict[str, Any]:
    """Return the empty channels envelope. Argument ignored; provided so the
    signature matches the other adapters' `build_status_view(parsed_payload)`
    contract."""
    return dict(_EMPTY_VIEW)


async def get_channels_status(timeout: float | None = None) -> dict[str, Any]:
    """No CLI call needed — claude_code has no channels. Returns the empty
    envelope so the unified route and the startup status seed treat
    claude_code uniformly with the other agents."""
    return build_status_view()


__all__ = ["ClaudeCodeStatusError", "build_status_view", "get_channels_status"]
