"""
Chat prompt / streaming / abort routes.

The `/api/chat/prompt` endpoint decides whether we're starting a new OpenClaw
session (→ prefetched SSE flow) or continuing an existing one (→ direct
streaming). `/api/chat/stream/{id}` is the SSE consumer that dispatches to the
right generator based on the stored stream metadata.

For non-openclaw agents (AGENT_NAME env var or per-request agent_name), the
AgentDispatcher is used.
"""

import asyncio
import json
import os
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from services.cowork_agent.chat_state import active_streams

# Tracks recently-started streams so a fast reconnect (e.g. navigation-caused
# double-mount) gets a graceful done event rather than "Stream not found".
# Maps stream_id -> {session_id, started_at}
_recently_started: dict[str, dict] = {}
_RECENTLY_STARTED_TTL = 30  # seconds
from services.cowork_agent.sessions_io import find_session_key
from services.cowork_agent.streaming import (
    create_new_session,
    emit_prefetched_sse,
    find_session_id_by_key,
    openclaw_agent_id_from_prompt_body,
    stream_openclaw_to_sse,
)

router = APIRouter()

_AGENT_NAME = os.getenv("AGENT_NAME", "openclaw")


def _resolve_backend_for_session(session_id: str) -> str | None:
    """Return 'claude_code' if session_id belongs to a Claude Code session, else None."""
    # New index format: ~/claude-cowork/agents/*/sessions/sessions.json
    from services.cowork_agent.adapters.claude_code.adapter import find_session_key_for_session_id
    if find_session_key_for_session_id(session_id) is not None:
        return "claude_code"
    # Old format: ~/claude-cowork/{agent_id}/.sessions/{session_id}.json
    from services.cowork_agent.claude_sessions import load_session
    if load_session(session_id) is not None:
        return "claude_code"
    return None


async def _dispatcher_sse(stream_info: dict):
    """
    SSE generator for non-OpenClaw agents using AgentDispatcher.

    Emits named SSE events matching SSE_EVENTS in the frontend:
      event: text-delta    data: {"text":"..."}
      event: session-created  data: {"session_id":"..."}
      event: agent-error   data: {"error_message":"..."}
      event: done          data: {"session_id":"..."}
    """
    from services.cowork_agent.dispatcher import AgentDispatcher
    from services.cowork_agent.claude_sessions import save_session
    from services.cowork_agent.adapter_registry import get_adapter
    from services.cowork_agent.settings import load_agent_config

    agent_name = stream_info["agent_name"]
    question = stream_info["question"]
    session_id = stream_info.get("session_id")
    our_session_id = stream_info.get("our_session_id") or session_id
    agent_type = stream_info.get("agent_type")
    agent_id = stream_info.get("agent_id")
    is_new_session = stream_info.get("is_new_session", False)
    session_key = stream_info.get("session_key")

    # Emit session-created immediately for new sessions so the frontend can navigate
    if is_new_session and our_session_id:
        event_id = 1
        yield f"id: {event_id}\nevent: session-created\ndata: {json.dumps({'session_id': our_session_id})}\n\n"
        event_id += 1
    else:
        event_id = 1

    # For Claude Code, resolve the native_session_id for --resume
    native_resume_id = None
    if agent_name == "claude_code" and not is_new_session and session_key:
        try:
            adapter = get_adapter("claude_code", load_agent_config("claude_code"))
            native_resume_id = adapter.get_native_session_id(session_key)
        except Exception:
            pass
        # Fall back to old .sessions JSON format
        if not native_resume_id and session_id:
            from services.cowork_agent.claude_sessions import load_session
            rec = load_session(session_id)
            if rec:
                native_resume_id = rec.get("native_session_id") or None

    # Write preliminary index entry for new Claude Code sessions so
    # the session is discoverable before the subprocess finishes.
    if agent_name == "claude_code" and is_new_session and session_key and our_session_id:
        try:
            adapter = get_adapter("claude_code", load_agent_config("claude_code"))
            cwd = adapter.config.get("workspace_root") or "/home/coder"
            adapter.write_preliminary_entry(session_key, our_session_id, cwd)
        except Exception:
            pass

    dispatcher = AgentDispatcher(agent_name)
    final_native_session_id = None
    first_token = None

    # Build extra kwargs to pass through to adapter.stream() for session tracking
    stream_kwargs: dict = {}
    if agent_name == "claude_code":
        stream_kwargs = {
            "session_key": session_key,
            "our_session_id": our_session_id,
            "agent_id": agent_id,
            # "question" is already the first positional arg — do NOT repeat it here
        }

    try:
        async for event in dispatcher.stream(
            question,
            native_resume_id if not is_new_session else None,
            agent_type=agent_type,
            **stream_kwargs,
        ):
            if event.get("done"):
                final_native_session_id = event.get("native_session_id")
                break
            elif event.get("type") == "token":
                token = event.get("token", "")
                if first_token is None:
                    first_token = token
                yield f"id: {event_id}\nevent: text-delta\ndata: {json.dumps({'text': token})}\n\n"
                event_id += 1
            elif event.get("type") == "error":
                yield f"id: {event_id}\nevent: agent-error\ndata: {json.dumps({'error_message': event.get('error', 'Stream error')})}\n\n"
                event_id += 1
    except Exception as exc:
        yield f"id: {event_id}\nevent: agent-error\ndata: {json.dumps({'error_message': str(exc)})}\n\n"
        event_id += 1

    # Persist Claude Code session record (backward-compat .sessions/{id}.json metadata)
    # The JSONL messages file is now written by the adapter itself.
    if agent_name == "claude_code" and our_session_id:
        title = (first_token or question)[:60] if first_token or question else "Untitled"
        eff_agent_id = agent_id or ""
        try:
            save_session(
                session_id=our_session_id,
                native_session_id=final_native_session_id,
                agent_id=eff_agent_id,
                title=title,
            )
        except Exception:
            pass
        # NOTE: save_session_messages() intentionally NOT called here —
        # the adapter's stream() method appends the JSONL exchange itself.

    resolved_session_id = our_session_id or final_native_session_id or session_id
    yield f"id: {event_id}\nevent: done\ndata: {json.dumps({'session_id': resolved_session_id})}\n\n"


@router.post("/api/chat/prompt")
async def chat_prompt(request: Request):
    body = await request.json()
    text = body.get("text", "").strip()
    session_id = body.get("session_id")
    agent_name = body.get("agent_name")

    if not text:
        return JSONResponse(status_code=400, content={"detail": "Empty message"})

    # For existing sessions: auto-detect backend (claude_code sessions take precedence)
    if session_id and not agent_name:
        detected = _resolve_backend_for_session(session_id)
        if detected:
            agent_name = detected

    if not agent_name:
        agent_name = _AGENT_NAME

    # Non-OpenClaw agents: use AgentDispatcher
    if agent_name != "openclaw":
        is_new_session = not bool(session_id)
        our_session_id = str(uuid.uuid4()) if is_new_session else session_id

        # Derive agent_id from workspace path for new sessions
        agent_id = body.get("agent_id")
        if not agent_id and is_new_session:
            workspace = body.get("workspace", "")
            from services.cowork_agent.settings import CLAUDE_COWORK_DIR
            try:
                ws_path = __import__("pathlib").Path(workspace).expanduser().resolve()
                cc_path = CLAUDE_COWORK_DIR.expanduser().resolve()
                if str(ws_path).startswith(str(cc_path) + "/"):
                    agent_id = ws_path.relative_to(cc_path).parts[0]
            except Exception:
                pass

        # For Claude Code: attach a session_key to every stream so the adapter
        # can write the JSONL index and resume sessions via --resume.
        session_key = None
        if agent_name == "claude_code":
            if is_new_session:
                from services.cowork_agent.adapter_registry import get_adapter
                from services.cowork_agent.settings import load_agent_config
                try:
                    adapter = get_adapter("claude_code", load_agent_config("claude_code"))
                    session_key = adapter.make_session_key(agent_id or "default")
                except Exception:
                    pass
            else:
                # Existing session: look up the session_key from the index
                from services.cowork_agent.adapter_registry import get_adapter
                from services.cowork_agent.settings import load_agent_config
                try:
                    adapter = get_adapter("claude_code", load_agent_config("claude_code"))
                    session_key = adapter.find_session_key_for_session_id(our_session_id)
                except Exception:
                    pass

        stream_id = str(uuid.uuid4())
        active_streams[stream_id] = {
            "question": text,
            "session_id": our_session_id,
            "our_session_id": our_session_id,
            "agent_name": agent_name,
            "agent_type": body.get("agent_type"),
            "agent_id": agent_id,
            "is_new_session": is_new_session,
            "session_key": session_key,
        }
        return {"stream_id": stream_id, "session_id": our_session_id}

    # OpenClaw: existing flow unchanged below.

    # New session: kick off create_new_session as a background task.
    if not session_id:
        oc_agent = openclaw_agent_id_from_prompt_body(body)
        session_key = f"agent:{oc_agent}:web:{uuid.uuid4().hex[:8]}"
        task = asyncio.create_task(create_new_session(text, session_key=session_key))

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
    # Purge stale recently-started records
    now = time.time()
    stale = [k for k, v in _recently_started.items() if now - v["started_at"] > _RECENTLY_STARTED_TTL]
    for k in stale:
        _recently_started.pop(k, None)

    stream_info = active_streams.get(stream_id)
    if not stream_info:
        # Reconnect after double-mount: wait for the original stream to finish,
        # then send done so the client refetches messages from DB.
        recent = _recently_started.get(stream_id)
        if recent:
            session_id = recent["session_id"]
            done_event = recent.get("done_event")
            async def reconnect_done():
                if done_event and not done_event.is_set():
                    try:
                        await asyncio.wait_for(done_event.wait(), timeout=300)
                    except asyncio.TimeoutError:
                        pass
                if session_id:
                    yield f"id: 1\nevent: session-created\ndata: {json.dumps({'session_id': session_id})}\n\n"
                yield f"id: 2\nevent: done\ndata: {json.dumps({'session_id': session_id})}\n\n"
            generator = reconnect_done()
        else:
            async def not_found():
                yield f"id: 1\nevent: error\ndata: {json.dumps({'error_message': 'Stream not found'})}\n\n"
            generator = not_found()
    elif stream_info.get("agent_name"):
        # Pop now so a reconnect triggers the recently-started path above
        # instead of starting a duplicate subprocess.
        active_streams.pop(stream_id, None)
        done_event = asyncio.Event()
        _recently_started[stream_id] = {
            "session_id": stream_info.get("our_session_id"),
            "started_at": now,
            "done_event": done_event,
        }
        async def _dispatcher_with_signal():
            try:
                async for chunk in _dispatcher_sse(stream_info):
                    yield chunk
            finally:
                done_event.set()
        generator = _dispatcher_with_signal()
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
