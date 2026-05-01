"""
Manus AI connector — API key approach.

No OAuth. User generates an API key on manus.im and pastes it.
We store it in mcp-tokens.json and validate via task.list.

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
# Paths & URLs
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
TOKEN_FILE = _PROJECT_ROOT / "mcp-tokens.json"

MANUS_API = "https://api.manus.ai/v2"

# ---------------------------------------------------------------------------
# Token storage (shared via mcp-tokens.json)
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


def get_manus_key() -> str | None:
    """Return the stored Manus API key, or None."""
    entry = _read_tokens().get("manus")
    if not entry:
        return None
    return entry.get("api_key") or None


def save_manus_key(api_key: str) -> None:
    """Save a Manus API key to mcp-tokens.json."""
    data = _read_tokens()
    data["manus"] = {
        "api_key": api_key,
        "expires_at": 0,
        "token_type": "api_key",
    }
    _write_tokens(data)
    log.info("Manus API key saved to %s", TOKEN_FILE)


def delete_manus_key() -> None:
    """Remove the Manus entry from mcp-tokens.json."""
    data = _read_tokens()
    data.pop("manus", None)
    _write_tokens(data)
    log.info("Manus API key removed from %s", TOKEN_FILE)


# ---------------------------------------------------------------------------
# API key validation
# ---------------------------------------------------------------------------

ManusStatus = Literal["connected", "needs_auth", "failed"]


async def validate_key(api_key: str) -> dict[str, Any]:
    """
    Validate a Manus API key by calling task.list.

    Returns:
        {
            "valid": True/False,
            "status": "connected" | "needs_auth" | "failed",
            "error": "...",  # if not valid
        }
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{MANUS_API}/task.list",
                headers={
                    "x-manus-api-key": api_key,
                    "Content-Type": "application/json",
                },
                params={"limit": 1},
            )

        body = resp.json()

        if resp.status_code == 200 and body.get("ok"):
            return {
                "valid": True,
                "status": "connected",
            }
        elif resp.status_code in (401, 403) or body.get("error", {}).get("code") == "permission_denied":
            return {
                "valid": False,
                "status": "needs_auth",
                "error": "API key is invalid or revoked.",
            }
        else:
            error_msg = body.get("error", {}).get("message", f"HTTP {resp.status_code}")
            return {
                "valid": False,
                "status": "failed",
                "error": f"Manus API error: {error_msg}",
            }

    except httpx.TimeoutException:
        return {
            "valid": False,
            "status": "failed",
            "error": "Timed out connecting to Manus. Check your internet.",
        }
    except Exception as exc:
        return {
            "valid": False,
            "status": "failed",
            "error": f"Could not connect to Manus: {exc}",
        }


async def get_status() -> dict[str, Any]:
    """Compute the current Manus connector status."""
    api_key = get_manus_key()
    if not api_key:
        return {"status": "needs_auth"}

    result = await validate_key(api_key)
    return result
