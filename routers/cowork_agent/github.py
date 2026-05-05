"""
REST routes for the GitHub connector (PAT approach).

Endpoints:
  POST /api/connectors/github/token       — receive & validate a PAT
  GET  /api/connectors/github/status      — current connection status
  POST /api/connectors/github/disconnect   — delete stored token
  POST /api/connectors/github/reconnect    — re-validate stored token
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.cowork_agent.github_connector import (
    delete_github_token,
    get_github_token,
    get_status,
    save_github_token,
    validate_token,
)

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# POST /api/connectors/github/token
# ---------------------------------------------------------------------------

class TokenBody(BaseModel):
    token: str


@router.post("/api/connectors/github/token")
async def submit_github_token(body: TokenBody) -> JSONResponse:
    """Validate a GitHub PAT, store it, and return the connection status."""
    token = body.token.strip()
    if not token:
        raise HTTPException(400, detail="Token cannot be empty.")

    # Quick format check
    if not (
        token.startswith("ghp_")
        or token.startswith("github_pat_")
        or token.startswith("gho_")
        or len(token) >= 30
    ):
        raise HTTPException(400, detail="This doesn't look like a valid GitHub token.")

    # Validate against GitHub API
    result = await validate_token(token)

    if result.get("valid"):
        save_github_token(token)
        log.info("GitHub connected as @%s", result.get("username"))
        return JSONResponse({
            "status": "connected",
            "username": result.get("username", ""),
            "name": result.get("name", ""),
            "avatar_url": result.get("avatar_url", ""),
            "scopes": result.get("scopes", ""),
        })
    else:
        return JSONResponse(
            {"status": result["status"], "error": result.get("error", "Validation failed.")},
            status_code=400 if result["status"] == "needs_auth" else 502,
        )


# ---------------------------------------------------------------------------
# GET /api/connectors/github/status
# ---------------------------------------------------------------------------

@router.get("/api/connectors/github/status")
async def github_status() -> JSONResponse:
    """Return the current GitHub connector status."""
    status = await get_status()
    return JSONResponse(status)


# ---------------------------------------------------------------------------
# POST /api/connectors/github/disconnect
# ---------------------------------------------------------------------------

@router.post("/api/connectors/github/disconnect")
async def disconnect_github() -> JSONResponse:
    """Delete the stored GitHub token and clear the connection."""
    delete_github_token()
    return JSONResponse({"status": "needs_auth"})


# ---------------------------------------------------------------------------
# POST /api/connectors/github/reconnect
# ---------------------------------------------------------------------------

@router.post("/api/connectors/github/reconnect")
async def reconnect_github() -> JSONResponse:
    """Re-validate the stored token and return the new status."""
    token = get_github_token()
    if not token:
        return JSONResponse({"status": "needs_auth", "error": "No token stored."})

    result = await validate_token(token)
    if result.get("valid"):
        return JSONResponse({
            "status": "connected",
            "username": result.get("username", ""),
            "name": result.get("name", ""),
            "avatar_url": result.get("avatar_url", ""),
            "scopes": result.get("scopes", ""),
        })
    else:
        return JSONResponse(
            {"status": result["status"], "error": result.get("error", "")},
            status_code=502,
        )
