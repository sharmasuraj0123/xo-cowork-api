"""
GitHub connector — PAT (Personal Access Token) approach.

No environment variables. No OAuth app. The user generates a fine-grained
PAT on GitHub, pastes it into the UI, and we store it in a local JSON file.

Token file: <project_root>/mcp-tokens.json  (.gitignored)
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Literal

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
TOKEN_FILE = _PROJECT_ROOT / "mcp-tokens.json"

GITHUB_API = "https://api.github.com"

# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------

def _read_tokens() -> dict[str, Any]:
    """Read the full mcp-tokens.json file."""
    if not TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s: %s", TOKEN_FILE, exc)
        return {}


def _write_tokens(data: dict[str, Any]) -> None:
    """Write the full mcp-tokens.json file."""
    TOKEN_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def get_github_token() -> str | None:
    """Return the stored GitHub PAT, or None."""
    entry = _read_tokens().get("github")
    if not entry:
        return None
    return entry.get("access_token") or None


def save_github_token(token: str) -> None:
    """Save a GitHub PAT to mcp-tokens.json."""
    data = _read_tokens()
    data["github"] = {
        "access_token": token,
        "refresh_token": None,
        "expires_at": 0,
        "token_type": "Bearer",
        "scope": "",
    }
    _write_tokens(data)
    log.info("GitHub token saved to %s", TOKEN_FILE)


def delete_github_token() -> None:
    """Remove the GitHub entry from mcp-tokens.json."""
    data = _read_tokens()
    data.pop("github", None)
    _write_tokens(data)
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

    Returns a dict with `status`, and optionally `username`, `avatar_url`, `scopes`.
    """
    token = get_github_token()
    if not token:
        return {"status": "needs_auth"}

    result = await validate_token(token)
    return result
