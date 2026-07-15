"""
Antigravity (agy) model listing (`/api/models`).

Lists the models `agy models` reports, one row per model in the common
``{id, name, provider, ...}`` shape used by openclaw/hermes. The model *ids* are
slugified (``antigravity/gemini-3-5-flash-low``); the human name is preserved as
``name`` and IS what agy's ``--model`` flag expects. Status is login-derived (agy
has no per-model probe) — same source of truth as ``models_status``.

Falls back to the manifest ``models.catalog`` if the CLI isn't callable, so the
list is stable even when ``agy`` is momentarily unavailable.
"""
from __future__ import annotations

import re
import subprocess

from services.cowork_agent.adapters.antigravity.auth import has_usable_login
from services.cowork_agent.registry.agent_registry import get_agent

_MODEL_PREFIX = "antigravity"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{_MODEL_PREFIX}/{s}"


def _provider_for(name: str) -> str:
    low = name.lower()
    if "gemini" in low:
        return "google"
    if "claude" in low:
        return "anthropic"
    if "gpt" in low or "oss" in low:
        return "open"
    return "google"


def _catalog() -> list[str]:
    """Model names from ``agy models``; fall back to the manifest catalog."""
    cli = "agy"
    try:
        out = subprocess.run(
            [cli, "models"], capture_output=True, text=True, timeout=15
        )
        if out.returncode == 0 and out.stdout.strip():
            names = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
            if names:
                return names
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return list(get_agent(_MODEL_PREFIX).raw.get("models", {}).get("catalog", []))
    except Exception:
        return []


def list_models() -> list[dict]:
    status = "ok" if has_usable_login() else "error"
    rows: list[dict] = []
    for name in _catalog():
        rows.append({
            "id": _slug(name),
            "name": name,
            "provider": _provider_for(name),
            "status": status,
        })
    return rows


__all__ = ["list_models"]
