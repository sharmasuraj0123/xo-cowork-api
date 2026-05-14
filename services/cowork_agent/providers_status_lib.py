"""
Shared building blocks for `/providers/status`.

Three things live here so the per-agent adapters can stay tiny:

1. ``_read_xo_models`` — pulls the current ``models`` section out of xo.json
   (with a DEFAULTS fallback when the file is missing). The endpoint reports
   only on providers the manifest currently marks ``enabled: true``.

2. OAuth probes — ``claude_oauth_connected`` runs ``claude auth status
   --json``; ``codex_oauth_connected`` checks ``$CODEX_HOME/auth.json``.
   Both are agent-independent because they query CLI-local state. Both are
   best-effort: any failure (binary missing, timeout, parse error) is
   treated as ``connected: false`` since this endpoint drives a frontend
   tile, not an operational alert — richer error paths already exist on
   ``/models/status``.

3. ``build_providers_status`` — composes the response. Adapters pass two
   callables that report whether each API key is present in *their* env
   source (openclaw `.env`, hermes `.env`, claude_code `os.environ`).
   Disabled providers are omitted from the response entirely; the frontend
   already drives section visibility off xo.json's `enabled` flags.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from services.cowork_agent.project_layout import xo_projects_root
from services.xo_manifest import build_static_manifest

CLAUDE_BIN_ENV = "CLAUDE_CLI_PATH"
DEFAULT_CLAUDE_BIN = "claude"
DEFAULT_TIMEOUT_SECONDS = 10.0


# ── xo.json ──────────────────────────────────────────────────────────────────


def _read_xo_models(agent: str) -> dict[str, Any]:
    """Return xo.json's ``models`` section; fall back to DEFAULTS for `agent`.

    The cascade rule (parent ``enabled=false`` → children false) is already
    applied to whatever is on disk, so callers can trust leaf flags directly.
    """
    try:
        path = xo_projects_root() / ".xo" / "xo.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("models"), dict):
                return data["models"]
    except Exception:
        # Manifest unreadable — fall through to defaults so the endpoint
        # still returns *something* useful for the frontend.
        pass
    return build_static_manifest(agent).get("models", {})


def _leaf_enabled(node: Any, *keys: str) -> bool:
    """Walk `node` via `keys`; return True iff the final dict has ``enabled: true``."""
    cur: Any = node
    for k in keys:
        if not isinstance(cur, dict):
            return False
        cur = cur.get(k)
    return bool(isinstance(cur, dict) and cur.get("enabled"))


# ── .env parsing ─────────────────────────────────────────────────────────────


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal ``KEY=value`` parser. Returns ``{}`` if the file is absent or unreadable.

    Mirrors hermes' gateway_pool helper in spirit — we re-implement instead of
    importing to keep the dependency arrows pointing one way.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


# ── OAuth probes (agent-independent) ─────────────────────────────────────────


def codex_oauth_connected() -> bool:
    """True iff ``$CODEX_HOME/auth.json`` (or ``~/.codex/auth.json``) exists."""
    home = (os.getenv("CODEX_HOME", "") or "").strip() or str(Path.home() / ".codex")
    return (Path(home) / "auth.json").is_file()


async def claude_oauth_connected(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> bool:
    """Run ``claude auth status --json`` and return its ``loggedIn`` flag.

    Any failure path → False, on purpose. The frontend tile only cares
    whether the user is authenticated *right now*; if the binary is missing
    or the JSON is malformed we treat that as "not connected" rather than
    bubbling an error.
    """
    binary = (os.getenv(CLAUDE_BIN_ENV, "") or "").strip() \
        or shutil.which(DEFAULT_CLAUDE_BIN) \
        or DEFAULT_CLAUDE_BIN
    if os.path.isabs(binary) and not os.path.isfile(binary):
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "auth", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError):
        return False
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        return False
    if proc.returncode != 0:
        return False
    try:
        payload = json.loads((stdout or b"").decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return False
    return bool(isinstance(payload, dict) and payload.get("loggedIn"))


# ── Composer ─────────────────────────────────────────────────────────────────


async def build_providers_status(
    agent: str,
    *,
    anthropic_key_present: Callable[[], bool],
    openai_key_present: Callable[[], bool],
) -> dict[str, Any]:
    """Compose the ``/providers/status`` response for `agent`.

    `anthropic_key_present` / `openai_key_present` are callables so each
    adapter can resolve the key from its own env source without this module
    knowing where that source lives.
    """
    models = _read_xo_models(agent)

    oauth: dict[str, dict] = {}
    if _leaf_enabled(models, "oauth", "claude_code"):
        oauth["claude_code"] = {"connected": await claude_oauth_connected()}
    if _leaf_enabled(models, "oauth", "codex"):
        oauth["codex"] = {"connected": codex_oauth_connected()}

    api_keys: dict[str, dict] = {}
    if _leaf_enabled(models, "api_keys", "anthropic"):
        api_keys["anthropic"] = {"connected": bool(anthropic_key_present())}
    if _leaf_enabled(models, "api_keys", "openai"):
        api_keys["openai"] = {"connected": bool(openai_key_present())}

    return {"agent": agent, "oauth": oauth, "api_keys": api_keys}


__all__ = [
    "build_providers_status",
    "claude_oauth_connected",
    "codex_oauth_connected",
    "parse_env_file",
]
