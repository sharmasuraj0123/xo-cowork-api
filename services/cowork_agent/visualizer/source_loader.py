"""
Dynamic loader for the active agent's visualizer source module.

Mirrors :mod:`services.cowork_agent.engine.usage_loader`. The active agent is
resolved from ``AGENT_NAME`` (via
``services.cowork_agent.registry.agent_registry.get_active_agent``); the
corresponding source module is imported at
``services.cowork_agent.adapters.<name>.visualizer_source``.

Unlike the usage loader, a missing source is **not** an error — it
means the active agent has chosen not to publish watcher telemetry.
An agent that ships no ``visualizer_source.py`` produces an empty
source list: the watcher still runs so the sinks keep serving what is
already on disk. (Every agent ships one today, so the branch is
dormant — it exists so a new agent boots before its source is written.)

No if/elif. Single resolver, single source of truth.
"""
from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Optional

from services.cowork_agent.registry.agent_registry import get_active_agent

logger = logging.getLogger(__name__)


def load_source_module() -> Optional[ModuleType]:
    """Return the active agent's visualizer source module, or ``None``
    if the active agent ships no source.
    """
    name = get_active_agent().name
    try:
        return importlib.import_module(
            f"services.cowork_agent.adapters.{name}.visualizer_source"
        )
    except ModuleNotFoundError:
        logger.info(
            "no visualizer source for active agent %r "
            "(expected services/cowork_agent/adapters/%s/visualizer_source.py); "
            "watcher will run with sinks only",
            name, name,
        )
        return None
