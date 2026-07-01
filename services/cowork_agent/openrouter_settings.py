"""Read/write the OpenRouter ``env`` block in Claude Code's ``settings.json``.

Pointing the ``claude`` CLI at OpenRouter is just a matter of setting a few env
vars in its own settings file (``~/.claude/settings.json`` by default), which the
CLI reads natively on every run. This module owns that edit and is deliberately
**pure stdlib** (``json`` / ``os`` / ``pathlib``) so it can be imported both by the
FastAPI server (the API Keys "Save" flow) and by the venv-less
``scripts/openrouter.py`` CLI.

Only the OpenRouter-related keys inside the ``env`` block are managed; every other
setting (``theme``, ``model``, ``effortLevel``, unrelated env vars, …) is preserved.
This is provider/CLI configuration — it names no agent, so it is safe to call from
core routes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS = "~/.claude/settings.json"
DEFAULT_BASE_URL = "https://openrouter.ai/api"          # no /v1; the CLI appends it
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"

BASE_URL_KEY = "ANTHROPIC_BASE_URL"
AUTH_TOKEN_KEY = "ANTHROPIC_AUTH_TOKEN"
API_KEY_KEY = "ANTHROPIC_API_KEY"
# Claude Code requests these tiers internally; mapping one chosen model onto all
# of them lets a single choice fully substitute. A per-tier override still wins.
TIER_KEYS = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "subagent": "CLAUDE_CODE_SUBAGENT_MODEL",
}
MANAGED_KEYS = [BASE_URL_KEY, AUTH_TOKEN_KEY, API_KEY_KEY, *TIER_KEYS.values()]


def _path(settings_path: str | os.PathLike) -> Path:
    return Path(os.path.expanduser(str(settings_path)))


def _load(path: Path) -> dict:
    """Return the parsed settings object, or ``{}`` if absent/empty.

    Raises ``json.JSONDecodeError`` on malformed JSON and ``ValueError`` if the
    top level is not an object — callers decide how to surface that.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _env_block(settings: dict) -> dict:
    env = settings.get("env")
    return env if isinstance(env, dict) else {}


def write_openrouter_settings(
    api_key: str,
    *,
    model: str | None = None,
    per_tier: dict[str, str | None] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    settings_path: str | os.PathLike = DEFAULT_SETTINGS,
) -> dict[str, Any]:
    """Merge the OpenRouter ``env`` block into settings.json, preserving all other
    keys, and return :func:`read_openrouter_state`."""
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("api_key is required")
    default_model = (model or "").strip() or DEFAULT_MODEL
    per_tier = per_tier or {}

    path = _path(settings_path)
    settings = _load(path)
    env = _env_block(settings)

    env[BASE_URL_KEY] = base_url
    env[AUTH_TOKEN_KEY] = api_key
    env[API_KEY_KEY] = ""  # must be present-but-empty so the CLI uses the bearer token
    for label, key_name in TIER_KEYS.items():
        override = (per_tier.get(label) or "").strip()
        env[key_name] = override or default_model

    settings["env"] = env
    _dump(path, settings)
    return read_openrouter_state(settings_path=settings_path)


def clear_openrouter_settings(*, settings_path: str | os.PathLike = DEFAULT_SETTINGS) -> dict[str, Any]:
    """Remove the OpenRouter-managed keys — revert Claude Code to native Anthropic.
    Leaves every other setting and env var intact."""
    path = _path(settings_path)
    if not path.exists():
        return read_openrouter_state(settings_path=settings_path)
    settings = _load(path)
    env = _env_block(settings)
    for k in MANAGED_KEYS:
        env.pop(k, None)
    if env:
        settings["env"] = env
    else:
        settings.pop("env", None)  # tidy up an empty env block
    _dump(path, settings)
    return read_openrouter_state(settings_path=settings_path)


def read_openrouter_state(*, settings_path: str | os.PathLike = DEFAULT_SETTINGS) -> dict[str, Any]:
    """Best-effort view of whether Claude Code is pointed at OpenRouter.

    Never raises — a missing/malformed file reads as not-connected, since this
    drives a status tile, not an operation."""
    try:
        settings = _load(_path(settings_path))
    except (json.JSONDecodeError, ValueError, OSError):
        settings = {}
    env = _env_block(settings)
    connected = (env.get(BASE_URL_KEY) or "").strip() == DEFAULT_BASE_URL and bool((env.get(AUTH_TOKEN_KEY) or "").strip())
    model = (env.get(TIER_KEYS["sonnet"]) or "").strip() or None
    return {
        "provider": "openrouter" if connected else "anthropic",
        "connected": connected,
        "model": model if connected else None,
    }


def current_api_key(*, settings_path: str | os.PathLike = DEFAULT_SETTINGS) -> str | None:
    """The OpenRouter key currently stored in settings.json (so a model-only
    switch can reuse it), or ``None``."""
    try:
        settings = _load(_path(settings_path))
    except (json.JSONDecodeError, ValueError, OSError):
        return None
    return (_env_block(settings).get(AUTH_TOKEN_KEY) or "").strip() or None


def mask(token: str) -> str:
    token = (token or "").strip()
    if len(token) <= 10:
        return "set" if token else "(none)"
    return f"{token[:8]}…{token[-4:]}"


__all__ = [
    "DEFAULT_SETTINGS",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "TIER_KEYS",
    "MANAGED_KEYS",
    "AUTH_TOKEN_KEY",
    "write_openrouter_settings",
    "clear_openrouter_settings",
    "read_openrouter_state",
    "current_api_key",
    "mask",
]
