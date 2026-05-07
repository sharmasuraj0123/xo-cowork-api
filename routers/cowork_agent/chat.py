"""
Chat prompt / streaming / abort routes.

All agents (openclaw, claude_code, …) go through AgentDispatcher → adapter.stream().
`/api/chat/prompt` stores stream metadata; `/api/chat/stream/{id}` is the SSE
consumer that calls _dispatcher_sse() to drive the adapter and translate events
into SSE.
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
_RECENTLY_STARTED_TTL = 600  # seconds — must outlast SSE_HEARTBEAT_TIMEOUT (45s) + full reconnect backoff

router = APIRouter()

_AGENT_NAME = os.getenv("AGENT_NAME", "openclaw")


def _resolve_backend_for_session(session_id: str) -> str | None:
    """Return the adapter name that owns session_id, or None (caller uses AGENT_NAME default).

    Delegates to find_session_backend() which is driven by each adapter's sessions_root().
    Adding a new adapter only requires implementing sessions_root() — this function is zero-touch.
    """
    from services.cowork_agent.sessions_io import find_session_backend
    return find_session_backend(session_id)


_KEEPALIVE_INTERVAL = 20  # seconds of silence before emitting an SSE keepalive comment

_SENTINEL = object()  # marks end-of-stream in the keepalive queue


async def _dispatcher_sse(stream_info: dict, _session_id_out: list | None = None):
    """
    SSE generator for all agents using AgentDispatcher.

    Emits named SSE events matching SSE_EVENTS in the frontend:
      event: text-delta    data: {"text":"..."}
      event: session-created  data: {"session_id":"..."}
      event: agent-error   data: {"error_message":"..."}
      event: done          data: {"session_id":"..."}

    During long tool-call runs, emits `event: heartbeat` named events every
    20 s so the frontend's heartbeat timer is reset and idle connections stay open.

    Keepalives use an asyncio.Queue producer-task pattern — NOT
    asyncio.wait_for(__anext__) — because cancelling __anext__ on an
    async generator corrupts its internal state and causes it to stop
    early (the bug that made text disappear after the first timeout).

    Adapters are responsible for all session tracking (session_key, native IDs,
    session persistence). This function is adapter-agnostic.
    """
    from services.cowork_agent.dispatcher import AgentDispatcher

    agent_name = stream_info["agent_name"]
    question = stream_info["question"]
    our_session_id = stream_info.get("our_session_id") or stream_info.get("session_id")
    agent_type = stream_info.get("agent_type")
    agent_id = stream_info.get("agent_id")
    is_new_session = stream_info.get("is_new_session", False)
    prefetch_task = stream_info.get("openclaw_prefetch_task")

    if is_new_session and our_session_id:
        event_id = 1
        yield f"id: {event_id}\nevent: session-created\ndata: {json.dumps({'session_id': our_session_id})}\n\n"
        event_id += 1
    else:
        event_id = 1

    dispatcher = AgentDispatcher(agent_name)
    final_native_session_id = None
    session_id_resolved_early = False
    queue: asyncio.Queue = asyncio.Queue()

    async def _produce():
        try:
            async for event in dispatcher.stream(
                question,
                None,
                agent_type=agent_type,
                our_session_id=our_session_id,
                agent_id=agent_id,
                is_new_session=is_new_session,
                openclaw_prefetch_task=prefetch_task,
            ):
                await queue.put(event)
        except Exception as exc:
            await queue.put({"type": "error", "error": str(exc)})
        finally:
            await queue.put(_SENTINEL)

    producer = asyncio.create_task(_produce())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                yield "event: heartbeat\ndata: {}\n\n"
                continue

            if item is _SENTINEL:
                break

            event = item
            if event.get("done"):
                final_native_session_id = event.get("native_session_id")
                break
            elif event.get("type") == "session-id-resolved":
                # Adapter resolved the session ID mid-stream (e.g. new openclaw
                # session where poll in chat_prompt didn't find it in time).
                sid = event.get("session_id")
                if sid and not our_session_id:
                    yield f"id: {event_id}\nevent: session-created\ndata: {json.dumps({'session_id': sid})}\n\n"
                    event_id += 1
                    session_id_resolved_early = True
            elif event.get("type") == "token":
                yield f"id: {event_id}\nevent: text-delta\ndata: {json.dumps({'text': event.get('token', '')})}\n\n"
                event_id += 1
            elif event.get("type") == "error":
                yield f"id: {event_id}\nevent: agent-error\ndata: {json.dumps({'error_message': event.get('error', 'Stream error')})}\n\n"
                event_id += 1
    finally:
        producer.cancel()

    resolved_session_id = our_session_id or final_native_session_id
    if _session_id_out is not None:
        _session_id_out.append(resolved_session_id)
    if is_new_session and not our_session_id and resolved_session_id and not session_id_resolved_early:
        yield f"id: {event_id}\nevent: session-created\ndata: {json.dumps({'session_id': resolved_session_id})}\n\n"
        event_id += 1
    yield f"id: {event_id}\nevent: done\ndata: {json.dumps({'finish_reason': 'stop', 'session_id': resolved_session_id})}\n\n"


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

    print(f"[chat] routing → agent_name={agent_name!r} session_id={session_id!r} workspace={body.get('workspace')!r}")

    is_new_session = not bool(session_id)

    # Resolve agent_id from explicit field or workspace hint (all agents, new sessions only).
    agent_id = body.get("agent_id")
    if not agent_id and is_new_session:
        workspace_hint = body.get("workspace", "")
        if workspace_hint:
            from services.cowork_agent.project_layout import xo_projects_root
            from services.cowork_agent.settings import CLAUDE_COWORK_DIR
            try:
                ws_path = __import__("pathlib").Path(workspace_hint).expanduser().resolve()
                xo_root = xo_projects_root().resolve()
                cc_path = CLAUDE_COWORK_DIR.resolve()
                if str(ws_path).startswith(str(xo_root) + "/"):
                    agent_id = ws_path.relative_to(xo_root).parts[0]
                elif str(ws_path).startswith(str(cc_path) + "/"):
                    agent_id = ws_path.relative_to(cc_path).parts[0]
            except Exception:
                pass

    if agent_name == "openclaw" and is_new_session:
        # OpenClaw assigns session IDs server-side. Start the HTTP call as a
        # background task and poll briefly so we can return a real session_id
        # in this HTTP response. The SSE phase awaits the same task via the
        # adapter — the openclaw API is only called once.
        from services.cowork_agent.streaming import create_new_session, find_session_id_by_key
        oc_agent = body.get("agent_type") or "main"
        session_key = f"agent:{oc_agent}:web:{uuid.uuid4().hex[:8]}"
        prefetch_task = asyncio.create_task(
            create_new_session(text, session_key=session_key, xo_agent_id=agent_id)
        )
        our_session_id = None
        for _ in range(20):
            await asyncio.sleep(1.0)
            our_session_id = find_session_id_by_key(session_key)
            if our_session_id:
                break
        stream_id = str(uuid.uuid4())
        active_streams[stream_id] = {
            "question": text,
            "session_id": our_session_id,
            "our_session_id": our_session_id,
            "agent_name": "openclaw",
            "agent_type": body.get("agent_type"),
            "agent_id": agent_id,
            "is_new_session": True,
            "openclaw_prefetch_task": prefetch_task,
        }
        return {"stream_id": stream_id, "session_id": our_session_id}

    # All other cases (non-openclaw new sessions, all existing sessions).
    our_session_id = str(uuid.uuid4()) if is_new_session else session_id
    stream_id = str(uuid.uuid4())
    active_streams[stream_id] = {
        "question": text,
        "session_id": our_session_id,
        "our_session_id": our_session_id,
        "agent_name": agent_name,
        "agent_type": body.get("agent_type"),
        "agent_id": agent_id,
        "is_new_session": is_new_session,
    }
    return {"stream_id": stream_id, "session_id": our_session_id}


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
            done_event = recent.get("done_event")
            async def reconnect_done():
                if done_event and not done_event.is_set():
                    try:
                        await asyncio.wait_for(done_event.wait(), timeout=300)
                    except asyncio.TimeoutError:
                        pass
                # Re-read after waiting: the original stream may have resolved
                # a server-assigned session ID (e.g. new openclaw sessions).
                sid = recent["session_id"]
                if sid:
                    yield f"id: 1\nevent: session-created\ndata: {json.dumps({'session_id': sid})}\n\n"
                yield f"id: 2\nevent: done\ndata: {json.dumps({'session_id': sid})}\n\n"
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
        session_id_out: list = []
        async def _dispatcher_with_signal():
            try:
                async for chunk in _dispatcher_sse(stream_info, session_id_out):
                    yield chunk
            finally:
                # Persist the resolved session ID before signalling done so
                # a concurrent reconnect_done() sees it after done_event.wait().
                if session_id_out:
                    _recently_started[stream_id]["session_id"] = session_id_out[0]
                done_event.set()
        generator = _dispatcher_with_signal()
    else:
        async def unknown_stream():
            yield f"id: 1\nevent: error\ndata: {json.dumps({'error_message': 'Unknown stream type'})}\n\n"
        generator = unknown_stream()

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
