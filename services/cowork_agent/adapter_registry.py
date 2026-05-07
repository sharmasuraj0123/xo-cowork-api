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


def get_sessions_roots() -> "dict[str, pathlib.Path]":
    """Return {adapter_name: sessions_root} for every adapter that declares one.

    Used by find_session_backend() so new adapters only need to override
    sessions_root() in their class — no other files need touching.
    """
    import pathlib
    return {
        name: root
        for name, cls in _REGISTRY.items()
        if (root := cls.sessions_root()) is not None
    }


def get_session_lookup_specs() -> "dict[str, list[tuple[pathlib.Path, str]]]":
    """Return {adapter_name: [(root, subpath), ...]} for ownership detection.

    Each tuple says: under ``root``, every immediate subdir's
    ``<entry>/<subpath>/<session_id>.jsonl`` is a candidate for this
    adapter. find_session_backend() checks every tuple in order.
    """
    return {name: cls.session_lookup_specs() for name, cls in _REGISTRY.items()}
