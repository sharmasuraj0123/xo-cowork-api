"""
Environment, paths, and constants for the cowork_agent subsystem.

Migrated from bridge/config.py. `CORS_ORIGINS` lives in server.py instead,
and `active_streams` lives in chat_state.py.
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── OpenClaw on-disk layout ──────────────────────────────────────────────────

OPENCLAW_DIR = Path.home() / ".openclaw"
AGENTS_DIR = OPENCLAW_DIR / "agents"
OPENCLAW_JSON = OPENCLAW_DIR / "openclaw.json"
DEFAULT_OPENCLAW_WORKSPACE = OPENCLAW_DIR / "workspace"

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

# ── OpenClaw API config ──────────────────────────────────────────────────────

OPENCLAW_API_URL = os.getenv("OPENCLAW_API_URL", "http://127.0.0.1:18789/v1/chat/completions")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "xo-cowork")
OPENCLAW_MODEL = os.getenv("OPENCLAW_MODEL", "openclaw/default")

OPENCLAW_MODEL_CAPABILITIES: dict = {
    "function_calling": True,
    "vision": False,
    "reasoning": True,
    "json_output": True,
    "max_context": 200000,
    "max_output": 16384,
}
