from __future__ import annotations
from typing import TYPE_CHECKING

from services.cowork_agent.adapters.base import BaseAgentAdapter
from services.cowork_agent.adapters.openclaw.adapter import OpenclawAdapter
from services.cowork_agent.adapters.claude_code.adapter import ClaudeCodeAdapter

_REGISTRY: dict[str, type[BaseAgentAdapter]] = {
    "openclaw":    OpenclawAdapter,
    "claude_code": ClaudeCodeAdapter,
}


def get_adapter(name: str, config: dict) -> BaseAgentAdapter:
    cls = _REGISTRY.get(name)
    if cls is None:
        registered = list(_REGISTRY.keys())
        raise ValueError(
            f"Unknown agent adapter: {name!r}. Registered adapters: {registered}"
        )
    return cls(config)


def list_adapters() -> list[str]:
    return list(_REGISTRY.keys())
