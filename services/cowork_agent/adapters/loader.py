"""
Dynamic capability resolver — the single seam every agent-specific module is
reached through.

Everything agent-specific lives at
``services.cowork_agent.adapters.<AGENT_NAME>.<capability>`` and is resolved
from the active agent (``AGENT_NAME`` → ``DEFAULT_AGENT`` → single-manifest
auto-pick, via ``get_active_agent()``). No core module names a specific agent;
it asks for a *capability* and the loader imports the active agent's
implementation.

Capabilities are just module names inside the adapter package, e.g.
``adapter``, ``usage``, ``models_status``, ``channels_status``,
``providers_status``, ``visualizer_source``, ``routes``.

Adding a new agent is "drop services/cowork_agent/adapters/<name>/" — no edits
to any core file.
"""
from __future__ import annotations

import importlib
from types import ModuleType

from services.cowork_agent.registry.agent_registry import get_active_agent


def load_capability(capability: str, *, agent: str | None = None) -> ModuleType:
    """Import the (active) agent's implementation of ``capability``.

    Args:
        capability: module name under the adapter package (e.g. ``"usage"``).
        agent: explicit agent name; defaults to the active agent.

    Raises:
        ModuleNotFoundError: if the agent has no
            ``services/cowork_agent/adapters/<name>/<capability>.py``. The
            error names the import path so the fix is obvious.
    """
    name = agent or get_active_agent().name
    return importlib.import_module(
        f"services.cowork_agent.adapters.{name}.{capability}"
    )


def try_load_capability(capability: str, *, agent: str | None = None) -> ModuleType | None:
    """Like :func:`load_capability` but returns ``None`` when the active agent
    does not implement ``capability`` (instead of raising).

    Use for optional capabilities — e.g. agent-owned ``routes`` that only some
    agents provide.
    """
    try:
        return load_capability(capability, agent=agent)
    except ModuleNotFoundError:
        return None
