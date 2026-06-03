"""
Dynamic loader for the active agent's usage module.

The active agent is resolved from ``AGENT_NAME`` (or ``DEFAULT_AGENT``) via
``get_active_agent()``. The corresponding usage module is imported at
``services.cowork_agent.adapters.<name>.usage`` — alongside the other
agent-specific Python (adapter.py, channels_status.py, providers_status.py,
models_status.py, streaming.py, transcript.py). Adding a new agent is
"drop services/cowork_agent/adapters/<name>/usage.py" with no other code
changes.

No if/elif dispatcher. Single resolver, single source of truth.
"""
from __future__ import annotations

import importlib
from types import ModuleType

from services.cowork_agent.agent_registry import get_active_agent


def load_usage_module() -> ModuleType:
    """Return the active agent's usage module.

    Raises:
        ModuleNotFoundError: if the active agent has no
            ``services/cowork_agent/adapters/<name>/usage.py``. The error
            names the path we tried to import so the fix is obvious.
    """
    name = get_active_agent().name
    return importlib.import_module(f"services.cowork_agent.adapters.{name}.usage")
