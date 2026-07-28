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

import logging

from fastapi import APIRouter, Request

from services.cowork_agent.adapters.loader import try_load_capability

log = logging.getLogger(__name__)
router = APIRouter()


# ── Empty-list stubs ─────────────────────────────────────────────────────────


@router.get("/api/tools")
def list_tools(request: Request):
    """Aggregate tools across the user's connected Composio toolkits.

    Returns [] when Composio is not configured or the user has no active
    connections, preserving the previous stub's contract.
    """
    try:
        from services.composio import service as composio_service
        from services.composio.router import _resolve_user_id
    except Exception as exc:
        log.debug("tools: composio not importable: %s", exc)
        return []

    try:
        user_id = _resolve_user_id(request)
        active_toolkits = {
            (row.get("toolkit") or "").upper()
            for row in composio_service.list_connections(user_id)
            if row.get("status") == "ACTIVE"
        }
    except Exception as exc:
        log.debug("tools: list_connections failed: %s", exc)
        return []

    out: list[dict] = []
    for toolkit_id, meta in composio_service.TOOLKITS.items():
        if meta.slug not in active_toolkits:
            continue
        try:
            for tool in composio_service.list_tools(user_id, toolkit_id):
                out.append({
                    "toolkit": meta.slug,
                    "slug": tool.get("slug"),
                    "name": tool.get("name"),
                    "description": tool.get("description"),
                })
        except Exception as exc:
            log.debug("tools: list_tools(%s) failed: %s", meta.slug, exc)
    return out


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
