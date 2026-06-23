"""
Adapter resolution — no hardcoded agent list.

An adapter for agent ``<name>`` is the class exposed as ``Adapter`` in
``services.cowork_agent.adapters.<name>.adapter``. Both lookups go through the
dynamic ``load_capability`` seam, so adding an agent is "drop
``services/cowork_agent/adapters/<name>/adapter.py`` (exposing ``Adapter``)"
— this file never changes.
"""
from __future__ import annotations

from pathlib import Path

import services.cowork_agent.adapters as _adapters_pkg
from services.cowork_agent.adapters.base import BaseAgentAdapter
from services.cowork_agent.adapters.loader import load_capability

# Resolve from the adapters package itself, so this is independent of where
# adapter_registry.py lives.
_ADAPTERS_DIR = Path(_adapters_pkg.__file__).resolve().parent


def get_adapter(name: str, config: dict) -> BaseAgentAdapter:
    """Instantiate the adapter for ``name`` from its ``adapter`` module."""
    try:
        module = load_capability("adapter", agent=name)
    except ModuleNotFoundError as exc:
        raise ValueError(
            f"Unknown agent adapter: {name!r}. Available adapters: {list_adapters()}"
        ) from exc

    adapter_cls = getattr(module, "Adapter", None)
    if adapter_cls is None:
        raise ValueError(
            f"adapter module for {name!r} does not expose an `Adapter` class "
            f"(expected `Adapter = <YourAdapter>` in "
            f"services/cowork_agent/adapters/{name}/adapter.py)"
        )
    return adapter_cls(config)


def list_adapters() -> list[str]:
    """Discover adapters by scanning for ``adapters/<name>/adapter.py``."""
    if not _ADAPTERS_DIR.exists():
        return []
    return sorted(
        p.name
        for p in _ADAPTERS_DIR.iterdir()
        if p.is_dir() and (p / "adapter.py").exists()
    )
