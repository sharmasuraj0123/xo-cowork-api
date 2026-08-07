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

The redirect_uri is resolved per request rather than hardcoded — see
``_resolve_redirect_uri``. Vercel sends the browser back to *this* service's
/callback, so the URI has to be an origin the user's browser can actually
reach: in a remote workspace that is the proxy hostname, not loopback.
"""

import json
import logging
import os
import re
from html import escape
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from services.cowork_agent.connectors.vercel_connector import (
    RedirectUriLockedError,
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

# Path appended to a resolved origin to form the redirect_uri.
_CALLBACK_PATH = "/callback"

# A bare hostname with an optional port. Deliberately strict: whatever we accept
# here gets persisted and POSTed to Vercel as a redirect target.
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d+)?$")


# ---------------------------------------------------------------------------
# redirect_uri resolution
# ---------------------------------------------------------------------------

def _origin_of(value: str) -> str | None:
    """Return the ``scheme://host[:port]`` origin of *value*, or None.

    Rejects anything that is not a plain http(s) URL with a hostname-shaped
    netloc — notably ``javascript:`` URIs and scheme-relative ``//host`` forms.
    """
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.netloc or not _HOST_RE.match(parts.netloc):
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _clean_redirect_uri(value: str) -> str | None:
    """Validate *value* as an http(s) URL and drop its query and fragment.

    Returns None if it is not a usable redirect target.
    """
    origin = _origin_of(value)
    if origin is None:
        return None
    return origin + (urlsplit(value.strip()).path or _CALLBACK_PATH)


def workspace_origin() -> str | None:
    """This workspace's browser-reachable origin, from the IDE proxy template.

    Coder/VS Code export ``VSCODE_PROXY_URI`` as e.g.
    ``https://{{port}}--main--ws--user.example.com``; substituting our own port
    yields the URL a browser outside the container can load. Returns None when
    not running behind such a proxy.
    """
    template = os.getenv("VSCODE_PROXY_URI", "").strip()
    if not template or "{{port}}" not in template:
        return None
    port = os.getenv("PORT", "5002").strip()
    if not port.isdigit():
        return None
    return _origin_of(template.replace("{{port}}", port))


def _allowed_redirect_origins() -> set[str]:
    """Origins a *client-supplied* redirect URI is allowed to point at.

    Defaults to the CORS allowlist plus this workspace's own proxy origin, so
    the common case needs no configuration. Server-side sources
    (VERCEL_OAUTH_REDIRECT_URI, VSCODE_PROXY_URI) skip this check — they are
    not attacker-controlled.
    """
    raw = os.getenv("VERCEL_OAUTH_ALLOWED_REDIRECT_ORIGINS")
    if raw is None:
        raw = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
    origins = {o for o in (_origin_of(part) for part in raw.split(",")) if o}
    workspace = workspace_origin()
    if workspace:
        origins.add(workspace)
    return origins


def _forwarded_origin(request: Request) -> str | None:
    """Origin reconstructed from proxy headers, falling back to Referer.

    Both are client-influenced, so the caller must allowlist the result. Note
    that Next.js's rewrite proxy overwrites X-Forwarded-Host with the inbound
    Host and does not forward X-Forwarded-Proto, so behind that proxy Referer
    is often the only faithful source. ``Origin`` is deliberately not consulted:
    a same-origin fetch GET never sends it, and the one mode that does (the
    desktop shell) sends a ``tauri://`` origin that no browser could return to.
    """
    host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if host and _HOST_RE.match(host):
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        if proto not in ("http", "https"):
            proto = "http" if host.partition(":")[0] in ("localhost", "127.0.0.1") else "https"
        origin = _origin_of(f"{proto}://{host}")
        if origin:
            return origin
    return _origin_of(request.headers.get("referer") or "")


def _resolve_redirect_uri(request: Request, override: str | None) -> str:
    """Resolve a redirect URI Vercel can send the browser back to.

    Order: explicit ``?redirect_uri=`` → ``VERCEL_OAUTH_REDIRECT_URI`` → this
    workspace's IDE-proxy origin → proxy/Referer headers.

    Raises rather than falling back to the legacy ``http://127.0.0.1/callback``:
    that resolves to port 80 on the *user's* machine, where nothing listens, so
    a silent fallback would just move the dead end somewhere harder to diagnose.
    """
    if override:
        cleaned = _clean_redirect_uri(override)
        if cleaned is None:
            raise HTTPException(400, detail={
                "error": "invalid_redirect_uri",
                "detail": "redirect_uri must be an absolute http(s) URL.",
                "suggestion": "Pass something like https://your-workspace.example.com/callback.",
            })
        if _origin_of(cleaned) not in _allowed_redirect_origins():
            raise HTTPException(400, detail={
                "error": "redirect_uri_not_allowed",
                "detail": f"{_origin_of(cleaned)} is not an allowed redirect origin.",
                "suggestion": "Add it to VERCEL_OAUTH_ALLOWED_REDIRECT_ORIGINS (comma-separated).",
            })
        return cleaned

    env_override = os.getenv("VERCEL_OAUTH_REDIRECT_URI", "").strip()
    if env_override:
        cleaned = _clean_redirect_uri(env_override)
        if cleaned is None:
            raise HTTPException(500, detail={
                "error": "invalid_redirect_uri_config",
                "detail": "VERCEL_OAUTH_REDIRECT_URI is not a valid absolute http(s) URL.",
                "suggestion": "Set it to <origin>/callback, or unset it to auto-detect.",
            })
        return cleaned

    workspace = workspace_origin()
    if workspace:
        return workspace + _CALLBACK_PATH

    forwarded = _forwarded_origin(request)
    if forwarded and forwarded in _allowed_redirect_origins():
        return forwarded + _CALLBACK_PATH

    raise HTTPException(409, detail={
        "error": "redirect_uri_unresolved",
        "detail": (
            "Could not determine a browser-reachable callback URL for this "
            f"workspace (derived origin: {forwarded or 'none'})."
        ),
        "suggestion": (
            "Set VERCEL_OAUTH_REDIRECT_URI to <origin>/callback, using the URL "
            "you open this workspace with in the browser."
        ),
    })


def _js_string(value: str) -> str:
    """A JSON string literal safe to embed inside a <script> block.

    json.dumps on its own is not enough: it leaves ``</script>`` intact, which
    would close the enclosing element. Escaping the slash keeps the literal
    valid JavaScript while making that sequence inert.
    """
    return json.dumps(str(value)).replace("</", "<\\/")


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
    request: Request,
    redirect_uri: str = Query(default=None, description="Override the resolved redirect URI"),
) -> JSONResponse:
    """
    Initiate the Vercel OAuth 2.1 PKCE flow.

    Returns {"auth_url": "...", "redirect_uri": "...", "state": "..."}.
    The frontend should open auth_url in a popup. The returned redirect_uri is
    where Vercel will send the browser back — surface it so the user knows
    which tab to expect, and so a mismatch is diagnosable.
    """
    effective_redirect = _resolve_redirect_uri(request, redirect_uri)

    try:
        # Auto-register the OAuth client via Vercel's DCR endpoint on first
        # use, so a fresh checkout works without manual mcp-tokens.json setup.
        await ensure_oauth_client(effective_redirect)
        flow = start_oauth_flow(redirect_uri=effective_redirect)
    except RedirectUriLockedError as exc:
        raise HTTPException(409, detail={
            "error": "redirect_uri_locked",
            "detail": str(exc),
            "suggestion": "Disconnect Vercel first, then connect again.",
        })
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(500, detail=str(exc))

    return JSONResponse({
        "auth_url": flow["auth_url"],
        "redirect_uri": effective_redirect,
        "state": flow["state"],
    })


@router.post("/api/connectors/vercel/oauth/exchange")
async def vercel_oauth_exchange(body: OAuthExchangeBody) -> JSONResponse:
    """
    REST alternative to the /callback redirect — for environments where the
    resolved redirect_uri is unreachable from the user's browser.

    The frontend pastes the full callback URL; it extracts code+state and
    POSTs them here to complete the token exchange without a browser redirect.

    Note that /callback consumes the pending state, so pasting a URL that
    already reached /callback reports an expired state even though the
    connection succeeded — check status before treating that as a failure.
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

    The registered redirect_uri is resolved per flow (see
    ``_resolve_redirect_uri``); in a remote workspace it is the IDE-proxy
    origin, so this page renders in the user's browser and completes the
    exchange server-side.

    On success it posts vercel_oauth_success to the opener and closes — but the
    settings panel currently opens the auth tab with ``noopener``, so treat the
    rendered page, not the postMessage, as the user-visible outcome.
    """
    if error:
        desc = str(error_description or error)
        page = f"""<!DOCTYPE html>
<html><head><title>Vercel Authorization Failed</title></head>
<body>
<h2>Vercel Authorization Failed</h2>
<p>{escape(desc)}</p>
<p>Close this tab and try connecting again from the Vercel panel.</p>
<script>
  if (window.opener) {{
    window.opener.postMessage(
      {{ type: 'vercel_oauth_error', error: {_js_string(desc)} }},
      '*'
    );
    window.close();
  }}
</script>
</body></html>"""
        return HTMLResponse(content=page, status_code=400)

    if not code or not state:
        return HTMLResponse(
            content="<h2>Missing code or state parameter.</h2>",
            status_code=400,
        )

    result = await exchange_code_for_tokens(code=code, state=state)

    if not result.get("valid"):
        err = str(result.get("error", "Unknown error"))
        page = f"""<!DOCTYPE html>
<html><head><title>Vercel Token Exchange Failed</title></head>
<body>
<h2>Token Exchange Failed</h2>
<p>{escape(err)}</p>
<p>Close this tab and try connecting again from the Vercel panel.</p>
<script>
  if (window.opener) {{
    window.opener.postMessage(
      {{ type: 'vercel_oauth_error', error: {_js_string(err)} }},
      '*'
    );
    window.close();
  }}
</script>
</body></html>"""
        return HTMLResponse(content=page, status_code=502)

    username = str(result.get("username", ""))
    name = str(result.get("name", "") or username)
    greeting = f"Welcome, {escape(name)}! " if name else ""
    page = f"""<!DOCTYPE html>
<html><head><title>Vercel Connected</title></head>
<body>
<h2>Vercel Connected</h2>
<p>{greeting}Your Vercel account is now connected.</p>
<p>Close this tab and reopen the Vercel panel in Settings to see the updated status.</p>
<script>
  if (window.opener) {{
    window.opener.postMessage(
      {{
        type: 'vercel_oauth_success',
        username: {_js_string(username)},
        name: {_js_string(name)},
      }},
      '*'
    );
    window.close();
  }}
</script>
</body></html>"""
    return HTMLResponse(content=page)


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
