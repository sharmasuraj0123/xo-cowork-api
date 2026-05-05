"""
REST routes for the Google Drive connector.

Exposes:
  GET  /api/connectors/gdrive/remotes
  POST /api/connectors/gdrive/remotes
  GET  /api/connectors/gdrive/sessions/{session_id}
  DELETE /api/connectors/gdrive/remotes/{name}
  POST /api/connectors/gdrive/sessions/{session_id}/cancel
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.cowork_agent.gdrive_rclone import (
    cancel_session,
    create_remote_session,
    delete_remote,
    get_session,
    list_drive_remotes,
    rclone_available,
    validate_remote_name,
)

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# GET /api/connectors/gdrive/remotes
# ---------------------------------------------------------------------------

@router.get("/api/connectors/gdrive/remotes")
async def get_gdrive_remotes() -> JSONResponse:
    if not await rclone_available():
        raise HTTPException(
            503,
            detail="Could not reach rclone daemon. Check that rclone is installed and running.",
        )
    remotes = await list_drive_remotes()
    return JSONResponse({"remotes": remotes})


# ---------------------------------------------------------------------------
# POST /api/connectors/gdrive/remotes
# ---------------------------------------------------------------------------

class CreateRemoteBody(BaseModel):
    name: str
    force: bool = False


@router.post("/api/connectors/gdrive/remotes")
async def create_gdrive_remote(body: CreateRemoteBody) -> JSONResponse:
    if not await rclone_available():
        raise HTTPException(503, detail="Could not reach rclone daemon.")

    # Validate name
    err = await validate_remote_name(body.name)
    if err:
        raise HTTPException(400, detail=err)

    try:
        session = await create_remote_session(body.name, force=body.force)
    except RuntimeError as exc:
        # Concurrent flow
        raise HTTPException(409, detail=str(exc)) from exc

    return JSONResponse(
        {"session_id": session.session_id, "status": "pending"},
        status_code=202,
    )


# ---------------------------------------------------------------------------
# GET /api/connectors/gdrive/sessions/{session_id}
# ---------------------------------------------------------------------------

@router.get("/api/connectors/gdrive/sessions/{session_id}")
async def poll_gdrive_session(session_id: str) -> JSONResponse:
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found or expired.")

    payload: dict = {"status": session.status}
    if session.status == "awaiting_oauth" and session.auth_url:
        payload["auth_url"] = session.auth_url
        payload["needs_manual_code"] = session.needs_manual_code
    if session.status == "completed":
        payload["remote_name"] = session.remote_name
    if session.status == "failed" and session.error:
        payload["error"] = session.error

    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# DELETE /api/connectors/gdrive/remotes/{name}
# ---------------------------------------------------------------------------

@router.delete("/api/connectors/gdrive/remotes/{name}")
async def remove_gdrive_remote(name: str) -> JSONResponse:
    if not await rclone_available():
        raise HTTPException(503, detail="Could not reach rclone daemon.")
    try:
        await delete_remote(name)
    except Exception as exc:
        raise HTTPException(500, detail=str(exc)) from exc
    return JSONResponse(None, status_code=204)


# ---------------------------------------------------------------------------
# POST /api/connectors/gdrive/sessions/{session_id}/cancel
# ---------------------------------------------------------------------------

@router.post("/api/connectors/gdrive/sessions/{session_id}/cancel")
async def cancel_gdrive_session(session_id: str) -> JSONResponse:
    await cancel_session(session_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# POST /api/connectors/gdrive/sessions/{session_id}/submit
# Body: {"code": "<paste from URL bar>"}
# ---------------------------------------------------------------------------

class SubmitCodeBody(BaseModel):
    code: str


@router.post("/api/connectors/gdrive/sessions/{session_id}/submit")
async def submit_gdrive_code(session_id: str, body: SubmitCodeBody) -> JSONResponse:
    """Receive the redirect URL / verification code the user pasted from the browser."""
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found or expired.")
    if session.status != "awaiting_oauth":
        raise HTTPException(400, detail="Session is not waiting for a verification code.")

    # Accept either the full redirect URL or just the bare code
    code = body.code.strip()
    import re
    m = re.search(r"[?&]code=([^&]+)", code)
    if m:
        code = m.group(1)

    session.verification_input = code
    return JSONResponse({"ok": True})
