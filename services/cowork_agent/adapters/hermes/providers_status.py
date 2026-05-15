"""
Hermes providers-status adapter.

Resolves API-key presence from ``~/.hermes/.env`` — the base env hermes
gateways inherit (see ``adapters/hermes/gateway_pool.py`` for the broader
layering). Per-profile envs aren't consulted here; they're for channel
tokens, not provider keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from services.cowork_agent.providers_status_lib import (
    build_providers_status,
    parse_env_file,
)


def _hermes_env_path() -> Path:
    return Path.home() / ".hermes" / ".env"


async def get_providers_status() -> dict[str, Any]:
    env = parse_env_file(_hermes_env_path())
    return await build_providers_status(
        "hermes",
        anthropic_key_present=lambda: bool(env.get("ANTHROPIC_API_KEY", "").strip()),
        openai_key_present=lambda: bool(env.get("OPENAI_API_KEY", "").strip()),
    )


__all__ = ["get_providers_status"]
