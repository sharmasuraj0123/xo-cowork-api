"""
Codex channels-status view.

codex has no messaging channels (``channels.enabled = false`` in the static
defaults). Like claude_code, this returns the empty openclaw-shaped envelope so
xo.json's ``channels.status`` slot is still populated with a valid payload and
the unified route treats every agent uniformly.
"""
from __future__ import annotations

from typing import Any

from services.cowork_agent.adapters.cli_status import CliStatusError

_EMPTY_VIEW: dict[str, Any] = {"channels": []}


def build_status_view(_: Any = None) -> dict[str, Any]:
    """Return the empty channels envelope (argument ignored; kept for signature
    parity with the other adapters)."""
    return dict(_EMPTY_VIEW)


async def get_channels_status(timeout: float | None = None) -> dict[str, Any]:
    return build_status_view()


__all__ = ["CliStatusError", "build_status_view", "get_channels_status"]
