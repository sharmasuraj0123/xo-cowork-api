"""
Chat prompt / streaming / abort routes.

The router is backend-agnostic. An agent may contribute a ``chat`` capability
(``services/cowork_agent/adapters/<name>/chat.py``):
  - ``handle_prompt(...)`` — fully owns POST /api/chat/prompt (e.g. openclaw's
    direct prefetch path). When absent, the prompt goes through AgentDispatcher.
  - ``get_sse_generator(stream_id, stream_info)`` — the SSE generator for a
    stream that handler registered.
  - ``resolve_agent_id(body)`` — resolve an agent_id/profile from the prompt
    body (e.g. hermes ``model: "hermes/<profile>"``).
All agents without a custom handler stream through AgentDispatcher via
_run_dispatcher_turn. No backend is named in this file.

Turn lifecycle
--------------
A turn is owned by a PRODUCER task (``_run_dispatcher_turn`` /
``_run_adapter_turn``) that outlives any single HTTP connection. It writes SSE
frames into the turn's bounded replay ring (``chat_state.Turn``) and is the only
writer of terminal state.

Each HTTP connection is a SUBSCRIBER (``_turn_subscriber``): it replays whatever
the client has not seen yet, then follows the ring live. Closing a subscriber —
i.e. a client disconnect — is invisible to the turn. Two consequences, and they
are the whole point of this design:

  * a reconnect attaches to live work instead of being told the turn is done
    (the old code set the completion event in the *generator's* ``finally``, so
    a disconnect looked exactly like a finished turn and the reconnect replied
    ``done`` in 0 s, while the agent was still working); and
  * the producer keeps draining the adapter, so the child process never wedges
    on an unread pipe and the adapter's own ``finally`` — usage roll-up, native
    session-id persistence, transcript tee — still runs.

Replay ids are monotonic per turn and are never re-issued or rewound; frames the
client must not treat as progress (heartbeat, desync, error) deliberately carry
no ``id:`` at all, because the client assigns its ``lastEventId`` from any
``id:`` line it sees.
"""

import asyncio
import json
import re
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.engine import chat_state
from services.cowork_agent.engine.chat_state import Turn, active_streams
from services.xo_manifest import resolve_agent_name

router = APIRouter()


def _resolve_backend_for_session(session_id: str) -> str | None:
    """Return the adapter name that owns session_id, or None (caller uses AGENT_NAME default)."""
    from services.cowork_agent.engine.sessions_io import find_session_backend
    return find_session_backend(session_id)


def _adapter_sse_generator(stream_info: dict, stream_id: str):
    """Return an adapter-owned SSE generator for this stream, or None.

    A stream registered by an agent's ``chat.handle_prompt`` is tagged with
    ``backend``; that adapter's ``get_sse_generator`` produces the stream. The
    router stays backend-agnostic.

    MUST stay a synchronous ``def``. Calling an async generator function merely
    constructs the generator object and runs none of its body, so this awaits
    nothing — which is what keeps the lookup-then-create window in
    ``chat_stream`` atomic. See the invariant note there.
    """
    backend = stream_info.get("backend")
    if not backend:
        return None
    chat_mod = try_load_capability("chat", agent=backend)
    factory = getattr(chat_mod, "get_sse_generator", None) if chat_mod else None
    if factory is None:
        return None
    return factory(stream_id, stream_info)


def _session_id_from_sse(chunk: str) -> str | None:
    """Best-effort extract a ``session_id`` from an SSE chunk's data payload.

    Adapter-owned streams resolve their session id mid-stream (e.g. an
    openclaw prefetch only learns it from the gateway, then emits it in a
    ``session-created`` event). This keeps ``Turn.session_id`` current so a
    synthesised terminal frame still carries the right session.
    Backend-agnostic: parses only the generic SSE wire shape.
    """
    for line in chunk.split("\n"):
        if line.startswith("data: "):
            try:
                payload = json.loads(line[6:])
            except (ValueError, TypeError):
                return None
            if isinstance(payload, dict):
                sid = payload.get("session_id")
                if isinstance(sid, str) and sid:
                    return sid
            return None
    return None


_KEEPALIVE_INTERVAL = 20  # seconds between SSE keepalives, on an independent clock
_HEARTBEAT_FRAME = "event: heartbeat\ndata: {}\n\n"
_DESYNC_FRAME = "event: desync\ndata: {}\n\n"
_SUB_QUEUE_MAX = 64       # wakeup slots per connection; overflow is safe (see Turn.wake)
_HEARTBEAT = object()     # marks a keepalive tick in a subscriber's wakeup queue

_ID_LINE = re.compile(r"^id: *\d+\r?\n", re.MULTILINE)
_DONE_LINE = re.compile(r"^event: *done\r?$", re.MULTILINE)

# Upper bound on the per-turn call_id -> tool map (see _run_dispatcher_turn). A
# turn's tool count is unbounded, so the correlation table needs a ceiling; past
# it, later steps simply lose the `tool` field rather than the process losing
# memory.
_MAX_TRACKED_TOOL_CALLS = 512


def _sse(event_id: int, name: str, payload: dict) -> str:
    return f"id: {event_id}\nevent: {name}\ndata: {json.dumps(payload)}\n\n"


def _emit(turn: Turn, name: str, payload: dict) -> None:
    """Append one router-composed SSE frame to the turn's replay ring.

    No-op once the turn is terminal, which is what lets abort mark a turn done
    while the producer goes on draining harmlessly to the end.
    """
    if turn.terminal:
        return
    event_id = turn.next_id()
    frame = _sse(event_id, name, payload)
    if name == "done":
        turn.terminal_frame = frame
    turn.append(event_id, frame)


def _tool_step_payload(event: dict, tool_by_call: dict[str, str]) -> dict:
    """Build the shared tool-result / tool-error payload for one step.

    ``tool_by_call`` is the calling turn's own correlation table, passed in
    explicitly — never module state, which would bleed between concurrent turns.
    """
    payload = {"call_id": event["call_id"]}
    for key in ("tool", "title", "output"):
        if event.get(key):
            payload[key] = event[key]
    if "tool" not in payload:
        # No adapter supplied one — recover it from the matching tool-call.
        # `pop`, not `get`: a duplicate result must not resurrect the mapping.
        recovered = tool_by_call.pop(event["call_id"], None)
        if recovered:
            payload["tool"] = recovered
    return payload


# ── Producers: own a turn, independent of any connection ─────────────────────

async def _run_dispatcher_turn(turn: Turn, stream_info: dict) -> None:
    """
    Own one AgentDispatcher turn end-to-end.

    Emits named SSE events matching SSE_EVENTS in the frontend:
      event: text-delta       data: {"text":"..."}
      event: session-created  data: {"session_id":"..."}
      event: agent-error      data: {"error_message":"..."}
      event: done             data: {"session_id":"..."}
    plus the tool/reasoning progress frames below.

    This task is the ONLY writer of terminal state, and it is deliberately not
    tied to the HTTP connection: a client disconnect closes a subscriber
    generator and leaves this running, so the adapter's stdout keeps being
    drained (an undrained pipe wedges the child once ~128 KiB accumulates) and
    the adapter's own ``finally`` bookkeeping — usage roll-up, native session
    id — still runs, with the real numbers rather than an empty usage record.

    Adapters are responsible for all session tracking (session_key, native IDs,
    session persistence). This function is adapter-agnostic.
    """
    from services.cowork_agent.engine.dispatcher import AgentDispatcher

    agent_name = stream_info["agent_name"]
    question = stream_info["question"]
    our_session_id = stream_info.get("our_session_id") or stream_info.get("session_id")
    agent_type = stream_info.get("agent_type")
    agent_id = stream_info.get("agent_id")
    model = stream_info.get("model")
    is_new_session = stream_info.get("is_new_session", False)

    if is_new_session and our_session_id:
        _emit(turn, "session-created", {"session_id": our_session_id})

    # call_id -> the mapped tool name we already sent on that step's `tool-call`.
    # A `tool_result` record on the wire identifies its step by id only and does
    # NOT repeat the tool name, so an adapter parsing one line at a time cannot
    # put `tool` on a `tool-result` frame. Correlating here (per turn — this is
    # a local of the one task that owns the turn, never module state, which
    # would bleed between concurrent turns) is what makes two client
    # integrations reachable: `use-sse.ts:336` refreshes the workspace file tree
    # after a write/edit/bash result, and `use-sse.ts:322` routes todo results
    # to the progress panel. Both are keyed on `data.tool` and both were dead
    # code without this.
    #
    # No privacy cost: the value echoed back is the mapped, allowlisted name the
    # client was already given, never a raw CLI or `mcp__*` name.
    tool_by_call: dict[str, str] = {}

    final_native_session_id = None
    state = chat_state.COMPLETED
    try:
        dispatcher = AgentDispatcher(agent_name)
        async for event in dispatcher.stream(
            question,
            None,
            agent_type=agent_type,
            our_session_id=our_session_id,
            agent_id=agent_id,
            model=model,
            is_new_session=is_new_session,
        ):
            # `done` is tested FIRST: a done event carries no `type` key.
            if event.get("done"):
                final_native_session_id = event.get("native_session_id")
                break
            elif event.get("type") == "token":
                _emit(turn, "text-delta", {"text": event.get("token", "")})
            elif event.get("type") == "model-loading":
                # Hermes runs tool calls (RAG recall, web search, etc.) before
                # streaming text — emits ``hermes.tool.progress`` events with a
                # human-readable label. Forward them as model-loading so the UI
                # shows live activity during the 10-30 s tool-call phase
                # instead of looking frozen.
                #
                # SECOND ROLE, easy to miss: adapters also ride this event for
                # operational notices (oversized-line truncation, partial exit),
                # so dropping this branch would silently delete every one of
                # them as well.
                _emit(turn, "model-loading", {"label": event.get("label", "")})
            elif event.get("type") == "error":
                _emit(turn, "agent-error", {"error_message": event.get("error", "Stream error")})
            elif event.get("type") == "tool-call":
                # Live tool progress. The client's TOOL_START handler is
                # `if (data.tool && data.call_id)` — both must be truthy or the
                # frame renders nothing at all. ``tool`` has already been mapped
                # by the adapter onto the client's fixed lowercase vocabulary, so
                # no raw name (which could be `mcp__<server>__<tool>`) reaches
                # the SSE wire. ``arguments`` is never forwarded here: tool
                # inputs are PII.
                #
                # CAVEAT — do not read the two sentences above as an end-to-end
                # guarantee. The message-history endpoint is a second path to
                # the same client and it is not covered: engine/messages.py:360
                # ships the raw tool name and :364 the raw ``input``, and the
                # client refetches history on ``done``. See the SCOPE note in
                # adapters/claude_code/streaming.py.
                if event.get("tool") and event.get("call_id"):
                    if len(tool_by_call) < _MAX_TRACKED_TOOL_CALLS:
                        tool_by_call[event["call_id"]] = event["tool"]
                    _emit(turn, "tool-call", {
                        "tool": event["tool"],
                        "call_id": event["call_id"],
                        "title": event.get("title", ""),
                    })
            elif event.get("type") == "tool-result":
                # ``call_id`` is mandatory — the client's handler is
                # `if (data.call_id)`. ``title`` is omitted by the adapter on
                # purpose (the client does `title ?? existing`, so sending a
                # generic one would overwrite the informative start label) and
                # ``output`` is only forwarded if an adapter supplies a redacted
                # one; raw tool output is PII and is not sent today.
                #
                # ``tool`` is absent on every claude_code tool-result — the wire
                # record identifies its step by id only — so _tool_step_payload
                # recovers it from the matching tool-call.
                if event.get("call_id"):
                    _emit(turn, "tool-result", _tool_step_payload(event, tool_by_call))
            elif event.get("type") == "tool-error":
                # A separate event, not a flag on tool-result: the client's
                # TOOL_RESULT handler marks the step *completed*, so a failed
                # step reported that way renders a green check labelled
                # "Step failed". TOOL_ERROR is the only shape that renders red.
                if event.get("call_id"):
                    _emit(turn, "tool-error", _tool_step_payload(event, tool_by_call))
            elif event.get("type") == "reasoning":
                # One pulse per thinking block. Forwarded rather than dropped so
                # the frame stream stays dense during long silent thinking
                # stretches (it also refreshes the client's activity watchdog).
                # NOTE: deliberately NO ``text`` key — the client's
                # REASONING_DELTA handler appends ``data.text`` verbatim into the
                # user-visible reasoning transcript, so shipping the label
                # "Thinking" there would print it in the transcript once per
                # block. With no ``text`` the handler is a no-op by design.
                _emit(turn, "reasoning-delta", {"title": event.get("title", "Thinking")})

            # Same contract as the adapter lane: never run a whole turn into the
            # ring without letting the subscribers drain it. A dispatcher that
            # reads a subprocess awaits on its own, so in practice this is a
            # no-op here — but it costs one loop iteration per event and removes
            # the assumption entirely, and the assumption is exactly what broke
            # the adapter lane.
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        # Only the janitor's max-runtime sweep cancels a producer, and it puts a
        # terminal frame in the ring before it does. Mark the state anyway so a
        # cancellation from anywhere else cannot leave a turn stuck RUNNING.
        turn.finish(chat_state.ABORTED)
        raise
    except Exception as exc:
        _emit(turn, "agent-error", {"error_message": str(exc)})
        state = chat_state.FAILED

    resolved_session_id = our_session_id or final_native_session_id
    if resolved_session_id:
        turn.session_id = resolved_session_id
    _emit(turn, "done", {"finish_reason": "stop", "session_id": resolved_session_id})
    turn.finish(state)


async def _run_adapter_turn(turn: Turn, adapter_gen) -> None:
    """Own an adapter-supplied SSE stream, whose frames arrive pre-formatted.

    Adapter ids are restamped onto the turn's id space so replay stays monotonic
    whatever the adapter numbers (in practice both are 1..n, so the bytes are
    unchanged). Frames without an ``id:`` are adapter keepalives; they are
    dropped, because every connection synthesises its own on an independent
    clock. That makes an implicit contract on adapter-owned generators: emit an
    ``id:`` on anything you want replayed. A source that ends without a ``done``
    gets one synthesised, so a subscriber always terminates.
    """
    state = chat_state.COMPLETED
    try:
        async for chunk in adapter_gen:
            if not _ID_LINE.search(chunk):
                continue
            sid = _session_id_from_sse(chunk)
            if sid:
                turn.session_id = sid
            if turn.terminal:
                # Aborted from underneath us. Keep draining the source to the
                # end so its own cleanup runs; just stop publishing.
                continue
            event_id = turn.next_id()
            frame = _ID_LINE.sub(f"id: {event_id}\n", chunk, count=1)
            if _DONE_LINE.search(chunk):
                turn.terminal_frame = frame
            turn.append(event_id, frame)
            # Hand the loop to the subscribers before pulling the next chunk.
            # NOT optional and not a micro-optimisation: an adapter generator is
            # under no obligation to await between yields, and the one that
            # matters does not — the openclaw prefetch replays an
            # already-complete answer with `for i in range(0, len(text), 4)` and
            # no await in the loop. `async for` over such a generator runs it to
            # COMPLETION without ever yielding to the event loop, so no
            # subscriber gets to drain, the whole turn lands in the ring at once,
            # and any answer past the ring budget evicts its own head. The first,
            # never-disconnected connection then received `desync` + `done` and
            # not one text-delta. This also restores incremental rendering: the
            # 4-character chunking is pointless if the client gets the answer in
            # one buffered burst.
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        turn.finish(chat_state.ABORTED)
        raise
    except Exception as exc:
        _emit(turn, "agent-error", {"error_message": str(exc)})
        state = chat_state.FAILED

    if turn.terminal_frame is None:
        _emit(turn, "done", {"finish_reason": "stop", "session_id": turn.session_id})
    turn.finish(state)


def _emit_sweep_terminal(turn: Turn) -> None:
    """Terminal frame for a turn the janitor is force-finishing.

    Registered with chat_state so ``sweep()`` never leaves a terminal turn
    without a terminal frame — a reconnect onto one would yield nothing at all,
    and a client reads an empty 200 as an error and reconnects in a loop.
    """
    _emit(turn, "done", {"finish_reason": "timeout", "session_id": turn.session_id})


chat_state.set_sweep_hook(_emit_sweep_terminal)


# ── Subscriber: one HTTP connection reading off a turn ────────────────────────

async def _heartbeat_pump(queue: asyncio.Queue) -> None:
    """Emit a keepalive tick on an independent monotonic clock.

    This task never observes adapter traffic, so the cadence cannot be reset by
    forwarding richer event types — the failure mode that would otherwise
    silently starve an unmodified client's 45 s watchdog. It also covers the
    long silences *between* progress events (a single 60 s Bash call).

    Reads the module global each iteration so tests can retune the interval.
    """
    while True:
        await asyncio.sleep(_KEEPALIVE_INTERVAL)
        try:
            queue.put_nowait(_HEARTBEAT)
        except asyncio.QueueFull:
            pass


async def _turn_subscriber(turn: Turn, cursor: int):
    """Stream one client connection off a turn's replay ring.

    NEVER mutates turn lifecycle state. Closing this generator is a client
    disconnect and must not be observable as "the turn finished" — that single
    rule is the bug fix. The ``finally`` therefore does exactly two things:
    stop this connection's heartbeat and deregister its wakeup queue.

    Stored frames are re-emitted verbatim, so a client's ``lastEventId`` can
    only ever move forward.
    """
    if cursor > turn.last_id:
        # A cursor AHEAD of everything this turn has ever emitted cannot
        # describe frames of this turn — the client is carrying a cursor from a
        # PREVIOUS turn (each turn restarts its id space at 1), or junk. Neither
        # `evicted_past` nor `frames_after` catches this: the former only guards
        # the low side and the latter returns [] for anything past the ring, so
        # an unclamped high cursor produced a permanent SILENT hole — no replay,
        # no desync, no error, the connection simply dead for the rest of the
        # turn while the answer streamed past it.
        #
        # Not a malformed-input edge case. The client keeps `lastEventId` across
        # turns, so every second-and-later connection of a new turn arrives with
        # the previous turn's cursor: a StrictMode double mount, or any of the
        # routine reconnects. `chat_stream` already forces 0 for the FIRST
        # connection of a turn for exactly this reason; this is that same
        # defence applied where it actually has to live, since every subsequent
        # connection takes the `turns.get` path instead.
        #
        # Degrade to full replay, matching `_resolve_cursor`'s rule for a
        # malformed value. This does not "rewind" the client in any meaningful
        # sense: the id space it is being moved into is a different one, and it
        # is the same space the turn's first connection was already served from.
        # If the ring has since evicted, the `evicted_past(0)` check below turns
        # this into an honest `desync` rather than a hole.
        cursor = 0

    queue: asyncio.Queue = asyncio.Queue(maxsize=_SUB_QUEUE_MAX)
    turn.subscribers.add(queue)
    pump = asyncio.create_task(_heartbeat_pump(queue))
    emitted_terminal = False
    try:
        while True:
            if turn.evicted_past(cursor):
                # Frames this client never saw are already gone. Tell it to
                # reconcile from the canonical store and then follow LIVE only:
                # replaying the retained tail on top of a client that has just
                # cleared and refetched would duplicate that tail and still
                # leave the evicted head as a hole. Deliberately carries no id.
                #
                # Checked every pass, not just at connect: a slow reader on a
                # fast turn can fall off the back of the ring mid-connection,
                # and skipping the hole silently would be the same lie in a
                # different costume.
                yield _DESYNC_FRAME
                cursor = turn.last_id
                continue

            pending = turn.frames_after(cursor)
            if pending:
                for event_id, frame in pending:
                    cursor = event_id
                    if frame is turn.terminal_frame:
                        emitted_terminal = True
                    yield frame
                # Re-drain: the producer may have appended while we were
                # suspended in a yield above.
                continue
            if turn.terminal:
                break
            item = await queue.get()
            if item is _HEARTBEAT:
                yield _HEARTBEAT_FRAME

        if not emitted_terminal:
            # Either this connection joined after the terminal frame (a
            # reconnect on a finished turn — re-emit the real frame, same id, so
            # it is a repeat and not a rewind), or the turn was force-finished
            # with no terminal frame at all. Never close without one: a client
            # that gets an empty 200 treats it as an error and reconnect-loops.
            if turn.terminal_frame:
                yield turn.terminal_frame
            else:
                yield (f"event: done\ndata: "
                       f"{json.dumps({'finish_reason': turn.state, 'session_id': turn.session_id})}\n\n")
    finally:
        pump.cancel()
        turn.subscribers.discard(queue)


async def _terminal_error(message: str):
    """A one-frame error stream. Carries no id: an error must not move a cursor."""
    yield f"event: error\ndata: {json.dumps({'error_message': message})}\n\n"


def _sse_response(generator) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _resolve_cursor(request: Request, last_event_id: str | None) -> int:
    """Highest event id this client has already rendered.

    Two sources, both lower bounds on "what I have": the ``last_event_id`` query
    param the product client appends to every reconnect, and the standard
    ``Last-Event-ID`` header a native EventSource sends on its own auto-reconnect
    (which is all the in-repo space_ui debug client has). Take the max so replay
    never repeats what the client already showed. Parsed defensively — a
    malformed value must degrade to a full replay, never to a 422, which is why
    the route annotates it as ``str`` and does no validation.
    """
    def _as_int(raw) -> int:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return 0
        return value if value > 0 else 0

    return max(_as_int(last_event_id), _as_int(request.headers.get("last-event-id")))


@router.post("/api/chat/prompt")
async def chat_prompt(request: Request):
    chat_state.ensure_janitor()
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

    # Agent switched mid-session (e.g., user picked a different agent from the
    # sidebar dropdown while a chat was open): the incoming session_id belongs
    # to a different backend than the one the user just selected. Treat as a
    # fresh session under the new agent.
    if session_id and agent_name:
        detected = _resolve_backend_for_session(session_id)
        if detected and detected != agent_name:
            print(f"[chat] agent switch detected (session backend={detected!r} new agent={agent_name!r}); starting fresh session")
            session_id = None

    if not agent_name:
        agent_name = resolve_agent_name()

    print(f"[chat] routing → agent_name={agent_name!r} agent_id={body.get('agent_id')!r} session_id={session_id!r} workspace={body.get('workspace')!r}")

    is_new_session = not bool(session_id)

    # Resolve agent_id from explicit field or workspace hint (all agents, new sessions only).
    # For openclaw this becomes xo_agent_id (xo-projects subdir for the transcript tee).
    agent_id = body.get("agent_id")
    if not agent_id and is_new_session:
        workspace_hint = body.get("workspace", "")
        if workspace_hint:
            from services.cowork_agent.project_layout import xo_projects_root
            from services.cowork_agent.registry.settings import CLAUDE_COWORK_DIR
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

    # Optional adapter hook: resolve an agent_id/profile from the prompt body
    # (e.g. hermes ``model: "hermes/<profile>"``). Explicit agent_id wins.
    if not agent_id:
        chat_mod = try_load_capability("chat", agent=agent_name)
        resolver = getattr(chat_mod, "resolve_agent_id", None) if chat_mod else None
        if resolver:
            agent_id = resolver(body)

    # Optional adapter hook: an agent may fully own the prompt path (e.g.
    # openclaw's direct prefetch/streaming). When present it returns the
    # response; otherwise we fall through to the shared dispatcher path.
    chat_mod = try_load_capability("chat", agent=agent_name)
    handle_prompt = getattr(chat_mod, "handle_prompt", None) if chat_mod else None
    if handle_prompt:
        return await handle_prompt(
            body=body,
            text=text,
            session_id=session_id,
            agent_id=agent_id,
            is_new_session=is_new_session,
        )

    # Default: route through AgentDispatcher.
    our_session_id = str(uuid.uuid4()) if is_new_session else session_id
    stream_id = str(uuid.uuid4())
    active_streams[stream_id] = {
        "question": text,
        "session_id": our_session_id,
        "our_session_id": our_session_id,
        "agent_name": agent_name,
        "agent_type": body.get("agent_type"),
        "agent_id": agent_id,
        "model": body.get("model"),
        "is_new_session": is_new_session,
    }
    return {"stream_id": stream_id, "session_id": our_session_id}


@router.get("/api/chat/stream/{stream_id}")
async def chat_stream(stream_id: str, request: Request, last_event_id: str | None = None):
    chat_state.ensure_janitor()
    cursor = _resolve_cursor(request, last_event_id)

    turn = chat_state.turns.get(stream_id)
    if turn is not None:
        # Reconnect. Running -> the client attaches to live work; finished ->
        # it replays the tail and gets the real terminal frame. Either way the
        # answer comes from what the producer actually emitted, never a
        # fabricated `done`.
        return _sse_response(_turn_subscriber(turn, cursor))

    # INVARIANT: nothing between the lookup above and the new_turn calls below
    # may await. asyncio only switches at an await, so lookup -> create ->
    # register is atomic and a StrictMode double-mount always finds the turn
    # above instead of racing a second producer onto the same stream_id. This
    # also has to come BEFORE _adapter_sse_generator: an adapter-owned generator
    # may pop active_streams on its first step, so a second instantiation would
    # find the record gone and emit a spurious "Stream not found".
    stream_info = active_streams.get(stream_id)
    if not stream_info:
        return _sse_response(_terminal_error("Stream not found"))

    adapter_gen = _adapter_sse_generator(stream_info, stream_id)
    if adapter_gen is not None:
        # Adapter-owned stream (e.g. openclaw prefetch / live gateway stream).
        # The adapter owns active_streams cleanup, so we do not pop it here.
        turn = chat_state.new_turn(
            stream_id,
            stream_info.get("session_id") or stream_info.get("our_session_id"),
        )
        turn.task = asyncio.create_task(_run_adapter_turn(turn, adapter_gen))
    elif stream_info.get("agent_name"):
        # Shared dispatcher path.
        active_streams.pop(stream_id, None)
        turn = chat_state.new_turn(stream_id, stream_info.get("our_session_id"))
        turn.task = asyncio.create_task(_run_dispatcher_turn(turn, stream_info))
    else:
        return _sse_response(_terminal_error("Unknown stream type"))

    # First connection for this turn: nothing has been emitted yet, so any
    # cursor the client supplied belongs to a previous stream. Start from zero.
    return _sse_response(_turn_subscriber(turn, 0))


@router.post("/api/chat/abort")
async def chat_abort(request: Request):
    body = await request.json()
    stream_id = body.get("stream_id")
    if stream_id:
        active_streams.pop(stream_id, None)
        turn = chat_state.turns.get(stream_id)
        if turn is not None and not turn.terminal:
            # STATE ONLY. This endpoint is fired by the client on component
            # unmount and on session switch, not just from a Stop button, and
            # the server cannot tell those apart — so it must never kill
            # anything. It detaches the turn (marks it terminal, emits a
            # terminal frame so any live subscriber closes cleanly) and leaves
            # the producer task running. The producer keeps draining the
            # adapter to completion, so the transcript and the adapter's usage
            # roll-up still land. `_emit` no-ops after `finish`, hence the
            # order: signal, frame, finish.
            turn.handle.abort("client")
            _emit(turn, "done", {"finish_reason": "abort", "session_id": turn.session_id})
            turn.finish(chat_state.ABORTED)
    return {"ok": True}


@router.post("/api/chat/respond")
async def chat_respond(request: Request):
    return {"ok": True}
