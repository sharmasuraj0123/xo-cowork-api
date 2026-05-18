"""
Miscellaneous status / listing endpoints.

Houses the grab-bag of small endpoints the frontend pings to check for
optional integrations (Ollama, Codex, plugins, MCP, connectors, channels)
and to populate empty UI lists (tools, skills, automations, active chats).

Most of these are stubs returning empty / disabled states. A few
(`/api/channels/openclaw/status`, `/api/codex/status`) do real work: probing
the OpenClaw gateway or scanning auth profiles.
"""

import json
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter

from services.cowork_agent.adapters.openclaw.settings import OPENCLAW_API_URL, OPENCLAW_JSON

router = APIRouter()


# ── Empty-list stubs ─────────────────────────────────────────────────────────


@router.get("/api/tools")
def list_tools():
    return []


@router.get("/api/skills")
def list_skills():
    return []


@router.get("/api/chat/active")
def chat_active():
    return []


@router.get("/api/mcp/status")
def mcp_status():
    return []


@router.get("/api/connectors")
def list_connectors():
    return []


@router.get("/api/channels")
def list_channels():
    """Return the list of messaging channels with their connection state.

    Always returns ``{channels: {...}, gateway_running: bool}`` regardless
    of backend. Openclaw has no on-disk equivalent of hermes's
    ``~/.hermes/gateway_state.json`` to read connected channels from, so
    it just reports ``{}`` — but the *shape* stays the same so FE
    consumers can do ``data.channels[id]`` without runtime guards.
    """
    import os
    if os.getenv("AGENT_NAME", "openclaw") != "hermes":
        return {"channels": {}, "gateway_running": False}

    from services.cowork_agent.agent_registry import get_agent
    state_file = get_agent("hermes").home_dir / "gateway_state.json"
    if not state_file.is_file():
        return {"channels": {}, "gateway_running": False}

    try:
        state = json.loads(state_file.read_text())
    except Exception:
        return {"channels": {}, "gateway_running": False}

    platforms = state.get("platforms") or {}
    channels: dict[str, dict] = {}
    for platform_id, info in platforms.items():
        if not isinstance(info, dict):
            continue
        # The gateway lists api_server too — that's the hermes API itself,
        # not a user-facing messaging channel. Hide it from the UI list.
        if platform_id == "api_server":
            continue
        channels[platform_id] = {
            "id": platform_id,
            "name": platform_id,
            "type": platform_id,
            "status": info.get("state") or "unknown",
            "account": info.get("error_message") or None,
        }

    return {
        "channels": channels,
        "gateway_running": (state.get("gateway_state") == "running"),
    }


@router.get("/api/automations")
def list_automations():
    return []


@router.get("/api/plugins/status")
def plugins_status():
    return {}


@router.get("/api/ollama/status")
def ollama_status():
    return {"binary_installed": False, "running": False}


# ── Active integration probes ────────────────────────────────────────────────


@router.get("/api/channels/openclaw/status")
def openclaw_status():
    """Check if OpenClaw gateway is reachable."""
    parsed = urlparse(OPENCLAW_API_URL)
    port = parsed.port or 18789
    try:
        resp = httpx.get(OPENCLAW_API_URL, timeout=3.0)
        running = resp.status_code in (200, 405)
    except Exception:
        running = False
    return {
        "installed": True,
        "running": running,
        "port": port if running else None,
        "ws_url": None,
    }


@router.get("/api/codex/status")
def codex_status():
    """List all Codex OAuth accounts found in openclaw.json and the main agent's auth-profiles.json."""
    accounts: list[dict] = []
    seen_emails: set[str] = set()

    def _collect(profiles_obj):
        if not isinstance(profiles_obj, dict):
            return
        for pid, prof in profiles_obj.items():
            if not isinstance(prof, dict):
                continue
            if prof.get("provider") != "openai-codex":
                continue
            email = prof.get("email") or pid
            if email in seen_emails:
                continue
            seen_emails.add(email)
            accounts.append({
                "id": pid,
                "email": email,
                "expires": prof.get("expires"),
            })

    try:
        config = json.loads(OPENCLAW_JSON.read_text(encoding="utf-8"))
        _collect(config.get("auth", {}).get("profiles", {}))
    except (OSError, json.JSONDecodeError):
        pass

    try:
        agent_auth_path = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
        agent_auth = json.loads(agent_auth_path.read_text(encoding="utf-8"))
        _collect(agent_auth.get("profiles", {}))
    except (OSError, json.JSONDecodeError):
        pass

    first_email = accounts[0]["email"] if accounts else ""
    return {
        "is_connected": bool(accounts),
        "email": first_email,
        "accounts": accounts,
    }
