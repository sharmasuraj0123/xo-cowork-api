"""
REST routes for Composio-backed integrations.

Composio is the single source of truth — this router is a thin proxy. See
services/composio_service.py and docs/composio-xo-swarm-api-migration.md.

Endpoints (all under /api/connectors/composio):
  GET    /toolkits                       — catalog + per-user connection status
  POST   /{toolkit}/connect              — start OAuth flow or submit an API key
  GET    /{toolkit}/status               — poll a pending connection_request
  POST   /{toolkit}/disconnect           — revoke a connection
  GET    /{toolkit}/tools                — list actions available in a toolkit
                                          (includes disabled rows + category tag)
  GET    /{toolkit}/prefs                — per-action enable/disable map
  PUT    /{toolkit}/prefs                — toggle one or more actions
                                          (404 for toolkits not yet supported)
  POST   /execute                        — direct (non-chat) action invocation
  GET    /mcp-url                        — per-user hosted MCP URL (used by adapters)
  GET    /callback                       — OAuth callback (HTML, postMessages opener)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from services import composio_service

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_user_id(request: Request, body_user_id: Optional[str] = None) -> str:
    """Pull user_id from (in order): request body, request.state.user_id (set by
    middleware in future), auth_state["user_id"] from routers/auth.py, then a
    "default_user" sentinel for single-tenant local dev."""
    if body_user_id:
        return body_user_id
    state_user = getattr(request.state, "user_id", None)
    if state_user:
        return str(state_user)
    try:
        from routers.auth import auth_state  # local import avoids circular
        uid = auth_state.get("user_id")
        if uid:
            return str(uid)
    except Exception:
        pass
    return "default_user"


def _toolkit_status_map(user_id: str) -> dict[str, dict[str, Any]]:
    """{toolkit_slug_upper: {status, connected_account_id, scheme}} for fast lookups."""
    by_slug: dict[str, dict[str, Any]] = {}
    for row in composio_service.list_connections(user_id):
        slug = (row.get("toolkit") or "").upper()
        if not slug:
            continue
        # Prefer the most-recent ACTIVE row per toolkit.
        prev = by_slug.get(slug)
        if prev and prev.get("status") == "ACTIVE" and row.get("status") != "ACTIVE":
            continue
        by_slug[slug] = row
    return by_slug


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class ConnectBody(BaseModel):
    auth_scheme: str = "OAUTH2"
    api_key: Optional[str] = None
    redirect_uri: Optional[str] = None
    user_id: Optional[str] = None


class DisconnectBody(BaseModel):
    connected_account_id: str
    user_id: Optional[str] = None


class ExecuteBody(BaseModel):
    toolkit: str
    tool_slug: str
    arguments: dict[str, Any] = {}
    user_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Catalog + status
# ---------------------------------------------------------------------------

@router.get("/api/connectors/composio/toolkits")
async def list_toolkits(request: Request) -> JSONResponse:
    user_id = _resolve_user_id(request)
    status_by_slug = _toolkit_status_map(user_id)

    toolkits: list[dict[str, Any]] = []
    for toolkit_id, meta in composio_service.TOOLKITS.items():
        connection = status_by_slug.get(meta.slug)
        toolkits.append({
            "id": toolkit_id,
            "slug": meta.slug,
            "display_name": meta.display_name,
            "schemes": list(meta.schemes),
            "status": (connection or {}).get("status", "NEEDS_AUTH"),
            "connected_account_id": (connection or {}).get("connected_account_id"),
            "scheme": (connection or {}).get("scheme"),
        })
    return JSONResponse({"toolkits": toolkits})


# ---------------------------------------------------------------------------
# Connect / status / disconnect
# ---------------------------------------------------------------------------

@router.post("/api/connectors/composio/{toolkit}/connect")
async def connect(toolkit: str, body: ConnectBody, request: Request) -> JSONResponse:
    user_id = _resolve_user_id(request, body.user_id)
    try:
        result = composio_service.initiate_connection(
            user_id=user_id,
            toolkit_id=toolkit,
            auth_scheme=body.auth_scheme,
            api_key=body.api_key,
            redirect_uri=body.redirect_uri,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return JSONResponse(result)


@router.get("/api/connectors/composio/{toolkit}/status")
async def connect_status(
    toolkit: str,
    connection_request_id: str = Query(...),
) -> JSONResponse:
    result = composio_service.check_connection(connection_request_id)
    return JSONResponse(result)


@router.post("/api/connectors/composio/{toolkit}/disconnect")
async def disconnect(toolkit: str, body: DisconnectBody, request: Request) -> JSONResponse:
    user_id = _resolve_user_id(request, body.user_id)
    ok = composio_service.disconnect(body.connected_account_id)
    if not ok:
        raise HTTPException(status_code=502, detail="Composio disconnect failed.")
    # Best-effort: re-fetch authoritative status from Composio for the response.
    rows = composio_service.list_connections(user_id)
    still_connected = any(
        r.get("connected_account_id") == body.connected_account_id and r.get("status") == "ACTIVE"
        for r in rows
    )
    return JSONResponse({"status": "needs_auth" if not still_connected else "connected"})


# ---------------------------------------------------------------------------
# Tools listing + direct execution
# ---------------------------------------------------------------------------

@router.get("/api/connectors/composio/{toolkit}/tools")
async def list_toolkit_tools(toolkit: str, request: Request) -> JSONResponse:
    """Full action catalogue for a toolkit — including currently disabled
    actions, each carrying `enabled` and (where supported) `category`.

    `include_disabled=True` because this endpoint feeds the Connectors UI's
    toggle list — the UI needs to render the OFF rows, not just the ON ones.
    The agent path (`composio_list_tools` meta-tool) keeps the default
    `include_disabled=False` so disabled actions never enter the prompt.
    """
    user_id = _resolve_user_id(request)
    try:
        tools = composio_service.list_tools(user_id, toolkit, include_disabled=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return JSONResponse({"tools": tools})


# ---------------------------------------------------------------------------
# Per-action enable/disable prefs (UI: Connectors → Composio → tile expand)
# ---------------------------------------------------------------------------
#
# v1: only googlecalendar accepts mutations (PUT). All toolkits accept reads
# (GET) and return an empty map by default — keeps the UI's fetch logic free
# of toolkit-specific branches.

_PREFS_WRITABLE_TOOLKITS = frozenset({"googlecalendar"})


class PrefsBody(BaseModel):
    actions: dict[str, bool]


@router.get("/api/connectors/composio/{toolkit}/prefs")
async def get_toolkit_prefs(toolkit: str, request: Request) -> JSONResponse:
    """Return the disabled-slug map for one toolkit. Absent slugs are enabled
    by default; the response shape mirrors the on-disk store.
    """
    # Local import: keep router lazy-loaded, mirrors composio_service style.
    from services import composio_action_prefs  # noqa: PLC0415
    user_id = _resolve_user_id(request)
    del user_id  # prefs are install-wide today; user_id reserved for future
    return JSONResponse({"actions": composio_action_prefs.get_toolkit_prefs(toolkit)})


@router.put("/api/connectors/composio/{toolkit}/prefs")
async def put_toolkit_prefs(
    toolkit: str, body: PrefsBody, request: Request,
) -> JSONResponse:
    """Toggle one or more actions for a toolkit.

    Body: ``{"actions": {"GOOGLECALENDAR_DELETE_EVENT": false, ...}}``.
    Slugs set to ``true`` are pruned from the store (enabled-by-default).

    404 for any toolkit not yet wired into the Connectors UI — keeps the
    surface honest about which toolkits actually have UI scaffolding.
    """
    from services import composio_action_prefs  # noqa: PLC0415
    if toolkit not in _PREFS_WRITABLE_TOOLKITS:
        raise HTTPException(
            status_code=404,
            detail=f"Per-action prefs are not configurable for toolkit '{toolkit}' yet.",
        )
    user_id = _resolve_user_id(request)
    del user_id
    updated = composio_action_prefs.bulk_set(toolkit, body.actions)
    return JSONResponse({"actions": updated})


@router.post("/api/connectors/composio/execute")
async def execute(body: ExecuteBody, request: Request) -> JSONResponse:
    user_id = _resolve_user_id(request, body.user_id)
    try:
        result = composio_service.execute_tool(user_id, body.tool_slug, body.arguments)
    except Exception as exc:
        log.exception("composio: execute_tool failed")
        raise HTTPException(status_code=502, detail=str(exc))
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Per-user MCP URL (consumed by the Claude adapter to wire --mcp-config)
# ---------------------------------------------------------------------------

@router.get("/api/connectors/composio/mcp-url")
async def mcp_url(request: Request) -> JSONResponse:
    user_id = _resolve_user_id(request)
    url = composio_service.get_mcp_url(user_id)
    return JSONResponse({"url": url})


# ---------------------------------------------------------------------------
# Gateway install — openclaw / hermes
#
# Unlike claude_code which gets per-session --mcp-config, openclaw and hermes
# expose tools/MCP via gateway-side config only. This endpoint writes the
# current user's Composio MCP URL into the gateway's config file. The gateway
# typically needs a restart to pick the change up.
# ---------------------------------------------------------------------------

@router.post("/api/connectors/composio/install-into-gateway")
async def install_into_gateway(
    request: Request,
    agent: str = Query(..., regex="^(openclaw|hermes)$"),
) -> JSONResponse:
    user_id = _resolve_user_id(request)
    if agent == "openclaw":
        result = composio_service.install_into_openclaw(user_id)
    else:
        result = composio_service.install_into_hermes(user_id)
    status = 200 if result.get("ok") else 422
    return JSONResponse(result, status_code=status)


# ---------------------------------------------------------------------------
# OAuth callback — Composio redirects here after the user authorizes.
# Mirrors the Vercel /callback pattern: postMessage to opener, then close.
# Frontend listens for {type: "connector-auth-complete"}.
# ---------------------------------------------------------------------------

@router.get("/api/connectors/composio/callback")
async def composio_callback(
    toolkit: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    error_description: Optional[str] = Query(default=None),
) -> HTMLResponse:
    if error or (status and status.upper() == "FAILED"):
        desc = error_description or error or "Authorization failed."
        body = {
            "type": "connector-auth-error",
            "connector": "composio",
            "toolkit": toolkit or "",
            "error": desc,
        }
        return HTMLResponse(content=_callback_html(body, ok=False), status_code=400)

    body = {
        "type": "connector-auth-complete",
        "connector": "composio",
        "toolkit": toolkit or "",
    }
    return HTMLResponse(content=_callback_html(body, ok=True))


def _callback_html(payload: dict[str, Any], ok: bool) -> str:
    title = "Connected" if ok else "Authorization failed"
    heading = "You're connected." if ok else "Authorization failed"
    sub = "You can close this window." if ok else payload.get("error", "")
    payload_json = json.dumps(payload)
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; padding: 32px; max-width: 480px; margin: 0 auto; }}
  h2 {{ margin: 0 0 8px; }} p {{ color: #555; }}
</style></head>
<body>
  <h2>{heading}</h2>
  <p>{sub}</p>
  <script>
    try {{
      if (window.opener) {{
        window.opener.postMessage({payload_json}, "*");
      }}
    }} catch (e) {{}}
    setTimeout(function () {{ window.close(); }}, 300);
  </script>
</body></html>"""
