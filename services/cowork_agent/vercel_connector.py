"""
Vercel connector — OAuth 2.1 with PKCE (no env vars).

Uses Vercel's dynamic client registration + PKCE flow:
  1. Register a client dynamically (one-time, stored in mcp-tokens.json)
  2. Generate PKCE code_verifier + S256 challenge
  3. Start a local callback server on a free port
  4. Open browser to Vercel's authorization endpoint
  5. Receive callback with auth code
  6. Exchange code for access_token + refresh_token
  7. Store tokens in mcp-tokens.json

No client_secret needed. No environment variables.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from typing import Any, Literal
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths & URLs
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
TOKEN_FILE = _PROJECT_ROOT / "mcp-tokens.json"

VERCEL_AUTHORIZE_URL = "https://vercel.com/oauth/authorize"
VERCEL_TOKEN_URL = "https://vercel.com/api/login/oauth/token"
VERCEL_REGISTER_URL = "https://vercel.com/api/login/oauth/register"
VERCEL_REVOKE_URL = "https://vercel.com/api/login/oauth/token/revoke"
VERCEL_USER_URL = "https://api.vercel.com/v2/user"

SESSION_TTL = 600   # 10 min
OAUTH_TIMEOUT = 300  # 5 min

# Fixed callback port so remote dev environments (Coder, Codespaces, etc.)
# can set up a stable port-forward. Override with VERCEL_CALLBACK_PORT.
CALLBACK_PORT = int(os.environ.get("VERCEL_CALLBACK_PORT", "53683"))

# ---------------------------------------------------------------------------
# Token storage (shared with github_connector via mcp-tokens.json)
# ---------------------------------------------------------------------------

def _read_tokens() -> dict[str, Any]:
    if not TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s: %s", TOKEN_FILE, exc)
        return {}


def _write_tokens(data: dict[str, Any]) -> None:
    TOKEN_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def get_vercel_token() -> dict[str, Any] | None:
    """Return the stored Vercel token entry, or None."""
    return _read_tokens().get("vercel") or None


def save_vercel_token(token_data: dict[str, Any]) -> None:
    """Save Vercel OAuth tokens to mcp-tokens.json."""
    data = _read_tokens()
    data["vercel"] = token_data
    _write_tokens(data)
    log.info("Vercel token saved to %s", TOKEN_FILE)


def delete_vercel_token() -> None:
    """Remove the Vercel entry from mcp-tokens.json."""
    data = _read_tokens()
    data.pop("vercel", None)
    # Also remove cached client registration
    data.pop("vercel_client", None)
    _write_tokens(data)
    log.info("Vercel token removed from %s", TOKEN_FILE)


def _get_or_register_client() -> dict[str, str]:
    """Get or create a dynamic OAuth client registration."""
    data = _read_tokens()
    client = data.get("vercel_client")
    if client and client.get("client_id"):
        return client

    # Register a new client dynamically
    log.info("Registering new OAuth client with Vercel...")
    resp = httpx.post(VERCEL_REGISTER_URL, json={
        "client_name": "xo-cowork",
        "redirect_uris": ["http://127.0.0.1:0/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, timeout=15)

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Client registration failed: {resp.status_code} {resp.text}")

    client = resp.json()
    data["vercel_client"] = client
    _write_tokens(data)
    log.info("Registered Vercel OAuth client: %s", client.get("client_id"))
    return client


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    """Handles the OAuth redirect callback."""

    auth_code: str | None = None
    error: str | None = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="font-family:system-ui;display:flex;align-items:center;
                justify-content:center;height:100vh;margin:0;background:#0a0a0a;color:#fff;">
                <div style="text-align:center">
                <h1 style="color:#00dc82">&#10003; Connected to Vercel!</h1>
                <p style="color:#888">You can close this tab and return to xo-cowork.</p>
                </div></body></html>
            """)
        elif "error" in params:
            _CallbackHandler.error = params.get("error_description", params["error"])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"""
                <html><body style="font-family:system-ui;display:flex;align-items:center;
                justify-content:center;height:100vh;margin:0;background:#0a0a0a;color:#fff;">
                <div style="text-align:center">
                <h1 style="color:#ef4444">Authorization Failed</h1>
                <p style="color:#888">{_CallbackHandler.error}</p>
                </div></body></html>
            """.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress default HTTP server logging
        pass


# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------

SessionStatus = Literal["pending", "awaiting_oauth", "completed", "failed", "cancelled"]


@dataclass
class VercelSession:
    session_id: str
    status: SessionStatus = "pending"
    auth_url: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = field(default=None, repr=False)


_sessions: dict[str, VercelSession] = {}


def get_session(session_id: str) -> VercelSession | None:
    return _sessions.get(session_id)


def _active_oauth_session() -> VercelSession | None:
    for s in _sessions.values():
        if s.status == "awaiting_oauth":
            return s
    return None


def _expire_sessions() -> None:
    now = time.time()
    for sid in [k for k, v in _sessions.items() if now - v.created_at > SESSION_TTL]:
        s = _sessions.pop(sid)
        if s.task and not s.task.done():
            s.task.cancel()


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

async def validate_vercel_token(access_token: str) -> dict[str, Any]:
    """Validate a Vercel token by calling /v2/user."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                VERCEL_USER_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if resp.status_code == 200:
            user = resp.json().get("user", resp.json())
            return {
                "valid": True,
                "status": "connected",
                "username": user.get("username", ""),
                "name": user.get("name", ""),
                "email": user.get("email", ""),
                "avatar_url": user.get("avatar") or "",
            }
        elif resp.status_code in (401, 403):
            return {
                "valid": False,
                "status": "needs_auth",
                "error": "Token is invalid or revoked.",
            }
        elif resp.status_code == 404:
            # MCP-scoped OAuth tokens may not have REST API access.
            # Treat 404 as "token works but can't access /v2/user".
            return {
                "valid": True,
                "status": "connected",
                "username": "",
                "name": "",
                "email": "",
                "avatar_url": "",
            }
        else:
            return {
                "valid": False,
                "status": "failed",
                "error": f"Vercel returned HTTP {resp.status_code}.",
            }
    except Exception as exc:
        return {
            "valid": False,
            "status": "failed",
            "error": f"Could not connect to Vercel: {exc}",
        }


async def get_status() -> dict[str, Any]:
    """Compute the current Vercel connector status."""
    entry = get_vercel_token()
    if not entry:
        return {"status": "needs_auth"}

    access_token = entry.get("access_token")
    if not access_token:
        return {"status": "needs_auth"}

    result = await validate_vercel_token(access_token)
    # Merge stored metadata (username, name) if API didn't return them
    if result.get("valid") and not result.get("username"):
        result["username"] = entry.get("username", "")
        result["name"] = entry.get("name", "")
    return result


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

async def _run_oauth_flow(session: VercelSession) -> None:
    """Run the full OAuth 2.1 + PKCE flow."""
    server: HTTPServer | None = None
    try:
        # ── 1. Get or register client ────────────────────────────────
        client_info = _get_or_register_client()
        client_id = client_info["client_id"]

        # ── 2. Generate PKCE ─────────────────────────────────────────
        code_verifier, code_challenge = _generate_pkce()
        state = secrets.token_urlsafe(32)

        # ── 3. Start local callback server on a free port ────────────
        _CallbackHandler.auth_code = None
        _CallbackHandler.error = None

        server = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}/callback"

        # Update client registration with the actual redirect URI
        client_info_data = _read_tokens()
        if "vercel_client" in client_info_data:
            client_info_data["vercel_client"]["redirect_uris"] = [redirect_uri]
            _write_tokens(client_info_data)

        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        log.info("Vercel OAuth callback server started on port %d", port)

        # ── 4. Build authorization URL ───────────────────────────────
        auth_params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        auth_url = f"{VERCEL_AUTHORIZE_URL}?{urlencode(auth_params)}"

        session.auth_url = auth_url
        session.status = "awaiting_oauth"
        log.info("Vercel %s: auth URL ready: %s", session.session_id, auth_url[:80])

        # ── 5. Wait for callback ─────────────────────────────────────
        deadline = time.time() + OAUTH_TIMEOUT
        while time.time() < deadline:
            if session.status == "cancelled":
                return
            if _CallbackHandler.auth_code or _CallbackHandler.error:
                break
            await asyncio.sleep(0.5)
        else:
            session.status = "failed"
            session.error = "Timed out waiting for Vercel authorization."
            return

        if _CallbackHandler.error:
            session.status = "failed"
            session.error = f"Vercel denied access: {_CallbackHandler.error}"
            return

        auth_code = _CallbackHandler.auth_code
        log.info("Vercel %s: received auth code", session.session_id)

        # ── 6. Exchange code for tokens ──────────────────────────────
        async with httpx.AsyncClient(timeout=15) as http:
            token_resp = await http.post(
                VERCEL_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": auth_code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if token_resp.status_code != 200:
            session.status = "failed"
            session.error = f"Token exchange failed: {token_resp.status_code} {token_resp.text}"
            return

        token_data = token_resp.json()
        log.info("Vercel %s: token exchange successful", session.session_id)

        # ── 7. Store token ────────────────────────────────────────────
        access_token = token_data.get("access_token")
        if not access_token:
            session.status = "failed"
            session.error = "No access_token in token response."
            return

        # Try to get user info (may fail with 404 for MCP-scoped tokens)
        username = ""
        display_name = ""
        validation = await validate_vercel_token(access_token)
        if validation.get("valid"):
            username = validation.get("username", "")
            display_name = validation.get("name", "")

        # Save to mcp-tokens.json
        save_vercel_token({
            "access_token": access_token,
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": int(time.time()) + token_data.get("expires_in", 0)
                if token_data.get("expires_in") else 0,
            "token_type": token_data.get("token_type", "Bearer"),
            "scope": token_data.get("scope", ""),
            "username": username,
            "name": display_name,
        })

        session.status = "completed"
        log.info("Vercel %s: connected ✓ (user=%s)", session.session_id, username or "unknown")

    except asyncio.CancelledError:
        session.status = "cancelled"
    except Exception as exc:
        log.exception("Vercel OAuth error in session %s", session.session_id)
        session.status = "failed"
        session.error = str(exc)
    finally:
        if server:
            server.shutdown()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def create_oauth_session() -> VercelSession:
    """Start a new Vercel OAuth flow."""
    _expire_sessions()
    active = _active_oauth_session()
    if active:
        raise RuntimeError("Another Vercel connection is being set up. Please finish or cancel it first.")

    session_id = str(uuid.uuid4())
    session = VercelSession(session_id=session_id)
    _sessions[session_id] = session
    session.task = asyncio.create_task(_run_oauth_flow(session))
    return session


async def cancel_session(session_id: str) -> None:
    session = _sessions.get(session_id)
    if not session:
        return
    session.status = "cancelled"
    if session.task and not session.task.done():
        session.task.cancel()
    _sessions.pop(session_id, None)
