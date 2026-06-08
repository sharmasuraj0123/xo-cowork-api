"""
Hermes model listing (`/api/models`).

One model row per hermes profile under ``~/.hermes/profiles/``. Reads from the
hermes state-db helper rather than scanning openclaw's agents dir — those two
layouts are independent, and listing the openclaw dir under hermes produced
stale model rows for the real hermes profiles.
"""

from __future__ import annotations

from services.cowork_agent.agent_registry import get_active_agent
from services.cowork_agent.adapters.hermes.state_db import list_all_profile_names
from services.cowork_agent.settings import OPENCLAW_MODEL_CAPABILITIES

_HERMES = get_active_agent()


def list_models() -> list[dict]:
    """One model row per hermes profile under ``~/.hermes/profiles/``."""
    prefix = _HERMES.model_prefix
    models: list[dict] = []
    for profile_name in list_all_profile_names():
        models.append(
            {
                "id": f"{prefix}/{profile_name}",
                "name": profile_name,
                "provider_id": _HERMES.name,
                "capabilities": dict(OPENCLAW_MODEL_CAPABILITIES),
                "pricing": {"prompt": 0, "completion": 0},
                "metadata": {"hermes_profile": profile_name},
            }
        )
    if not models:
        # Fresh install — surface at least one row so the dropdown isn't
        # blank before the user creates a profile.
        models.append(
            {
                "id": f"{prefix}/default",
                "name": "default",
                "provider_id": _HERMES.name,
                "capabilities": dict(OPENCLAW_MODEL_CAPABILITIES),
                "pricing": {"prompt": 0, "completion": 0},
                "metadata": {"hermes_profile": "default"},
            }
        )
    return models
