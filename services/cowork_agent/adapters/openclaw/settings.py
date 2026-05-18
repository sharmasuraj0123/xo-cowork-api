"""
OpenClaw-specific environment, paths, and constants.

Anchored to the **openclaw manifest specifically** (``get_agent("openclaw")``),
NOT to the active agent. Every consumer of these constants is openclaw-specific
(the openclaw adapter, store, env helper, ``/api/config/openclaw`` endpoint,
...) and must always see openclaw paths regardless of ``AGENT_NAME`` /
``DEFAULT_AGENT``.

Background: previously these constants resolved against ``get_active_agent()``.
With ``AGENT_NAME=hermes`` that made ``OPENCLAW_JSON = ~/.hermes/config.yaml``
and ``AGENTS_DIR = ~/.hermes/profiles``, so an openclaw-shaped write (e.g.
``write_openclaw_config`` from the agent-create flow) silently overwrote
hermes's config. Pinning to ``get_agent("openclaw")`` closes that foot-gun.

Originally lived in ``services/cowork_agent/settings.py``; moved here in
Phase 2 of the OpenClaw modularization.
"""

from services.cowork_agent.agent_registry import get_agent

_OPENCLAW = get_agent("openclaw")

# ── On-disk layout (sourced from the openclaw manifest) ──────────────────────

OPENCLAW_DIR = _OPENCLAW.home_dir
AGENTS_DIR = _OPENCLAW.agents_dir
OPENCLAW_JSON = _OPENCLAW.config_file
DEFAULT_OPENCLAW_WORKSPACE = _OPENCLAW.workspace_dir

# ── API config (sourced from manifest + env) ─────────────────────────────────

OPENCLAW_API_URL = _OPENCLAW.api_url
OPENCLAW_GATEWAY_TOKEN = _OPENCLAW.api_token
OPENCLAW_MODEL = _OPENCLAW.api_model
OPENCLAW_MODEL_CAPABILITIES: dict = dict(_OPENCLAW.model_capabilities)
