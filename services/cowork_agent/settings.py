"""
Environment, paths, and constants for the cowork_agent subsystem.

The agent-specific path/API constants live in their adapters
(``adapters/openclaw/paths.py`` and ``adapters/hermes/paths.py``) and are
re-exported here for backward compatibility, so no core file names a backend
to build them. Each is anchored to its own manifest specifically (NOT the
active agent): every consumer is backend-specific and must always see that
backend's paths regardless of ``AGENT_NAME`` (e.g. an openclaw-shaped write
must never land in ``~/.hermes`` when ``AGENT_NAME=hermes``).

Code that needs the *active* agent's manifest must call ``get_active_agent()``
explicitly.
"""

import re

from dotenv import load_dotenv

# Load .env BEFORE the adapter paths import below: those modules resolve each
# manifest's API config (url/token/model) from the environment at import time,
# so the .env values must already be present.
load_dotenv()

# Re-exported from the adapters that own these names (where the backend is
# resolved). Imported eagerly so ``from ...settings import AGENTS_DIR`` etc.
# keep working unchanged.
from services.cowork_agent.adapters.openclaw.paths import (  # noqa: E402,F401
    AGENTS_DIR,
    DEFAULT_OPENCLAW_WORKSPACE,
    OPENCLAW_API_URL,
    OPENCLAW_DIR,
    OPENCLAW_GATEWAY_TOKEN,
    OPENCLAW_JSON,
    OPENCLAW_MODEL,
    OPENCLAW_MODEL_CAPABILITIES,
)
from services.cowork_agent.adapters.hermes.paths import (  # noqa: E402,F401
    HERMES_API_TOKEN,
    HERMES_API_URL,
    HERMES_CONFIG_FILE,
    HERMES_DIR,
    HERMES_MODEL,
    HERMES_MODEL_CAPABILITIES,
    HERMES_PROFILES_DIR,
    HERMES_SESSION_HEADER,
)

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
