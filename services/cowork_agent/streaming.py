"""
Chat streaming — bridges OpenClaw's OpenAI-compatible API to xo-cowork's SSE
event stream expected by the frontend.

Two code paths:

1. `stream_openclaw_to_sse` — existing session: forward a single user message
   as a streaming chat completion and translate each `data:` chunk into a
   `text-delta` SSE event.
2. `create_new_session` + `emit_prefetched_sse` — new session: do a
   non-streaming completion so OpenClaw creates the session file, then replay
   the response back to the client as simulated SSE deltas.
"""

import asyncio
import json

import httpx

from services.cowork_agent.settings import (
    AGENTS_DIR,
    OPENCLAW_GATEWAY_TOKEN,
    OPENCLAW_API_URL,
    OPENCLAW_MODEL,
)
from services.cowork_agent.agent_registry import get_default_agent
from services.cowork_agent.chat_state import active_streams
from services.cowork_agent.helpers import normalize_agent_id

_AGENT = get_default_agent()
_SESSION_HEADER = _AGENT.session_header
_MODEL_PREFIX = _AGENT.model_prefix.lower()


def find_session_id_by_key(session_key: str) -> str | None:
    """Look up the sessionId for a given session key in sessions.json."""
    if not AGENTS_DIR.exists():
        return None
    for agent_dir in AGENTS_DIR.iterdir():
        if not agent_dir.is_dir():
            continue
        index_path = agent_dir / "sessions" / "sessions.json"
        if not index_path.exists():
            continue
        with open(index_path) as f:
            index_data = json.load(f)
        meta = index_data.get(session_key)
        if meta:
            return meta.get("sessionId")
    return None


def openclaw_agent_id_from_prompt_body(body: dict) -> str:
    """Resolve agent id from `model` (e.g. `<prefix>/research`) for new sessions.

    The expected prefix comes from the active agent's manifest
    (`model_prefix`), so swapping the default agent swaps the namespace.
    """
    model = body.get("model")
    if isinstance(model, str):
        lowered = model.strip().lower()
        if lowered.startswith(f"{_MODEL_PREFIX}/"):
            rest = model.split("/", 1)[1] if "/" in model else ""
            return normalize_agent_id(rest) if rest.strip() else "main"
        if lowered == _MODEL_PREFIX:
            return "main"
    return "main"


async def create_new_session(text: str, session_key: str) -> tuple[str, str, str]:
    """
    Create a new OpenClaw session by sending the first message.

    EXPERIMENT (2026-04-16): trying `stream=True` on the bootstrap call to
    see if the historical "bootstrap-duplicate" issue still reproduces.
    If the returned `response_text` contains duplicated content (or users
    report seeing the response twice in the UI), revert this to
    `stream=False` — see `bridge/docs/known-issues.md`.

    Behavior is otherwise preserved: we accumulate the streamed deltas
    into a single buffer and return the same tuple as before, so
    `emit_prefetched_sse` continues to fake-stream it to the client.
    """
    response_text = ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(1800.0, connect=10.0)) as client:
        async with client.stream(
            "POST",
            OPENCLAW_API_URL,
            headers={
                "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
                "Content-Type": "application/json",
                _SESSION_HEADER: session_key,
            },
            json={
                "model": OPENCLAW_MODEL,
                "stream": True,
                "messages": [{"role": "user", "content": text}],
            },
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                raise Exception(f"OpenClaw API error: {response.status_code} {body.decode()}")

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content:
                    response_text += content

    # Read sessions.json to find the new session ID
    session_id = find_session_id_by_key(session_key)
    if not session_id:
        raise Exception("Session was created but could not find its ID in sessions.json")

    return session_key, session_id, response_text


async def stream_openclaw_to_sse(stream_id: str):
    """
    Sends the user message to OpenClaw's OpenAI-compatible API using the
    session key header so OpenClaw continues the existing session.
    Streams the response as xo-cowork SSE events (text-delta, done).
    OpenClaw handles persisting messages to its own JSONL files.
    """
    stream_info = active_streams.pop(stream_id, None)
    if not stream_info:
        yield f"id: 1\nevent: error\ndata: {json.dumps({'error_message': 'Stream not found'})}\n\n"
        return

    session_id = stream_info["session_id"]
    text = stream_info["text"]
    session_key = stream_info["session_key"]

    event_id = 0

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(1800.0, connect=10.0)) as client:
            async with client.stream(
                "POST",
                OPENCLAW_API_URL,
                headers={
                    "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
                    "Content-Type": "application/json",
                    _SESSION_HEADER: session_key,
                },
                json={
                    "model": OPENCLAW_MODEL,
                    "stream": True,
                    "messages": [{"role": "user", "content": text}],
                },
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    event_id += 1
                    yield f"id: {event_id}\nevent: agent-error\ndata: {json.dumps({'error_message': f'OpenClaw API error: {response.status_code} {body.decode()}'})}\n\n"
                    return

                line_iter = response.aiter_lines().__aiter__()
                while True:
                    try:
                        line = await asyncio.wait_for(line_iter.__anext__(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield "event: heartbeat\ndata: {}\n\n"
                        continue
                    except StopAsyncIteration:
                        break

                    if not line.startswith("data: "):
                        continue

                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    content = delta.get("content")

                    if content:
                        event_id += 1
                        yield f"id: {event_id}\nevent: text-delta\ndata: {json.dumps({'session_id': session_id, 'text': content})}\n\n"

    except httpx.ConnectError:
        event_id += 1
        yield f"id: {event_id}\nevent: agent-error\ndata: {json.dumps({'error_message': 'Cannot connect to OpenClaw API at ' + OPENCLAW_API_URL})}\n\n"
        return
    except Exception as e:
        event_id += 1
        yield f"id: {event_id}\nevent: agent-error\ndata: {json.dumps({'error_message': str(e)})}\n\n"
        return

    event_id += 1
    yield f"id: {event_id}\nevent: done\ndata: {json.dumps({'finish_reason': 'stop', 'session_id': session_id})}\n\n"


async def emit_prefetched_sse(stream_id: str):
    """
    Await the background create_new_session task, emitting keepalives every
    15 s while waiting. Once complete, emit session-created + response text
    as SSE events.

    The asyncio.Task can be awaited multiple times (returns cached result),
    so React Strict Mode double-mounts are safe.
    """
    stream_info = active_streams.get(stream_id)
    if not stream_info or not stream_info.get("prefetched"):
        yield f"id: 1\nevent: error\ndata: {json.dumps({'error_message': 'Stream not found'})}\n\n"
        return

    task: asyncio.Task = stream_info["task"]
    event_id = 0

    # Wait for the task, emitting keepalives every 15s
    while not task.done():
        done, _ = await asyncio.wait({task}, timeout=15.0)
        if not done:
            yield "event: heartbeat\ndata: {}\n\n"

    # Task finished — check result
    try:
        _session_key, session_id, response_text = task.result()
    except Exception as e:
        event_id += 1
        yield f"id: {event_id}\nevent: agent-error\ndata: {json.dumps({'error_message': str(e)})}\n\n"
        active_streams.pop(stream_id, None)
        return

    # Emit session-created so the frontend can navigate
    event_id += 1
    yield f"id: {event_id}\nevent: session-created\ndata: {json.dumps({'session_id': session_id})}\n\n"

    # Emit response in chunks to simulate streaming
    chunk_size = 4
    for i in range(0, len(response_text), chunk_size):
        chunk = response_text[i : i + chunk_size]
        event_id += 1
        yield f"id: {event_id}\nevent: text-delta\ndata: {json.dumps({'session_id': session_id, 'text': chunk})}\n\n"

    event_id += 1
    yield f"id: {event_id}\nevent: done\ndata: {json.dumps({'finish_reason': 'stop', 'session_id': session_id})}\n\n"
    active_streams.pop(stream_id, None)
