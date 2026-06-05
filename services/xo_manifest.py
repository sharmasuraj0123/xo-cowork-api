"""
xo.json — agent capability + live-status manifest.

The xo-cowork frontend reads ~/xo-projects/.xo/xo.json to decide which UI
sections (Models / Data / Channels / Secrets) and which sub-items inside
each (Slack, Telegram, Google Drive, OAuth providers, API-key providers)
to render.

Two layers live in the file:

1. Static capability flags — hand-tuned per agent in `DEFAULTS`. Written
   once at server startup, overwriting any prior file.
2. Live status (openclaw only, for now) — the full response from
   `/openclaw/models/status` and `/openclaw/channels/status`, embedded
   under a `status` key in the corresponding section. Refreshed in a
   background task at startup and on every endpoint hit. Deferred for
   claude_code and hermes until they have their own status sources.

The defaults table is the source of truth; cascade rule (parent
`enabled=false` → children false) is applied at build time so future
toggle changes propagate without hand-editing leaves.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Any

from services.cowork_agent.project_layout import xo_projects_root




def _all_data_true() -> dict:
    return {
        "enabled": True,
        "google_drive": {"enabled": True},
        "one_drive":    {"enabled": True},
        "github":       {"enabled": True},
        "vercel":       {"enabled": True},
    }


def _channels(enabled: bool, telegram: bool, slack: bool) -> dict:
    return {
        "enabled": enabled,
        "telegram": {"enabled": telegram},
        "slack":    {"enabled": slack},
    }


DEFAULTS: dict[str, dict] = {
    "openclaw": {
        "models": {
            "enabled": True,
            "oauth": {
                "claude_code": {"enabled": False},
                "codex":       {"enabled": True},
            },
            "api_keys": {
                "enabled":    True,
                "anthropic":  {"enabled": True},
                "openai":     {"enabled": True},
                "openrouter": {"enabled": True},
            },
        },
        "data": _all_data_true(),
        "channels": _channels(enabled=True, telegram=True, slack=True),
        "secrets": {"enabled": True},
    },
    "claude_code": {
        "models": {
            "enabled": True,
            "oauth": {
                "claude_code": {"enabled": True},
                "codex":       {"enabled": False},
            },
            "api_keys": {
                "enabled":    True,
                "anthropic":  {"enabled": True},
                "openai":     {"enabled": False},
                "openrouter": {"enabled": True},
            },
        },
        "data": _all_data_true(),
        # channels.enabled is false; the cascade rule zeros out the children.
        "channels": _channels(enabled=False, telegram=True, slack=True),
        "secrets": {"enabled": True},
    },
    "hermes": {
        "models": {
            "enabled": True,
            "oauth": {
                "claude_code": {"enabled": False},
                "codex":       {"enabled": False},
            },
            "api_keys": {
                "enabled":    True,
                "anthropic":  {"enabled": True},
                "openai":     {"enabled": True},
                "openrouter": {"enabled": True},
            },
        },
        "data": _all_data_true(),
        "channels": _channels(enabled=True, telegram=True, slack=True),
        "secrets": {"enabled": True},
    },
}


_LOCK = asyncio.Lock()


def _xo_path() -> Path:
    return xo_projects_root() / ".xo" / "xo.json"


def _force_false_children(node: Any) -> None:
    """Recursively set every `enabled` flag inside `node` to False."""
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key == "enabled":
            node[key] = False
        elif isinstance(value, dict):
            _force_false_children(value)


def _apply_cascade(manifest: dict) -> dict:
    """If a top-level section's `enabled` is False, force every child
    `enabled` inside that section to False as well."""
    for section_name, section in manifest.items():
        if not isinstance(section, dict):
            continue
        if section.get("enabled") is False:
            for child_key, child_val in section.items():
                if child_key == "enabled":
                    continue
                _force_false_children(child_val)
    return manifest


def build_static_manifest(agent: str) -> dict:
    """Return a fresh manifest dict for `agent` with cascade applied and
    the `agent` field set. Caller owns the returned dict."""
    if agent not in DEFAULTS:
        raise KeyError(f"unknown agent '{agent}' (valid: {', '.join(sorted(DEFAULTS))})")
    manifest: dict[str, Any] = {"agent": agent}
    manifest.update(copy.deepcopy(DEFAULTS[agent]))
    return _apply_cascade(manifest)


def resolve_agent_name() -> str:
    """The active agent name — the single resolver used across core code.

    ``AGENT_NAME`` (if set) wins and is returned verbatim, even if it names no
    manifest (callers map that to a 501). When unset, the default is resolved
    by the registry (``DEFAULT_AGENT`` env → single-manifest → documented
    ``openclaw`` fallback). The agent-name default literal lives only there —
    no core file hardcodes an agent name.
    """
    explicit = (os.getenv("AGENT_NAME", "") or "").strip()
    if explicit:
        return explicit
    from services.cowork_agent.agent_registry import get_active_agent
    return get_active_agent().name


async def _write_atomic(manifest: dict) -> Path:
    """Write `manifest` to xo.json via temp-file + rename. Caller holds the lock."""
    path = _xo_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


async def write_static_manifest() -> None:
    """Resolve AGENT_NAME, build the static manifest, write to disk.

    Non-fatal on every failure: logs and returns. Unknown agent value
    means we deliberately do NOT touch any existing file.
    """
    agent = resolve_agent_name()
    if agent not in DEFAULTS:
        print(
            f"⚠️ xo.json: AGENT_NAME='{agent}' is not a known agent "
            f"(valid: {', '.join(sorted(DEFAULTS))}) — skipping"
        )
        return

    try:
        manifest = build_static_manifest(agent)
        async with _LOCK:
            path = await _write_atomic(manifest)
        print(f"✅ xo.json: wrote static manifest for agent={agent} at {path}")
    except Exception as exc:
        print(f"⚠️ xo.json: write failed (non-fatal): {exc}")


async def patch_status(section: str, status_payload: dict) -> None:
    """Read xo.json, set `<section>.status = status_payload`, write atomically.

    No-op (with a log) when the file doesn't exist yet, when the section
    isn't a dict, or when serialisation fails. The endpoint hooks fire
    this as a background task, so failures must never propagate.
    """
    try:
        path = _xo_path()
        async with _LOCK:
            if not path.exists():
                print(f"⚠️ xo.json: patch_status({section}) skipped — file not present yet")
                return
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"⚠️ xo.json: patch_status({section}) read failed: {exc}")
                return
            if not isinstance(manifest, dict) or not isinstance(manifest.get(section), dict):
                print(f"⚠️ xo.json: patch_status({section}) skipped — section missing")
                return
            manifest[section]["status"] = status_payload
            await _write_atomic(manifest)
    except Exception as exc:
        print(f"⚠️ xo.json: patch_status({section}) failed (non-fatal): {exc}")


def _resolve_status_fetchers(agent: str):
    """Return (fetch_models, fetch_channels) for the active agent, or
    (None, None) if the agent has no live-status sources yet.

    Imports lazily so a missing adapter module (e.g. early dev state) never
    breaks the import graph for callers that don't need it.
    """
    if agent == "openclaw":
        from services.cowork_agent.adapters.openclaw.models_status import get_models_status
        from services.cowork_agent.adapters.openclaw.channels_status import get_channels_status
        return get_models_status, get_channels_status
    if agent == "hermes":
        from services.cowork_agent.adapters.hermes.models_status import get_models_status
        from services.cowork_agent.adapters.hermes.channels_status import get_channels_status
        return get_models_status, get_channels_status
    if agent == "claude_code":
        from services.cowork_agent.adapters.claude_code.models_status import get_models_status
        from services.cowork_agent.adapters.claude_code.channels_status import get_channels_status
        return get_models_status, get_channels_status
    # Any future name without a status source falls through to a no-op.
    return None, None


async def seed_agent_status() -> None:
    """Background task: fetch the current agent's models + channels status in
    parallel and patch them into xo.json. Logs and continues on per-section
    failures so one bad fetch doesn't poison the other.

    No-op (with a log line) for agents without a status source (e.g.
    claude_code today). Driven by the current AGENT_NAME env var so a
    restart with a different agent automatically routes to the right
    adapter.
    """
    agent = resolve_agent_name()
    fetch_models, fetch_channels = _resolve_status_fetchers(agent)
    if fetch_models is None or fetch_channels is None:
        print(f"   xo.json: no live-status source for agent={agent} — skipping seed")
        return

    async def _seed(section: str, fetch) -> None:
        print(f"   xo.json: {section} status seed → fetching…")
        try:
            result = await fetch()
        except Exception as exc:
            print(f"⚠️ xo.json: {section} status seed failed: {exc}")
            return
        await patch_status(section, result)
        print(f"✅ xo.json: {section} status seeded")

    await asyncio.gather(
        _seed("models", fetch_models),
        _seed("channels", fetch_channels),
        return_exceptions=True,
    )


# Backwards-compat alias for the earlier openclaw-only name. Safe to remove
# once no callers reference it.
seed_openclaw_status = seed_agent_status
