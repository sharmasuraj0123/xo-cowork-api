"""
Chat prompt / streaming / abort routes.

The `/api/chat/prompt` endpoint decides whether we're starting a new OpenClaw
session (→ prefetched SSE flow) or continuing an existing one (→ direct
streaming). `/api/chat/stream/{id}` is the SSE consumer that dispatches to the
right generator based on the stored stream metadata.
"""

import asyncio
import json
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from services.cowork_agent.chat_state import active_streams
from services.cowork_agent.sessions_io import find_session_key
from services.cowork_agent.streaming import (
    create_new_session,
    emit_prefetched_sse,
    find_session_id_by_key,
    openclaw_agent_id_from_prompt_body,
    stream_openclaw_to_sse,
)

router = APIRouter()


@router.post("/api/chat/prompt")
async def chat_prompt(request: Request):
    body = await request.json()
    text = body.get("text", "").strip()
    session_id = body.get("session_id")

    if not text:
        return JSONResponse(status_code=400, content={"detail": "Empty message"})

    # New session: kick off create_new_session as a background task.
    # Poll for the session_id (OpenClaw creates it quickly) so the frontend
    # can navigate to /c/{session_id} immediately.
    # Uses stream=False to avoid OpenClaw's bootstrap-duplicate issue.
    if not session_id:
        oc_agent = openclaw_agent_id_from_prompt_body(body)
        session_key = f"agent:{oc_agent}:web:{uuid.uuid4().hex[:8]}"
        task = asyncio.create_task(create_new_session(text, session_key=session_key))

        # Poll for session_id — OpenClaw writes it to sessions.json quickly
        new_session_id = None
        for _ in range(20):
            await asyncio.sleep(1.0)
            new_session_id = find_session_id_by_key(session_key)
            if new_session_id:
                break

        stream_id = str(uuid.uuid4())
        active_streams[stream_id] = {
            "task": task,
            "prefetched": True,
        }
        return {"stream_id": stream_id, "session_id": new_session_id}

    # Existing session: look up the session key and stream
    session_key = find_session_key(session_id)
    if not session_key:
        return JSONResponse(status_code=404, content={"detail": "Session not found"})

    stream_id = str(uuid.uuid4())
    active_streams[stream_id] = {
        "session_id": session_id,
        "text": text,
        "session_key": session_key,
    }

    return {"stream_id": stream_id, "session_id": session_id}


@router.get("/api/chat/stream/{stream_id}")
async def chat_stream(stream_id: str):
    stream_info = active_streams.get(stream_id)
    if not stream_info:
        async def not_found():
            yield f"id: 1\nevent: error\ndata: {json.dumps({'error_message': 'Stream not found'})}\n\n"
        generator = not_found()
    elif stream_info.get("prefetched"):
        generator = emit_prefetched_sse(stream_id)
    else:
        generator = stream_openclaw_to_sse(stream_id)

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/chat/abort")
async def chat_abort(request: Request):
    body = await request.json()
    stream_id = body.get("stream_id")
    if stream_id:
        active_streams.pop(stream_id, None)
    return {"ok": True}


@router.post("/api/chat/respond")
async def chat_respond(request: Request):
    return {"ok": True}
