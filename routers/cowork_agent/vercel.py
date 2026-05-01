"""
REST routes for the Vercel connector (OAuth 2.1 + PKCE).

Endpoints:
  POST /api/connectors/vercel/connect       — start OAuth flow
  GET  /api/connectors/vercel/sessions/{id}  — poll session status
  POST /api/connectors/vercel/sessions/{id}/cancel — cancel OAuth flow
  GET  /api/connectors/vercel/status         — current connection status
  POST /api/connectors/vercel/disconnect     — delete stored token
  POST /api/connectors/vercel/reconnect      — re-validate stored token
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from services.cowork_agent.vercel_connector import (
    cancel_session,
    create_oauth_session,
    delete_vercel_token,
    get_session,
    get_status,
    get_vercel_token,
    validate_vercel_token,
)

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# POST /api/connectors/vercel/connect
# ---------------------------------------------------------------------------

@router.post("/api/connectors/vercel/connect")
async def start_vercel_oauth() -> JSONResponse:
    """Start a new Vercel OAuth flow."""
    try:
        session = await create_oauth_session()
    except RuntimeError as exc:
        raise HTTPException(409, detail=str(exc)) from exc

    return JSONResponse(
        {"session_id": session.session_id, "status": "pending"},
        status_code=202,
    )


# ---------------------------------------------------------------------------
# GET /api/connectors/vercel/sessions/{session_id}
# ---------------------------------------------------------------------------

@router.get("/api/connectors/vercel/sessions/{session_id}")
async def poll_vercel_session(session_id: str) -> JSONResponse:
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found or expired.")

    payload: dict = {"status": session.status}
    if session.status == "awaiting_oauth" and session.auth_url:
        payload["auth_url"] = session.auth_url
    if session.status == "failed" and session.error:
        payload["error"] = session.error

    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# POST /api/connectors/vercel/sessions/{session_id}/cancel
# ---------------------------------------------------------------------------

@router.post("/api/connectors/vercel/sessions/{session_id}/cancel")
async def cancel_vercel_session(session_id: str) -> JSONResponse:
    await cancel_session(session_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# GET /api/connectors/vercel/status
# ---------------------------------------------------------------------------

@router.get("/api/connectors/vercel/status")
async def vercel_status() -> JSONResponse:
    """Return the current Vercel connector status."""
    status = await get_status()
    return JSONResponse(status)


# ---------------------------------------------------------------------------
# POST /api/connectors/vercel/disconnect
# ---------------------------------------------------------------------------

@router.post("/api/connectors/vercel/disconnect")
async def disconnect_vercel() -> JSONResponse:
    delete_vercel_token()
    return JSONResponse({"status": "needs_auth"})


# ---------------------------------------------------------------------------
# POST /api/connectors/vercel/reconnect
# ---------------------------------------------------------------------------

@router.post("/api/connectors/vercel/reconnect")
async def reconnect_vercel() -> JSONResponse:
    """Re-validate the stored token and return the new status."""
    entry = get_vercel_token()
    if not entry:
        return JSONResponse({"status": "needs_auth", "error": "No token stored."})

    access_token = entry.get("access_token")
    if not access_token:
        return JSONResponse({"status": "needs_auth", "error": "No access token found."})

    result = await validate_vercel_token(access_token)
    if result.get("valid"):
        return JSONResponse({
            "status": "connected",
            "username": result.get("username", ""),
            "name": result.get("name", ""),
            "email": result.get("email", ""),
        })
    else:
        return JSONResponse(
            {"status": result["status"], "error": result.get("error", "")},
            status_code=502,
        )
