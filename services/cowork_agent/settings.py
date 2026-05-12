"""
Environment, paths, and constants for the cowork_agent subsystem.

The ``OPENCLAW_*`` module-level constants are anchored to the **openclaw
manifest specifically** (``get_agent("openclaw")``), NOT to the active
default agent. Every consumer of these constants is openclaw-specific
(the openclaw adapter, openclaw_store, the ``/api/config/openclaw``
endpoint, ...) and must always see openclaw paths regardless of
``DEFAULT_AGENT``. Same for ``HERMES_*``.

Code that needs the *active* agent's manifest must call
``get_default_agent()`` explicitly. Code that targets a specific backend
must call ``get_agent("<name>")``.

Background: previously these constants resolved against
``get_default_agent()``. With ``DEFAULT_AGENT=hermes`` that made
``OPENCLAW_JSON = ~/.hermes/config.yaml`` and ``AGENTS_DIR =
~/.hermes/profiles``, so an openclaw-shaped write (e.g.
``write_openclaw_config`` from the agent-create flow) silently
overwrote hermes's config. Re-anchoring closes that foot-gun.
"""

import re

from dotenv import load_dotenv

from services.cowork_agent.agent_registry import get_agent

load_dotenv()

_OPENCLAW = get_agent("openclaw")

# ── OpenClaw on-disk layout (sourced from openclaw manifest) ─────────────────

OPENCLAW_DIR = _OPENCLAW.home_dir
AGENTS_DIR = _OPENCLAW.agents_dir
OPENCLAW_JSON = _OPENCLAW.config_file
DEFAULT_OPENCLAW_WORKSPACE = _OPENCLAW.workspace_dir

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

# ── OpenClaw API config (sourced from openclaw manifest + env) ──────────────

OPENCLAW_API_URL = _OPENCLAW.api_url
OPENCLAW_GATEWAY_TOKEN = _OPENCLAW.api_token
OPENCLAW_MODEL = _OPENCLAW.api_model

OPENCLAW_MODEL_CAPABILITIES: dict = dict(_OPENCLAW.model_capabilities)

# ── Hermes on-disk layout + API config (sourced from hermes manifest + env) ──

_HERMES = get_agent("hermes")

HERMES_DIR = _HERMES.home_dir
HERMES_PROFILES_DIR = _HERMES.agents_dir
HERMES_CONFIG_FILE = _HERMES.config_file

HERMES_API_URL = _HERMES.api_url
HERMES_API_TOKEN = _HERMES.api_token
HERMES_MODEL = _HERMES.api_model
HERMES_SESSION_HEADER = (_HERMES.raw.get("api") or {}).get("session_header", "")

HERMES_MODEL_CAPABILITIES: dict = dict(_HERMES.model_capabilities)


# ── Adapter config loader ─────────────────────────────────────────────────────

import json as _json
import os as _os
from pathlib import Path as _Path

CLAUDE_COWORK_DIR: _Path = _Path(
    _os.environ.get("CLAUDE_COWORK_ROOT", str(_Path.home() / "claude-cowork"))
).expanduser()


def load_agent_config(agent_name: str) -> dict:
    """
    Load config/agents/{agent_name}/settings.json and resolve *_env keys.

    For every key ending in _env, reads os.environ.get(value) and adds the
    resolved value under the key with _env stripped.

    Example: "cli_path_env": "CLAUDE_CLI_PATH" → also sets "cli_path": <env value>.
    Raises FileNotFoundError if the settings file is absent.
    """
    settings_path = _Path(__file__).resolve().parents[2] / "config" / "agents" / agent_name / "settings.json"
    if not settings_path.exists():
        raise FileNotFoundError(
            f"No settings file for agent '{agent_name}': expected {settings_path}. "
            "Create config/agents/{agent_name}/settings.json."
        )
    config: dict = _json.loads(settings_path.read_text())
    resolved: dict = {}
    for key, value in config.items():
        if key.endswith("_env") and isinstance(value, str):
            resolved_key = key[: -len("_env")]
            resolved[resolved_key] = _os.environ.get(value)
    config.update(resolved)
    return config
