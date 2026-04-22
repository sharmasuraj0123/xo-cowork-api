"""
Environment, paths, and constants for the cowork_agent subsystem.

Values previously hardcoded to OpenClaw are now sourced from the active
agent manifest (see `services/cowork_agent/agent_registry.py` and
`config/agents/*.json`). The module-level names are preserved so the rest
of the subsystem keeps working without edits.
"""

import re

from dotenv import load_dotenv

from services.cowork_agent.agent_registry import get_default_agent

load_dotenv()

_AGENT = get_default_agent()

# ── Active-agent on-disk layout (sourced from manifest) ──────────────────────

OPENCLAW_DIR = _AGENT.home_dir
AGENTS_DIR = _AGENT.agents_dir
OPENCLAW_JSON = _AGENT.config_file
DEFAULT_OPENCLAW_WORKSPACE = _AGENT.workspace_dir

# ── Agent id normalization regexes ───────────────────────────────────────────

_VALID_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
_INVALID_AGENT_ID_CHARS = re.compile(r"[^a-z0-9_-]+", re.IGNORECASE)
_LEADING_DASHES = re.compile(r"^-+")
_TRAILING_DASHES = re.compile(r"-+$")

# ── Workspace doc sets ───────────────────────────────────────────────────────

_WORKSPACE_SEED_FILES = (
    "AGENTS.md",
    "SOUL.md",
    "TOOLS.md",
    "USER.md",
    "HEARTBEAT.md",
    "IDENTITY.md",
    "BOOTSTRAP.md",
)

_WORKSPACE_DOC_FILES = (
    "IDENTITY.md",
    "SOUL.md",
    "USER.md",
    "AGENTS.md",
    "TOOLS.md",
    "HEARTBEAT.md",
    "BOOTSTRAP.md",
    "MEMORY.md",
)

_MAX_AGENT_PAYLOAD_BYTES = 256_000

# ── Active-agent API config (sourced from manifest + env) ────────────────────

OPENCLAW_API_URL = _AGENT.api_url
OPENCLAW_GATEWAY_TOKEN = _AGENT.api_token
OPENCLAW_MODEL = _AGENT.api_model

OPENCLAW_MODEL_CAPABILITIES: dict = dict(_AGENT.model_capabilities)
