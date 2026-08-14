"""
REST routes for the Vercel connector.

Endpoints:
  POST /api/connectors/vercel/token                — validate & store API token
  GET  /api/connectors/vercel/status               — current connection status
  POST /api/connectors/vercel/disconnect           — delete stored token
  POST /api/connectors/vercel/reconnect            — re-validate stored token
  GET  /api/connectors/vercel/oauth/start          — initiate OAuth 2.1 PKCE flow
  POST /api/connectors/vercel/oauth/exchange       — complete the flow from a pasted callback URL
  GET  /callback                                    — OAuth 2.1 callback (matches registered redirect_uri)
  GET  /.well-known/oauth-protected-resource        — RFC 9728 resource server metadata
  OPTIONS /.well-known/oauth-protected-resource     — CORS preflight

Redirect URI: the flow used to hardcode ``http://127.0.0.1/callback``, which
dead-ends in a browser 404 for every deployment where port 80 of the user's
machine isn't this API (remote workspaces, containers, any non-default port).
/oauth/start now derives the redirect from the origin the request actually
arrived on, so the redirect lands back on this router's /callback and completes
by itself. VERCEL_OAUTH_REDIRECT_URI still overrides when a deployment knows
better, and ?redirect_uri= overrides that for one-off flows.

When the browser genuinely cannot reach this API (no port forwarding), the
redirect still dead-ends on a 404 page — but the URL in the address bar carries
the code and state, so pasting it into POST /oauth/exchange finishes the
connection. Same escape hatch the MagicPath connector uses for its code paste.
"""

import logging
import os
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from services.cowork_agent.connectors.vercel import (
    delete_vercel_token,
    ensure_oauth_client,
    exchange_code_for_tokens,
    get_oauth_client,
    get_status,
    get_valid_access_token,
    save_vercel_token,
    start_oauth_flow,
    validate_token,
)

log = logging.getLogger(__name__)
router = APIRouter()

# Deployment-wide override; when unset the redirect is derived per request.
_ENV_REDIRECT_URI = (os.getenv("VERCEL_OAUTH_REDIRECT_URI") or "").strip()
# Last resort only — reachable just when this API happens to serve port 80.
_LOOPBACK_REDIRECT_URI = "http://127.0.0.1/callback"

_CALLBACK_PATH = "/callback"


class TokenBody(BaseModel):
    token: str


class OAuthExchangeBody(BaseModel):
    """Either code+state, or the whole callback URL pasted from the browser.

    `code` accepts a full URL too, so a paste into the obvious field works.
    """

    code: str | None = Field(default=None, max_length=8192)
    state: str | None = Field(default=None, max_length=4096)
    callback_url: str | None = Field(default=None, max_length=8192)


def _public_origin(request: Request) -> str | None:
    """Origin this API was reached on, as the browser sees it.

    Prefers the proxy's forwarded headers (uvicorn runs without
    --proxy-headers here, so request.url is the internal address) and falls
    back to the Host header. Returns None when neither names a host.
    """
    headers = request.headers
    host = (headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if not host:
        host = (headers.get("host") or "").strip()
    if not host:
        return None
    scheme = (headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return f"{scheme or request.url.scheme}://{host}"


def _resolve_redirect_uri(request: Request, override: str | None) -> str:
    """Pick the redirect_uri for a flow: explicit > env > request origin."""
    explicit = (override or "").strip()
    if explicit:
        return explicit
    if _ENV_REDIRECT_URI:
        return _ENV_REDIRECT_URI
    origin = _public_origin(request)
    return f"{origin}{_CALLBACK_PATH}" if origin else _LOOPBACK_REDIRECT_URI


def _split_callback_input(body: OAuthExchangeBody) -> tuple[str, str]:
    """Extract (code, state) from separate fields or a pasted callback URL."""
    code = (body.code or "").strip()
    state = (body.state or "").strip()

    pasted = (body.callback_url or "").strip()
    if not pasted and code.lower().startswith(("http://", "https://")):
        pasted, code = code, ""

    if pasted:
        query = parse_qs(urlparse(pasted).query)
        error = (query.get("error") or [""])[0].strip()
        if error:
            desc = (query.get("error_description") or [""])[0].strip()
            raise HTTPException(422, detail=desc or error)
        code = (query.get("code") or [code])[0].strip()
        state = (query.get("state") or [state])[0].strip()

    if not code or not state:
        raise HTTPException(
            400,
            detail=(
                "Provide the full callback URL from the browser address bar "
                "(callback_url), or code and state separately."
            ),
        )
    return code, state


# ---------------------------------------------------------------------------
# API-token endpoints
# ---------------------------------------------------------------------------

@router.post("/api/connectors/vercel/token")
async def submit_vercel_token(body: TokenBody) -> JSONResponse:
    token = body.token.strip()
    if not token:
        raise HTTPException(400, detail="Token is required.")

    result = await validate_token(token)
    if not result.get("valid"):
        raise HTTPException(422, detail=result.get("error", "Invalid token."))

    save_vercel_token(
        token,
        username=result.get("username", ""),
        name=result.get("name", ""),
    )
    return JSONResponse({
        "status": "connected",
        "username": result.get("username", ""),
        "name": result.get("name", ""),
        "auth_method": "api_token",
    })


@router.get("/api/connectors/vercel/status")
async def vercel_status() -> JSONResponse:
    return JSONResponse(await get_status())


@router.post("/api/connectors/vercel/disconnect")
async def disconnect_vercel() -> JSONResponse:
    delete_vercel_token()
    return JSONResponse({"status": "needs_auth"})


@router.post("/api/connectors/vercel/reconnect")
async def reconnect_vercel() -> JSONResponse:
    token = await get_valid_access_token()
    if not token:
        return JSONResponse({"status": "needs_auth", "error": "No token stored."})

    result = await validate_token(token)
    if result.get("valid"):
        return JSONResponse({
            "status": "connected",
            "username": result.get("username", ""),
            "name": result.get("name", ""),
            "auth_method": result.get("auth_method", "api_token"),
        })
    return JSONResponse(
        {"status": result["status"], "error": result.get("error", "")},
        status_code=502,
    )


# ---------------------------------------------------------------------------
# OAuth 2.1 Authorization Code + PKCE flow
# ---------------------------------------------------------------------------

@router.get("/api/connectors/vercel/oauth/start")
async def vercel_oauth_start(
    request: Request,
    redirect_uri: str = Query(default=None, description="Override the registered redirect URI"),
) -> JSONResponse:
    """
    Initiate the Vercel OAuth 2.1 PKCE flow.

    Returns {"auth_url", "state", "redirect_uri", "manual_exchange_url"}.
    The frontend should open auth_url (e.g. in a popup) and listen for the
    postMessage from /callback to know when the flow completes.

    `redirect_uri` defaults to this API's own /callback on the origin the
    request came in on. If the browser can't reach that origin the redirect
    lands on a 404 page — the frontend should then offer the address-bar URL
    to manual_exchange_url, which completes the same flow without a redirect.
    """
    effective_redirect = _resolve_redirect_uri(request, redirect_uri)

    try:
        # Auto-register the OAuth client via Vercel's DCR endpoint on first
        # use, so a fresh checkout works without manual token.json setup.
        await ensure_oauth_client(effective_redirect)
        flow = start_oauth_flow(redirect_uri=effective_redirect)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(500, detail=str(exc))

    return JSONResponse({
        "auth_url": flow["auth_url"],
        "state": flow["state"],
        "redirect_uri": effective_redirect,
        "manual_exchange_url": "/api/connectors/vercel/oauth/exchange",
    })


@router.post("/api/connectors/vercel/oauth/exchange")
async def vercel_oauth_exchange(body: OAuthExchangeBody) -> JSONResponse:
    """
    REST alternative to the /callback redirect — for environments where the
    redirect lands somewhere the API isn't (remote workspaces, containers).

    Accepts the full callback URL straight from the browser address bar (in
    `callback_url`, or in `code` — a URL there is detected), or code+state as
    separate fields. Completes the token exchange without a browser redirect.
    """
    code, state = _split_callback_input(body)
    result = await exchange_code_for_tokens(code=code, state=state)
    if not result.get("valid"):
        raise HTTPException(422, detail=result.get("error", "Token exchange failed."))
    return JSONResponse({
        "status": "connected",
        "username": result.get("username", ""),
        "name": result.get("name", ""),
        "auth_method": "oauth",
    })


@router.get("/callback")
async def vercel_oauth_callback(
    code: str = Query(default=None),
    state: str = Query(default=None),
    error: str = Query(default=None),
    error_description: str = Query(default=None),
) -> HTMLResponse:
    """
    OAuth 2.1 callback — Vercel redirects here after user authorization.

    Reached only when the browser can resolve the redirect_uri chosen by
    /oauth/start; otherwise the frontend finishes via /oauth/exchange instead.
    On success, posts a vercel_oauth_success message to the opener and closes.
    On failure, posts vercel_oauth_error.
    """
    if error:
        desc = error_description or error
        html = f"""<!DOCTYPE html>
<html><head><title>Vercel Authorization Failed</title></head>
<body>
<h2>Vercel Authorization Failed</h2>
<p>{desc}</p>
<script>
  if (window.opener) {{
    window.opener.postMessage(
      {{ type: 'vercel_oauth_error', error: {repr(str(desc))} }},
      '*'
    );
    window.close();
  }}
</script>
</body></html>"""
        return HTMLResponse(content=html, status_code=400)

    if not code or not state:
        return HTMLResponse(
            content=(
                "<h2>Missing code or state parameter.</h2>"
                "<p>Start the connection again from the Vercel connector.</p>"
            ),
            status_code=400,
        )

    result = await exchange_code_for_tokens(code=code, state=state)

    if not result.get("valid"):
        err = result.get("error", "Unknown error")
        html = f"""<!DOCTYPE html>
<html><head><title>Vercel Token Exchange Failed</title></head>
<body>
<h2>Token Exchange Failed</h2>
<p>{err}</p>
<script>
  if (window.opener) {{
    window.opener.postMessage(
      {{ type: 'vercel_oauth_error', error: {repr(str(err))} }},
      '*'
    );
    window.close();
  }}
</script>
</body></html>"""
        return HTMLResponse(content=html, status_code=502)

    username = result.get("username", "")
    name = result.get("name", "") or username
    html = f"""<!DOCTYPE html>
<html><head><title>Vercel Connected</title></head>
<body>
<h2>Vercel Connected</h2>
<p>Welcome, {name}! You can close this window.</p>
<script>
  if (window.opener) {{
    window.opener.postMessage(
      {{
        type: 'vercel_oauth_success',
        username: {repr(username)},
        name: {repr(name)},
      }},
      '*'
    );
    window.close();
  }}
</script>
</body></html>"""
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# RFC 9728 — OAuth Protected Resource Metadata
# ---------------------------------------------------------------------------

@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource_metadata(request: Request) -> JSONResponse:
    """
    OAuth 2.0 Protected Resource Metadata per RFC 9728 and the MCP authorization spec.

    Allows MCP clients (e.g. Manus) to discover which authorization server issues
    valid tokens for this resource server and what scopes are supported.
    """
    base_url = str(request.base_url).rstrip("/")
    return JSONResponse(
        content={
            "resource": base_url,
            "authorization_servers": ["https://vercel.com"],
            "scopes_supported": ["read:projects", "deploy:projects"],
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://vercel.com/docs/rest-api",
        },
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Cache-Control": "no-store",
        },
    )


@router.options("/.well-known/oauth-protected-resource")
async def oauth_protected_resource_cors() -> JSONResponse:
    """CORS preflight for the OAuth protected resource metadata endpoint."""
    return JSONResponse(
        content={},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
    )
