"""
REST routes for the GitHub connector.

Two authentication methods are supported in parallel — only one identity is
connected at a time, but the user can pick how they connect.

PAT method (paste a personal access token):
  POST /api/connectors/github/token       — receive & validate a PAT
  GET  /api/connectors/github/status      — current connection status
  POST /api/connectors/github/disconnect   — delete stored token
  POST /api/connectors/github/reconnect    — re-validate stored token

CLI method (`gh auth login` device flow):
  POST /api/connectors/github/cli/start    — spawn `gh auth login`, return device code
  POST /api/connectors/github/cli/poll     — poll until the user authorizes
  POST /api/connectors/github/cli/cancel   — abort an in-progress login
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.cowork_agent.connectors import github_cli_auth
from services.cowork_agent.connectors.github_connector import (
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
        save_github_token(token, auth_method="pat")
        log.info("GitHub connected as @%s (via PAT)", result.get("username"))
        return JSONResponse({
            "status": "connected",
            "auth_method": "pat",
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


# ---------------------------------------------------------------------------
# CLI flow — `gh auth login` device-flow
# ---------------------------------------------------------------------------

class CliSessionBody(BaseModel):
    session_id: str


@router.post("/api/connectors/github/cli/start")
async def cli_login_start() -> JSONResponse:
    """Spawn `gh auth login --web` and return the device code + verification URL.

    The frontend should display `user_code` and a clickable link to
    `verification_uri`, then poll `/cli/poll` until status flips to `completed`.
    """
    try:
        info = await github_cli_auth.start_login()
    except RuntimeError as exc:
        # Caller-actionable: gh missing, parse failure, concurrent session, etc.
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(info)


@router.post("/api/connectors/github/cli/poll")
async def cli_login_poll(body: CliSessionBody) -> JSONResponse:
    """Check the status of an in-progress CLI login.

    On `completed`, the token is validated against /user, persisted to
    mcp-tokens.json with `auth_method="cli"`, and the user profile is returned
    in the same shape as the PAT flow.
    """
    result = await github_cli_auth.poll_login(body.session_id)
    status = result.get("status")

    if status == "pending":
        return JSONResponse(result)

    if status == "not_found":
        raise HTTPException(
            status_code=404,
            detail="Unknown or expired CLI login session. Start a new one.",
        )

    if status == "completed":
        token = result["token"]
        validation = await validate_token(token)
        if not validation.get("valid"):
            return JSONResponse(
                {
                    "status": "failed",
                    "error": validation.get(
                        "error",
                        "GitHub CLI login completed but the token failed validation.",
                    ),
                },
                status_code=502,
            )
        save_github_token(token, auth_method="cli")
        log.info("GitHub connected as @%s (via gh CLI)", validation.get("username"))
        return JSONResponse({
            "status": "connected",
            "auth_method": "cli",
            "username": validation.get("username", ""),
            "name": validation.get("name", ""),
            "avatar_url": validation.get("avatar_url", ""),
            "scopes": validation.get("scopes", ""),
        })

    # status == "failed"
    return JSONResponse(
        {"status": "failed", "error": result.get("error", "CLI login failed.")},
        status_code=502,
    )


@router.post("/api/connectors/github/cli/cancel")
async def cli_login_cancel(body: CliSessionBody) -> JSONResponse:
    """Abort an in-progress CLI login."""
    result = await github_cli_auth.cancel_login(body.session_id)
    return JSONResponse(result)
