"""
Loopback MCP reverse proxy → Composio Tool Router session.

OpenClaw and Hermes connect to /mcp/cowork-proxy/ on localhost with NO
headers. This handler resolves the user_id server-side, asks
composio_service.build_mcp_server_entry(user_id) for the upstream session
URL + auth headers, and forwards the request transparently. Response is
streamed back (Composio replies are usually text/event-stream).

End result: COMPOSIO_API_KEY never has to be written into
~/.openclaw/openclaw.json or ~/.hermes/config.yaml. It lives only in
the xo-cowork-api process's env (loaded from .env).

Claude Code is NOT routed through this proxy — it keeps writing the
direct Composio URL into /tmp/xo-cowork/<sk>/mcp.json (per the plan).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from services import composio_service

from .composio import _resolve_user_id

log = logging.getLogger(__name__)
router = APIRouter()

# Headers we never forward in either direction. `host`/`content-length` are
# managed by httpx; `authorization` would override our injected `x-api-key`
# if a client sent one; hop-by-hop headers can confuse the SSE relay.
_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "authorization",
}

# Long enough for Composio to stream a large tool result; we don't impose
# an internal timeout — clients can disconnect.
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)


def _forwarded_headers(incoming: dict[str, str], inject: dict[str, str]) -> dict[str, str]:
    """Strip hop-by-hop headers, then layer the upstream-required headers on top."""
    out: dict[str, str] = {}
    for k, v in incoming.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        out[k] = v
    for k, v in (inject or {}).items():
        out[k] = v
    return out


async def _proxy(request: Request, method: str) -> StreamingResponse | JSONResponse:
    """Forward `request` to the Composio Tool Router session URL for the
    request's resolved user_id. Stream the upstream response back."""
    user_id = _resolve_user_id(request)

    try:
        entry = composio_service.build_mcp_server_entry(user_id)
    except Exception as exc:
        log.exception("mcp_proxy: build_mcp_server_entry failed")
        return JSONResponse(
            status_code=502,
            content={"error": "composio_session_unavailable", "detail": str(exc)},
        )

    upstream_url = entry.get("url")
    upstream_headers = entry.get("headers") or {}
    if not upstream_url:
        return JSONResponse(
            status_code=502,
            content={"error": "composio_session_unavailable", "detail": "no upstream url"},
        )

    body = await request.body()
    forward_headers = _forwarded_headers(dict(request.headers), upstream_headers)

    client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    req = client.build_request(
        method,
        upstream_url,
        headers=forward_headers,
        content=body if body else None,
        params=dict(request.query_params),
    )
    try:
        upstream_resp = await client.send(req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        log.warning("mcp_proxy: upstream request failed: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"error": "composio_unreachable", "detail": str(exc)},
        )

    # Relay status + a curated set of response headers (notably mcp-session-id
    # and content-type so SSE survives).
    response_headers: dict[str, str] = {}
    for k, v in upstream_resp.headers.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        if k.lower() == "content-type":
            response_headers[k] = v
        elif k.lower() == "mcp-session-id":
            response_headers[k] = v

    async def relay() -> Any:
        try:
            async for chunk in upstream_resp.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


@router.post("/mcp/cowork-proxy/")
@router.post("/mcp/cowork-proxy")
async def mcp_proxy_post(request: Request):
    return await _proxy(request, "POST")


@router.get("/mcp/cowork-proxy/")
@router.get("/mcp/cowork-proxy")
async def mcp_proxy_get(request: Request):
    return await _proxy(request, "GET")


@router.delete("/mcp/cowork-proxy/")
@router.delete("/mcp/cowork-proxy")
async def mcp_proxy_delete(request: Request):
    return await _proxy(request, "DELETE")
