"""
Miscellaneous status / listing endpoints.

Houses the grab-bag of small endpoints the frontend pings to check for
optional integrations (Ollama, plugins, MCP, connectors, channels) and to
populate empty UI lists (tools, skills, automations, active chats).

Most of these are stubs returning empty / disabled states. Backend-specific
endpoints (e.g. openclaw gateway / codex status) live in that agent's adapter
``routes.py`` and are mounted only when that agent is active — no endpoint
here names a backend.
"""

from fastapi import APIRouter

from services.cowork_agent.adapters.loader import try_load_capability

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

    Always returns ``{channels: {...}, gateway_running: bool}`` regardless of
    backend. The active agent's ``channels_status`` capability may expose a
    ``list_channels()`` that reads its connected-channels source; agents
    without one report ``{}`` — but the *shape* stays the same so FE consumers
    can do ``data.channels[id]`` without runtime guards.
    """
    mod = try_load_capability("channels_status")
    fn = getattr(mod, "list_channels", None) if mod else None
    if fn is None:
        return {"channels": {}, "gateway_running": False}
    return fn()


@router.get("/api/automations")
def list_automations():
    return []


@router.get("/api/plugins/status")
def plugins_status():
    return {}


@router.get("/api/ollama/status")
def ollama_status():
    return {"binary_installed": False, "running": False}
