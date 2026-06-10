"""
GitHub connector — PAT (Personal Access Token) approach.

No environment variables. No OAuth app. The user generates a fine-grained
PAT on GitHub, pastes it into the UI, and we store it in a local JSON file.

Token file: <project_root>/mcp-tokens.json  (.gitignored)
"""

import logging
from typing import Any, Literal

import httpx

from .token_store import TOKEN_FILE, delete_entry, get_entry, set_entry

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# ---------------------------------------------------------------------------
# Token storage (provider key "github" in mcp-tokens.json, owned by token_store)
# ---------------------------------------------------------------------------

def get_github_token() -> str | None:
    """Return the stored GitHub access token, or None."""
    entry = get_entry("github")
    if not entry:
        return None
    return entry.get("access_token") or None


def get_github_auth_method() -> str | None:
    """Return the auth method used for the stored token: "pat", "cli", or None."""
    entry = get_entry("github")
    if not entry:
        return None
    # Pre-existing tokens (no auth_method field) are PATs.
    return entry.get("auth_method") or "pat"


def save_github_token(token: str, *, auth_method: str = "pat") -> None:
    """Save a GitHub access token to mcp-tokens.json.

    auth_method is "pat" (user-pasted PAT) or "cli" (from `gh auth login`).
    """
    set_entry("github", {
        "access_token": token,
        "refresh_token": None,
        "expires_at": 0,
        "token_type": "Bearer",
        "scope": "",
        "auth_method": auth_method,
    })
    log.info("GitHub token saved to %s (method=%s)", TOKEN_FILE, auth_method)


def delete_github_token() -> None:
    """Remove the GitHub entry from mcp-tokens.json."""
    delete_entry("github")
    log.info("GitHub token removed from %s", TOKEN_FILE)


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

GitHubStatus = Literal["connected", "needs_auth", "failed"]


async def validate_token(token: str) -> dict[str, Any]:
    """
    Validate a GitHub PAT by calling /user.

    Returns:
        {
            "valid": True/False,
            "status": "connected" | "needs_auth" | "failed",
            "username": "...",       # if valid
            "avatar_url": "...",     # if valid
            "scopes": "...",         # X-OAuth-Scopes header
            "error": "...",          # if not valid
        }
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{GITHUB_API}/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )

        if resp.status_code == 200:
            user = resp.json()
            scopes = resp.headers.get("x-oauth-scopes", "")
            return {
                "valid": True,
                "status": "connected",
                "username": user.get("login", ""),
                "name": user.get("name", ""),
                "avatar_url": user.get("avatar_url", ""),
                "scopes": scopes,
            }
        elif resp.status_code in (401, 403):
            return {
                "valid": False,
                "status": "needs_auth",
                "error": "Token is invalid or revoked.",
            }
        else:
            return {
                "valid": False,
                "status": "failed",
                "error": f"GitHub returned HTTP {resp.status_code}.",
            }

    except httpx.TimeoutException:
        return {
            "valid": False,
            "status": "failed",
            "error": "Timed out connecting to GitHub. Check your internet connection.",
        }
    except Exception as exc:
        return {
            "valid": False,
            "status": "failed",
            "error": f"Could not connect to GitHub: {exc}",
        }


async def get_status() -> dict[str, Any]:
    """
    Compute the current GitHub connector status.

    Returns a dict with `status`, and optionally `username`, `avatar_url`,
    `scopes`, and `auth_method` ("pat" | "cli") so the UI can show how the
    user is connected.
    """
    token = get_github_token()
    if not token:
        return {"status": "needs_auth"}

    result = await validate_token(token)
    method = get_github_auth_method()
    if method:
        result["auth_method"] = method
    return result
