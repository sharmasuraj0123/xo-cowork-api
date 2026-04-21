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

from services.cowork_agent.settings import OPENCLAW_API_URL, OPENCLAW_JSON

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
    return []


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
