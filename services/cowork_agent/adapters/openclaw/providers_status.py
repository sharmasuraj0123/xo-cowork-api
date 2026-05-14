"""
Openclaw providers-status adapter.

Resolves API-key presence from ``~/.openclaw/.env`` (override via
``OPENCLAW_HOME``). OAuth probes are delegated to the shared lib since they
query CLI-local state that doesn't vary by agent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from services.cowork_agent.providers_status_lib import (
    build_providers_status,
    parse_env_file,
)


def _openclaw_env_path() -> Path:
    home = (os.getenv("OPENCLAW_HOME", "") or "").strip()
    return (Path(home) if home else Path.home() / ".openclaw") / ".env"


async def get_providers_status() -> dict[str, Any]:
    env = parse_env_file(_openclaw_env_path())
    return await build_providers_status(
        "openclaw",
        anthropic_key_present=lambda: bool(env.get("ANTHROPIC_API_KEY", "").strip()),
        openai_key_present=lambda: bool(env.get("OPENAI_API_KEY", "").strip()),
    )


__all__ = ["get_providers_status"]
