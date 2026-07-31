"""
Loopback MCP reverse proxy → Composio Tool Router session.

Every runtime (OpenClaw, Hermes, Claude Code) connects to
/mcp/cowork-proxy/u/<token> on localhost with NO headers. This handler resolves
the token to a user_id, asks composio_service.build_mcp_server_entry(user_id)
for the upstream session URL + auth headers, and forwards the request
transparently. The response is streamed back (Composio replies are usually
text/event-stream).

End result: COMPOSIO_API_KEY is never written into ~/.openclaw/openclaw.json,
~/.hermes/config.yaml, or Claude Code's per-session mcp.json. It lives only in
the xo-cowork-api process's env (loaded from .env).

The token is opaque and random, resolved by lookup in the session store — not
signed, so there is no shared secret to configure. Because it is stable per
user, a config written once keeps working after connector toggles and restarts;
this handler picks up whatever session that user currently has.

The unscoped /mcp/cowork-proxy/ paths carry no identity and always 401 — they
exist so a stale config gets a clear error rather than a silent wrong-user call.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from services.composio import service as composio_service

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


def _proxy_user(token: str | None) -> str | None:
    """Resolve which Composio user this header-less proxy call belongs to.

    Identity comes only from the opaque token in the path, looked up in the
    session store. No token, or an unknown one, resolves to None and the caller
    401s — there is no fallback that could silently cross tenants.
    """
    if not token:
        return None
    return composio_service.user_for_proxy_token(token)


async def _proxy(
    request: Request, method: str, token: str | None = None,
) -> StreamingResponse | JSONResponse:
    """Forward `request` to the Composio Tool Router session URL for the
    request's resolved user_id. Stream the upstream response back."""
    user_id = _proxy_user(token)
    if not user_id:
        return JSONResponse(
            status_code=401,
            content={
                "error": "composio_identity_required",
                "detail": (
                    "This MCP proxy call carried no recognised user token. "
                    "Re-install the MCP config for this agent "
                    "(POST /api/connectors/composio/refresh-gateway) so it "
                    "points at a valid /mcp/cowork-proxy/u/<token> URL."
                ),
            },
        )

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


# User-scoped variants — the only ones that resolve to a user. The agent's MCP
# client is always configured with one of these; the opaque token is the only
# identity a header-less call can carry. Minted by
# composio_service._cowork_proxy_url() / proxy_token_for_user().


@router.post("/mcp/cowork-proxy/u/{token}/")
@router.post("/mcp/cowork-proxy/u/{token}")
async def mcp_proxy_post_scoped(request: Request, token: str):
    return await _proxy(request, "POST", token)


@router.get("/mcp/cowork-proxy/u/{token}/")
@router.get("/mcp/cowork-proxy/u/{token}")
async def mcp_proxy_get_scoped(request: Request, token: str):
    return await _proxy(request, "GET", token)


@router.delete("/mcp/cowork-proxy/u/{token}/")
@router.delete("/mcp/cowork-proxy/u/{token}")
async def mcp_proxy_delete_scoped(request: Request, token: str):
    return await _proxy(request, "DELETE", token)
