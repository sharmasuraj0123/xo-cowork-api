"""
REST routes for the Vercel connector.

Endpoints:
  POST /api/connectors/vercel/token                — validate & store API token
  GET  /api/connectors/vercel/status               — current connection status
  POST /api/connectors/vercel/disconnect           — delete stored token
  POST /api/connectors/vercel/reconnect            — re-validate stored token
  GET  /api/connectors/vercel/oauth/start          — initiate OAuth 2.1 PKCE flow
  GET  /callback                                    — OAuth 2.1 callback (matches registered redirect_uri)
  GET  /.well-known/oauth-protected-resource        — RFC 9728 resource server metadata
  OPTIONS /.well-known/oauth-protected-resource     — CORS preflight
"""

import logging
import os

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from services.cowork_agent.vercel_connector import (
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

# Must match exactly the redirect_uri registered in vercel_client.redirect_uris.
_DEFAULT_REDIRECT_URI = os.getenv("VERCEL_OAUTH_REDIRECT_URI", "http://127.0.0.1/callback")


class TokenBody(BaseModel):
    token: str


class OAuthExchangeBody(BaseModel):
    code: str
    state: str


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
    redirect_uri: str = Query(default=None, description="Override the registered redirect URI"),
) -> JSONResponse:
    """
    Initiate the Vercel OAuth 2.1 PKCE flow.

    Returns {"auth_url": "...", "state": "..."}.
    The frontend should open auth_url (e.g. in a popup) and listen for the
    postMessage from /callback to know when the flow completes.
    """
    effective_redirect = redirect_uri or _DEFAULT_REDIRECT_URI

    try:
        # Auto-register the OAuth client via Vercel's DCR endpoint on first
        # use, so a fresh checkout works without manual mcp-tokens.json setup.
        await ensure_oauth_client(effective_redirect)
        flow = start_oauth_flow(redirect_uri=effective_redirect)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(500, detail=str(exc))

    return JSONResponse({
        "auth_url": flow["auth_url"],
        "state": flow["state"],
    })


@router.post("/api/connectors/vercel/oauth/exchange")
async def vercel_oauth_exchange(body: OAuthExchangeBody) -> JSONResponse:
    """
    REST alternative to the /callback redirect — for environments where
    http://127.0.0.1/callback is unreachable (remote workspaces, containers).

    The frontend pastes the full callback URL; it extracts code+state and
    POSTs them here to complete the token exchange without a browser redirect.
    """
    result = await exchange_code_for_tokens(code=body.code.strip(), state=body.state.strip())
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

    Registered redirect_uri: http://127.0.0.1/callback (or VERCEL_OAUTH_REDIRECT_URI).
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
            content="<h2>Missing code or state parameter.</h2>",
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
