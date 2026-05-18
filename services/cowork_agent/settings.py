"""
Generic environment, paths, and constants for the cowork_agent subsystem.

Cross-runtime helpers live here: agent-id regex validators, the workspace
doc-file tuples, ``CLAUDE_COWORK_DIR``, ``load_agent_config``. Each runtime
adapter keeps its own backend-specific constants in its own settings module
(see ``services/cowork_agent/adapters/openclaw/settings.py``).

The ``HERMES_*`` module-level constants are anchored to the **hermes manifest
specifically** (``get_agent("hermes")``), NOT to the active agent. Every
consumer of these constants is hermes-specific and must always see hermes
paths regardless of ``AGENT_NAME`` / ``DEFAULT_AGENT``. (These stay in this
file pending hermes's own modularization.)

Code that needs the *active* agent's manifest must call
``get_active_agent()`` explicitly. Code that targets a specific backend
must call ``get_agent("<name>")``.
"""

import re

from dotenv import load_dotenv

from services.cowork_agent.agent_registry import get_agent

load_dotenv()

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
