"""
Vercel connector — supports both API Token and OAuth 2.1 (Authorization Code + PKCE).

API-token flow: user generates a token at https://vercel.com/account/tokens and pastes it.
OAuth 2.1 flow: initiated via /api/connectors/vercel/oauth/start; Vercel redirects back
  to /callback where the authorization code is exchanged for tokens (with PKCE).

Token file: <project_root>/mcp-tokens.json  (.gitignored)
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
TOKEN_FILE = _PROJECT_ROOT / "mcp-tokens.json"

VERCEL_USER_URL = "https://api.vercel.com/v2/user"
VERCEL_OAUTH_AUTHORIZE_URL = "https://vercel.com/oauth/authorize"
VERCEL_OAUTH_TOKEN_URL = "https://api.vercel.com/login/oauth/token"
VERCEL_OAUTH_REGISTER_URL = "https://api.vercel.com/login/oauth/register"

# In-memory store for pending OAuth flows: state → {code_verifier, redirect_uri}
_pending_oauth: dict[str, dict[str, str]] = {}


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def _generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Token storage
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


def get_oauth_client() -> dict[str, Any] | None:
    """Return the registered OAuth client credentials from mcp-tokens.json, or None."""
    return _read_tokens().get("vercel_client")


async def register_oauth_client(redirect_uri: str) -> dict[str, Any]:
    """
    Dynamically register a new OAuth 2.1 client with Vercel (RFC 7591).

    POSTs the client metadata to Vercel's registration endpoint and persists
    the returned client_id (plus client_secret if any) under `vercel_client`
    in mcp-tokens.json. Subsequent calls to get_oauth_client() will return it.
    """
    metadata = {
        "client_name": "xo-cowork",
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(
                VERCEL_OAUTH_REGISTER_URL,
                json=metadata,
                headers={"Content-Type": "application/json"},
            )
    except httpx.TimeoutException as exc:
        raise RuntimeError("Timed out registering Vercel OAuth client.") from exc
    except Exception as exc:
        raise RuntimeError(f"Could not reach Vercel registration endpoint: {exc}") from exc

    if resp.status_code not in (200, 201):
        try:
            body = resp.json()
            err = body.get("error_description") or body.get("error") or resp.text
        except Exception:
            err = resp.text
        log.error("Vercel DCR failed %s: %s", resp.status_code, err)
        raise RuntimeError(f"Vercel OAuth client registration failed: {err}")

    registered = resp.json()
    client_id = registered.get("client_id")
    if not client_id:
        raise RuntimeError("Vercel registration response missing client_id.")

    entry: dict[str, Any] = {
        "client_id": client_id,
        "token_endpoint_auth_method": metadata["token_endpoint_auth_method"],
        "grant_types": metadata["grant_types"],
        "response_types": metadata["response_types"],
        "client_name": metadata["client_name"],
        "redirect_uris": metadata["redirect_uris"],
    }
    if registered.get("client_secret"):
        entry["client_secret"] = registered["client_secret"]

    data = _read_tokens()
    data["vercel_client"] = entry
    _write_tokens(data)
    log.info("Registered new Vercel OAuth client (client_id=%s)", client_id)
    return entry


async def ensure_oauth_client(redirect_uri: str) -> dict[str, Any]:
    """
    Return the existing OAuth client, or register a new one via DCR if absent.
    Idempotent: only one registration round-trip per fresh checkout.
    """
    existing = get_oauth_client()
    if existing and existing.get("client_id"):
        return existing
    return await register_oauth_client(redirect_uri)


def get_vercel_token() -> str | None:
    """Return the stored access_token (API token or OAuth), or None."""
    entry = _read_tokens().get("vercel")
    if not entry:
        return None
    return entry.get("access_token") or None


def save_vercel_token(token: str, username: str = "", name: str = "") -> None:
    """Save a manually-provided API token (no expiry, no refresh_token)."""
    data = _read_tokens()
    data["vercel"] = {
        "access_token": token,
        "token_type": "Bearer",
        "auth_method": "api_token",
        "username": username,
        "name": name,
    }
    _write_tokens(data)
    log.info("Vercel API token saved to %s", TOKEN_FILE)


def save_oauth_tokens(
    access_token: str,
    refresh_token: str | None,
    expires_in: int,
    username: str = "",
    name: str = "",
) -> None:
    """Persist OAuth 2.1 access + refresh tokens."""
    data = _read_tokens()
    data["vercel"] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(time.time()) + expires_in,
        "token_type": "Bearer",
        "auth_method": "oauth",
        "username": username,
        "name": name,
    }
    _write_tokens(data)
    log.info("Vercel OAuth tokens saved to %s", TOKEN_FILE)


def delete_vercel_token() -> None:
    """Remove the user's Vercel tokens. Preserves the vercel_client OAuth registration."""
    data = _read_tokens()
    data.pop("vercel", None)
    _write_tokens(data)
    log.info("Vercel token removed from %s", TOKEN_FILE)


# ---------------------------------------------------------------------------
# OAuth 2.1 Authorization Code + PKCE flow
# ---------------------------------------------------------------------------

def start_oauth_flow(redirect_uri: str) -> dict[str, str]:
    """
    Create a new OAuth 2.1 PKCE authorization request.

    Returns {"auth_url": "...", "state": "..."}.
    The caller must send the user to auth_url and later call
    exchange_code_for_tokens() with the returned code + state.
    """
    client = get_oauth_client()
    if not client:
        raise ValueError("No Vercel OAuth client registered in mcp-tokens.json.")

    state = secrets.token_urlsafe(32)
    code_verifier = _generate_code_verifier()
    code_challenge = _generate_code_challenge(code_verifier)

    _pending_oauth[state] = {
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }

    params = {
        "client_id": client["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": "openid email profile",
    }
    auth_url = VERCEL_OAUTH_AUTHORIZE_URL + "?" + urlencode(params)
    return {"auth_url": auth_url, "state": state}


async def exchange_code_for_tokens(code: str, state: str) -> dict[str, Any]:
    """
    Exchange an authorization code for access + refresh tokens (PKCE).

    Consumes the pending state entry so each code can only be used once.
    On success, persists tokens via save_oauth_tokens() and returns
    {"valid": True, "status": "connected", "username": ..., "name": ..., "auth_method": "oauth"}.
    On failure, returns {"valid": False, "error": "..."}.
    """
    pending = _pending_oauth.pop(state, None)
    if pending is None:
        return {"valid": False, "error": "Invalid or expired OAuth state parameter."}

    client = get_oauth_client()
    if not client:
        return {"valid": False, "error": "No Vercel OAuth client registered."}

    form_data = {
        "client_id": client["client_id"],
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "code_verifier": pending["code_verifier"],
        "grant_type": "authorization_code",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(
                VERCEL_OAUTH_TOKEN_URL,
                data=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if resp.status_code != 200:
            try:
                body = resp.json()
                vercel_error = body.get("error_description") or body.get("error") or resp.text
            except Exception:
                vercel_error = resp.text
            log.error("Vercel token exchange failed %s: %s", resp.status_code, vercel_error)
            return {
                "valid": False,
                "error": f"Token exchange failed: {vercel_error}",
            }

        tokens = resp.json()
        access_token = tokens.get("access_token")
        if not access_token:
            return {"valid": False, "error": "No access_token in Vercel token response."}

        user_info = await validate_token(access_token)
        if not user_info.get("valid"):
            # Exchange succeeded so the token IS valid for MCP tool calls.
            # /v2/user may reject it if the scope doesn't cover user-info reads.
            # Save and connect with empty display name rather than failing the flow.
            log.warning(
                "Vercel /v2/user rejected the OAuth token (%s) — saving anyway",
                user_info.get("error"),
            )
            save_oauth_tokens(
                access_token=access_token,
                refresh_token=tokens.get("refresh_token"),
                expires_in=tokens.get("expires_in", 3600),
                username="",
                name="",
            )
            return {
                "valid": True,
                "status": "connected",
                "username": "",
                "name": "",
                "auth_method": "oauth",
            }

        save_oauth_tokens(
            access_token=access_token,
            refresh_token=tokens.get("refresh_token"),
            expires_in=tokens.get("expires_in", 3600),
            username=user_info.get("username", ""),
            name=user_info.get("name", ""),
        )
        return {
            "valid": True,
            "status": "connected",
            "username": user_info.get("username", ""),
            "name": user_info.get("name", ""),
            "auth_method": "oauth",
        }

    except httpx.TimeoutException:
        return {"valid": False, "error": "Timed out during token exchange with Vercel."}
    except Exception as exc:
        return {"valid": False, "error": f"Token exchange error: {exc}"}


async def refresh_oauth_token() -> str | None:
    """
    Use the stored refresh_token to obtain a new access_token.

    Updates mcp-tokens.json in-place on success.
    Returns the new access_token, or None if refresh is not possible.
    """
    data = _read_tokens()
    entry = data.get("vercel", {})
    refresh_token = entry.get("refresh_token")
    if not refresh_token:
        return None

    client = get_oauth_client()
    if not client:
        return None

    form_data = {
        "client_id": client["client_id"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(
                VERCEL_OAUTH_TOKEN_URL,
                data=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if resp.status_code != 200:
            log.warning("Vercel OAuth refresh failed: HTTP %s", resp.status_code)
            return None

        tokens = resp.json()
        new_access_token = tokens.get("access_token")
        if not new_access_token:
            return None

        entry["access_token"] = new_access_token
        entry["expires_at"] = int(time.time()) + tokens.get("expires_in", 3600)
        if tokens.get("refresh_token"):
            entry["refresh_token"] = tokens["refresh_token"]
        data["vercel"] = entry
        _write_tokens(data)
        log.info("Vercel OAuth token refreshed successfully")
        return new_access_token

    except Exception as exc:
        log.warning("Vercel OAuth token refresh error: %s", exc)
        return None


async def get_valid_access_token() -> str | None:
    """
    Return a valid access token, auto-refreshing OAuth tokens that are near expiry.

    For API tokens (no expires_at) the stored token is returned as-is.
    Returns None if no token is stored or refresh fails.
    """
    data = _read_tokens()
    entry = data.get("vercel", {})
    access_token = entry.get("access_token")
    if not access_token:
        return None

    expires_at = entry.get("expires_at", 0)
    if expires_at and time.time() >= expires_at - 60:
        log.info("Vercel OAuth token near expiry, attempting refresh")
        refreshed = await refresh_oauth_token()
        return refreshed

    return access_token


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

VercelStatus = Literal["connected", "needs_auth", "failed"]


async def validate_token(token: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                VERCEL_USER_URL,
                headers={"Authorization": f"Bearer {token}"},
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
            return {"valid": False, "status": "needs_auth", "error": "Token is invalid or revoked."}
        else:
            return {"valid": False, "status": "failed", "error": f"Vercel returned HTTP {resp.status_code}."}

    except httpx.TimeoutException:
        return {"valid": False, "status": "failed", "error": "Timed out connecting to Vercel."}
    except Exception as exc:
        return {"valid": False, "status": "failed", "error": f"Could not connect to Vercel: {exc}"}


async def get_status() -> dict[str, Any]:
    data = _read_tokens()
    entry = data.get("vercel", {})

    # OAuth tokens are MCP-scoped and can't be validated via /v2/user.
    # Trust the stored entry as long as the token exists and hasn't expired.
    if entry.get("auth_method") == "oauth":
        token = await get_valid_access_token()
        if not token:
            return {"status": "needs_auth"}
        return {
            "valid": True,
            "status": "connected",
            "username": entry.get("username", ""),
            "name": entry.get("name", ""),
            "email": entry.get("email", ""),
            "auth_method": "oauth",
        }

    # API token: validate live against /v2/user.
    token = await get_valid_access_token()
    if not token:
        return {"status": "needs_auth"}
    result = await validate_token(token)
    if result.get("valid"):
        result["auth_method"] = entry.get("auth_method", "api_token")
    return result
