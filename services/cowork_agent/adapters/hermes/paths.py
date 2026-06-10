"""
Hermes on-disk layout + API config constants, sourced from the hermes manifest
(``get_agent("hermes")``).

These were historically defined in ``services.cowork_agent.registry.settings`` as
``HERMES_*``; they live here now so the agent-specific resolution sits in the
hermes adapter, not in core. ``settings`` re-exports the same names for
backward compatibility.

Anchored to the hermes manifest **specifically** (not the active agent): every
consumer is hermes-specific and must always see hermes paths regardless of
``AGENT_NAME``.
"""
from __future__ import annotations

from services.cowork_agent.registry.agent_registry import get_agent

_HERMES = get_agent("hermes")

# ── On-disk layout ───────────────────────────────────────────────────────────

HERMES_DIR = _HERMES.home_dir
HERMES_PROFILES_DIR = _HERMES.agents_dir
HERMES_CONFIG_FILE = _HERMES.config_file

# ── API config ───────────────────────────────────────────────────────────────

HERMES_API_URL = _HERMES.api_url
HERMES_API_TOKEN = _HERMES.api_token
HERMES_MODEL = _HERMES.api_model
HERMES_SESSION_HEADER = (_HERMES.raw.get("api") or {}).get("session_header", "")
HERMES_MODEL_CAPABILITIES: dict = dict(_HERMES.model_capabilities)
