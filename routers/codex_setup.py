"""
OpenAI Codex OAuth setup: pure-Python PKCE flow that mirrors the JS
Codex-Oauth-Flow reference repo.

Flow:
  1. POST /codex/setup        -> SSE stream; emits session_id + auth URL
  2. User opens URL, logs in at OpenAI, gets redirected to localhost:1455
  3. User copies redirect URL (with ?code=...&state=...)
  4. POST /codex/setup/callback  -> exchanges code for tokens, writes creds

No external OAuth library required — the entire flow is standard
OAuth 2.0 Authorization Code + PKCE, implemented with httpx.

On success, persists credentials to:
  - ~/.openclaw/agents/main/agent/auth-profiles.json  (tokens / secrets)
  - ~/.openclaw/openclaw.json                          (metadata)
  - Project & OpenClaw .env files                      (OPENAI_CODEX_ACCESS_TOKEN)
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


# =============================================================================
# OAuth constants (extracted from @mariozechner/pi-ai library source)
# =============================================================================

OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_REDIRECT_URI = "http://localhost:1455/auth/callback"
OPENAI_SCOPES = "openid profile email offline_access"

# Credential file constants (match JS Codex-Oauth-Flow repo)
PROVIDER_ID = "openai-codex"
AUTH_STORE_VERSION = 1


# =============================================================================
# PKCE helpers
# =============================================================================

def _generate_pkce() -> tuple[str, str]:
    """Generate a PKCE code_verifier and code_challenge (S256)."""
    verifier_bytes = secrets.token_bytes(32)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _generate_state() -> str:
    """Random hex state parameter (16 bytes -> 32 hex chars)."""
    return secrets.token_hex(16)


# =============================================================================
# JWT identity extraction (mirrors jwt-identity.ts)
# =============================================================================

def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    """Decode the payload segment of a JWT (no signature verification)."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1]
    # Pad to multiple of 4
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception:
        return {}


def resolve_codex_auth_identity(
    access_token: str,
    email_hint: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """
    Extract email and profile name from a Codex JWT access token.

    Priority mirrors the JS implementation:
      email:       JWT profile claim > email_hint
      profileName: email > b64(chatgpt_account_user_id) > chatgpt_user_id
                   > user_id > iss|sub > "default"
    """
    claims = _decode_jwt_payload(access_token)

    profile_claim = claims.get("https://api.openai.com/profile", {})
    email = profile_claim.get("email") or email_hint

    auth_claim = claims.get("https://api.openai.com/auth", {})
    profile_name: Optional[str] = email

    if not profile_name and auth_claim.get("chatgpt_account_user_id"):
        raw = auth_claim["chatgpt_account_user_id"].encode("utf-8")
        profile_name = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if not profile_name:
        profile_name = auth_claim.get("chatgpt_user_id") or auth_claim.get("user_id")
    if not profile_name and claims.get("iss") and claims.get("sub"):
        profile_name = f"{claims['iss']}|{claims['sub']}"

    # Extract account_id (used by Codex CLI internally)
    account_id = auth_claim.get("chatgpt_account_id")

    return {
        "email": email,
        "profile_name": profile_name or "default",
        "account_id": account_id,
    }


# =============================================================================
# Credential file I/O (mirrors auth-store.ts)
# =============================================================================

def _resolve_state_dir() -> str:
    return os.environ.get("OPENCLAW_STATE_DIR") or str(Path.home() / ".openclaw")


def _resolve_auth_store_path() -> str:
    return str(Path(_resolve_state_dir()) / "agents" / "main" / "agent" / "auth-profiles.json")


def _resolve_config_path() -> str:
    return str(Path(_resolve_state_dir()) / "openclaw.json")


def _read_json(path: str, fallback: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return fallback


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def write_auth_credentials(
    profile_name: str,
    access: str,
    refresh: str,
    expires: int,
    email: Optional[str] = None,
) -> None:
    """
    Persist Codex OAuth credentials to ~/.openclaw/ files.

    Writes:
      auth-profiles.json  -> tokens (secrets)
      openclaw.json        -> metadata (no secrets)
    """
    auth_path = _resolve_auth_store_path()
    config_path = _resolve_config_path()
    profile_id = f"{PROVIDER_ID}:{profile_name}"

    # --- auth-profiles.json ---
    store = _read_json(auth_path, {"version": AUTH_STORE_VERSION, "profiles": {}})
    store["version"] = AUTH_STORE_VERSION
    store.setdefault("profiles", {})[profile_id] = {
        "type": "oauth",
        "provider": PROVIDER_ID,
        "access": access,
        "refresh": refresh,
        "expires": expires,
        "email": email,
    }
    order = store.setdefault("order", {})
    provider_order = order.setdefault(PROVIDER_ID, [])
    if profile_name not in provider_order:
        provider_order.append(profile_name)
    store.setdefault("lastGood", {})[PROVIDER_ID] = profile_name
    _write_json(auth_path, store)

    # --- openclaw.json ---
    config = _read_json(config_path, {})
    auth_section = config.setdefault("auth", {})
    profiles = auth_section.setdefault("profiles", {})
    profiles[profile_id] = {
        "provider": PROVIDER_ID,
        "mode": "oauth",
        "email": email,
    }
    _write_json(config_path, config)


# =============================================================================
# .env persistence (same pattern as claude_setup_token.py)
# =============================================================================

_CODEX_TOKEN_ENV_KEYS = ["OPENAI_CODEX_ACCESS_TOKEN"]


def _project_env_path() -> str:
    return os.getenv("DOTENV_PATH") or str(Path(__file__).resolve().parent.parent / ".env")


def _openclaw_env_path() -> str:
    return str(Path.home() / ".openclaw" / ".env")


def _upsert_env_key(env_path: str, key: str, value: str) -> None:
    if not os.path.isfile(env_path):
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w") as f:
            f.write(f'{key}="{value}"\n')
        return
    lines: list[str] = []
    found = False
    with open(env_path, "r") as f:
        for raw in f:
            if raw.strip().startswith(f"{key}="):
                lines.append(f'{key}="{value}"\n')
                found = True
            else:
                lines.append(raw)
    if not found:
        lines.append(f'\n{key}="{value}"\n')
    with open(env_path, "w") as f:
        f.writelines(lines)


def _persist_token_to_env_files(access_token: str) -> None:
    env_paths = [_project_env_path(), _openclaw_env_path()]
    for env_path in env_paths:
        for key in _CODEX_TOKEN_ENV_KEYS:
            try:
                _upsert_env_key(env_path, key, access_token)
            except OSError as e:
                print(f"[codex-setup] Failed to write {key} to {env_path}: {e}")


# =============================================================================
# Session state
# =============================================================================

_setup_lock = asyncio.Lock()

# Per-session state stored in a dict keyed by session_id
_sessions: Dict[str, Dict[str, Any]] = {}

CODEX_SETUP_TIMEOUT_SECONDS = int(os.getenv("CODEX_SETUP_TIMEOUT", "300"))


# =============================================================================
# Token exchange
# =============================================================================

async def _exchange_code_for_tokens(
    code: str,
    code_verifier: str,
) -> Dict[str, Any]:
    """
    POST to OpenAI token endpoint to exchange authorization code for tokens.

    Returns dict with: access_token, refresh_token, expires_in
    """
    payload = {
        "grant_type": "authorization_code",
        "client_id": OPENAI_CLIENT_ID,
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": OPENAI_REDIRECT_URI,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            OPENAI_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        detail = resp.text[:500]
        raise HTTPException(
            status_code=502,
            detail=f"OpenAI token exchange failed ({resp.status_code}): {detail}",
        )
    data = resp.json()
    for field in ("access_token", "refresh_token", "expires_in"):
        if field not in data:
            raise HTTPException(
                status_code=502,
                detail=f"OpenAI token response missing '{field}'",
            )
    return data


async def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    """
    Refresh an expired access token using the refresh_token.

    Returns dict with: access_token, refresh_token, expires_in
    """
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OPENAI_CLIENT_ID,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            OPENAI_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"OpenAI token refresh failed ({resp.status_code}): {resp.text[:500]}",
        )
    return resp.json()


# =============================================================================
# Router & endpoints
# =============================================================================

router = APIRouter(prefix="/codex", tags=["codex-setup"])


class CodexSetupCallbackBody(BaseModel):
    """Body for exchanging the OAuth code after user login."""
    code: str  # Full redirect URL, code#state, querystring, or bare code
    session_id: str  # Must match the session_id from the SSE setup stream


def _normalize_callback_code(raw_value: str) -> tuple[Optional[str], Optional[str]]:
    """
    Parse multiple callback formats and return (code, state).

    Accepted formats:
      - Full redirect URL:  http://localhost:1455/auth/callback?code=X&state=Y
      - Query string:       code=X&state=Y
      - Hash format:        CODE#STATE
      - Bare code:          CODE
    """
    value = (raw_value or "").strip()
    if not value:
        return None, None

    # Full URL
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        params = parse_qs(parsed.query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        return code, state

    # Query string format
    if "code=" in value:
        params = parse_qs(value)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        return code, state

    # code#state format
    if "#" in value:
        parts = value.split("#", 1)
        return parts[0], parts[1]

    # Bare code
    return value, None


@router.post("/setup")
async def codex_setup():
    """
    Start the Codex OAuth PKCE flow.

    Returns an SSE stream that emits:
      - {type: "session", session_id: "..."}        session created
      - {type: "auth_url", url: "...", state: "..."}  user must open this URL
      - {type: "waiting"}                            waiting for callback
      - {type: "error", error: "..."}                on failure
      - {type: "done", ...}                          on timeout (callback completes separately)
    """
    async def generate() -> AsyncGenerator[str, None]:
        session_id = str(uuid.uuid4())
        print(f"[codex-setup] session start (session_id={session_id})")

        try:
            # Generate PKCE pair and state
            code_verifier, code_challenge = _generate_pkce()
            state = _generate_state()

            # Store session
            async with _setup_lock:
                _sessions[session_id] = {
                    "code_verifier": code_verifier,
                    "state": state,
                    "created_at": time.time(),
                    "status": "waiting",
                }

            # Build authorization URL
            auth_params = {
                "response_type": "code",
                "client_id": OPENAI_CLIENT_ID,
                "redirect_uri": OPENAI_REDIRECT_URI,
                "scope": OPENAI_SCOPES,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
            }
            auth_url = f"{OPENAI_AUTHORIZE_URL}?{urlencode(auth_params)}"

            # Emit session
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"

            # Emit auth URL
            yield f"data: {json.dumps({'type': 'auth_url', 'url': auth_url, 'state': state})}\n\n"
            print(f"[codex-setup] auth URL generated (state={state[:8]}...)")

            # Emit waiting status
            yield f"data: {json.dumps({'type': 'waiting', 'message': 'Open the URL, log in, then paste the redirect URL into the callback endpoint.'})}\n\n"

            # Keep SSE alive with heartbeats until timeout or session completes
            deadline = time.monotonic() + CODEX_SETUP_TIMEOUT_SECONDS
            heartbeat_interval = 15  # seconds

            while time.monotonic() < deadline:
                await asyncio.sleep(heartbeat_interval)

                async with _setup_lock:
                    session = _sessions.get(session_id)

                if session is None:
                    # Session was cleaned up (callback succeeded)
                    print(f"[codex-setup] session completed (session_id={session_id})")
                    yield f"data: {json.dumps({'type': 'done', 'status': 'completed'})}\n\n"
                    return

                if session.get("status") == "completed":
                    yield f"data: {json.dumps({'type': 'done', 'status': 'completed'})}\n\n"
                    return

                # Heartbeat
                yield ": heartbeat\n\n"

            # Timeout — clean up session
            async with _setup_lock:
                _sessions.pop(session_id, None)
            print(f"[codex-setup] session timed out (session_id={session_id})")
            yield f"data: {json.dumps({'type': 'error', 'error': 'Setup timed out'})}\n\n"

        except Exception as e:
            async with _setup_lock:
                _sessions.pop(session_id, None)
            print(f"[codex-setup] unexpected error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/setup/callback")
async def codex_setup_callback(body: CodexSetupCallbackBody):
    """
    Complete the Codex OAuth flow by exchanging the authorization code for tokens.

    The user pastes the redirect URL (or code) from the browser after logging in.
    This endpoint:
      1. Validates the session and extracts code + state
      2. Exchanges the code for access_token + refresh_token via OpenAI
      3. Parses JWT to extract email / profile identity
      4. Writes credentials to ~/.openclaw/ files
      5. Persists access token to .env files
    """
    print(f"[codex-setup] callback received (session_id={body.session_id})")

    async with _setup_lock:
        session = _sessions.get(body.session_id)

    if session is None:
        raise HTTPException(
            status_code=409,
            detail="No active session with this ID. Start one with POST /codex/setup first.",
        )

    if session.get("status") == "completed":
        raise HTTPException(
            status_code=409,
            detail="This session has already been completed.",
        )

    # Parse the callback code
    code, state = _normalize_callback_code(body.code)

    if not code:
        raise HTTPException(
            status_code=400,
            detail="Could not extract authorization code from the provided value.",
        )

    # Validate state if present in callback
    if state and state != session["state"]:
        raise HTTPException(
            status_code=400,
            detail=f"State mismatch. Expected '{session['state'][:8]}...' but got '{state[:8]}...'.",
        )

    # Exchange code for tokens
    print(f"[codex-setup] exchanging code for tokens (code_len={len(code)})")
    token_data = await _exchange_code_for_tokens(
        code=code,
        code_verifier=session["code_verifier"],
    )

    access_token = token_data["access_token"]
    refresh_token = token_data["refresh_token"]
    expires_in = token_data["expires_in"]
    expires_at = int(time.time() * 1000) + (expires_in * 1000)  # ms timestamp

    # Resolve identity from JWT
    identity = resolve_codex_auth_identity(access_token)
    email = identity["email"]
    profile_name = identity["profile_name"]
    account_id = identity["account_id"]

    print(f"[codex-setup] token exchange success (email={email}, profile={profile_name}, expires_in={expires_in}s)")

    # Write credentials to ~/.openclaw/ files
    try:
        write_auth_credentials(
            profile_name=profile_name,
            access=access_token,
            refresh=refresh_token,
            expires=expires_at,
            email=email,
        )
        print(f"[codex-setup] credentials written to {_resolve_auth_store_path()}")
    except OSError as e:
        print(f"[codex-setup] WARNING: failed to write auth credentials: {e}")

    # Persist to .env files
    _persist_token_to_env_files(access_token)
    for key in _CODEX_TOKEN_ENV_KEYS:
        os.environ[key] = access_token
    print("[codex-setup] token persisted to .env files")

    # Mark session as completed and clean up
    async with _setup_lock:
        if body.session_id in _sessions:
            _sessions[body.session_id]["status"] = "completed"
        # Clean up after a short delay (let SSE stream detect completion)
        async def _deferred_cleanup():
            await asyncio.sleep(5)
            async with _setup_lock:
                _sessions.pop(body.session_id, None)
        asyncio.create_task(_deferred_cleanup())

    return {
        "ok": True,
        "message": "Codex OAuth setup completed successfully",
        "session_id": body.session_id,
        "email": email,
        "profile_name": profile_name,
        "account_id": account_id,
        "expires_in": expires_in,
        "expires_at": expires_at,
        "auth_store_path": _resolve_auth_store_path(),
        "config_path": _resolve_config_path(),
    }


@router.get("/setup/status/{session_id}")
async def codex_setup_status(session_id: str):
    """Check the status of an active setup session."""
    async with _setup_lock:
        session = _sessions.get(session_id)

    if session is None:
        return {"active": False, "session_id": session_id}

    return {
        "active": True,
        "session_id": session_id,
        "status": session.get("status", "unknown"),
        "created_at": session.get("created_at"),
        "age_seconds": int(time.time() - session.get("created_at", time.time())),
    }
