"""
Environment, paths, and constants for the cowork_agent subsystem.

This module holds only **agent-agnostic** config. Each backend's own
path/API constants live in its adapter (``adapters/<name>/paths.py``),
anchored to that backend's manifest; core never builds or re-exports them, so
no core file names a backend. Code that needs the *active* agent's manifest
calls ``get_active_agent()`` explicitly.
"""

import re

from dotenv import load_dotenv

# Load .env so consumers (and each adapter's paths module) see configured
# values at import time.
load_dotenv()

# Agent-specific path/API constants are NOT defined or re-exported here. Each
# adapter owns its own under adapters/<name>/paths.py; routing them through core
# would re-introduce the agent coupling this layout removes. settings.py holds
# only the agent-agnostic config below.

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
    settings_path = _Path(__file__).resolve().parents[3] / "config" / "agents" / agent_name / "settings.json"
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
