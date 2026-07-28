"""SSE contract gates — docs §9 #5, #6, #8, #9.

All of these run against the real ASGI app with a fake adapter. No `claude`
process, no socket, no network. Whole file runs in about a second.

`test_no_false_done_on_reconnect` was EXPECTED TO FAIL before the turn-lifecycle
change: it is the executable statement of the false-`done` bug. It now passes,
so its xfail marker is gone (strict xfail makes an unexpected pass a suite
failure, which is exactly the signal we wanted).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.support.fake_agent import dispatcher_from_capture
from tests.support.sse import SSESession


@pytest.fixture(autouse=True)
def _drain_turns():
    """Tear down turn state between tests.

    The producer now deliberately OUTLIVES the connection, so a test that drops
    a client mid-flight (`hang_after=...` sleeps an hour inside the fake
    dispatcher) leaves a live task behind. Without this the tasks are still
    pending when the function-scoped event loop closes — "Task was destroyed but
    it is pending" on stderr — and `chat_state.turns` accumulates turns whose
    tasks belong to dead loops.
    """
    from services.cowork_agent.engine import chat_state

    yield

    for turn in list(chat_state.turns.values()):
        if turn.task is not None and not turn.task.done():
            turn.task.cancel()
    chat_state.turns.clear()
    chat_state.active_streams.clear()
    if chat_state._janitor is not None:
        chat_state._janitor.cancel()
        chat_state._janitor = None


@pytest.fixture
def fake_dispatcher(monkeypatch):
    """Install a capture-replaying dispatcher. Returns the installer."""

    def _install(capture: str, **kwargs):
        from services.cowork_agent.engine import dispatcher as dispatcher_mod

        cls = dispatcher_from_capture(capture, **kwargs)
        monkeypatch.setattr(dispatcher_mod, "AgentDispatcher", cls)
        return cls

    return _install


async def start_turn(app, text="hello") -> tuple[str, str]:
    """POST /api/chat/prompt through the real router; returns (stream_id, session_id)."""
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/chat/prompt", json={"text": text, "agent_name": "claude_code"})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["stream_id"], body["session_id"]


# ── Gate #5 — no false `done` ────────────────────────────────────────────────

async def test_no_false_done_on_reconnect(app, fake_dispatcher):
    """Start a turn, drop the connection mid-flight, reconnect: the server must
    NOT claim the turn is done while the adapter is still producing."""
    fake = fake_dispatcher("multi_tool", scale=200.0, hang_after=2)
    stream_id, _ = await start_turn(app)

    first = SSESession(app, f"/api/chat/stream/{stream_id}")
    await first.start()
    await first.read_until(lambda f: f["event"] == "text-delta", timeout=2)
    await first.disconnect()

    assert not fake.finished.is_set(), "fixture bug: the turn should still be running"

    second = SSESession(app, f"/api/chat/stream/{stream_id}")
    await second.start()
    frames = await second.read_frames(timeout=0.5)

    done = [f for f in frames if f["event"] == "done"]
    assert not done, (
        f"server replied done in <0.5 s while the turn was still running: {done}"
    )
    await second.disconnect()


async def test_reconnect_after_genuine_completion_does_say_done(app, fake_dispatcher):
    """The other half of gate #5, and the reason it cannot be fixed by simply
    deleting the reconnect path: once the turn really is finished, a reconnect
    MUST get `done` promptly, or the client hangs forever."""
    fake_dispatcher("text_only", gap=0.0)
    stream_id, _ = await start_turn(app)

    first = SSESession(app, f"/api/chat/stream/{stream_id}")
    await first.start()
    frames = await first.read_until(lambda f: f["event"] == "done", timeout=3)
    assert any(f["event"] == "done" for f in frames)

    second = SSESession(app, f"/api/chat/stream/{stream_id}")
    await second.start()
    frames = await second.read_until(lambda f: f["event"] == "done", timeout=2)
    assert any(f["event"] == "done" for f in frames)
    await second.disconnect()


async def test_disconnect_is_invisible_to_the_turn(app, fake_dispatcher):
    """The mechanism behind gate #5. A closed connection must not write ANY
    lifecycle state — that is the whole bug: terminal state used to be set in
    the subscriber generator's `finally`, so a disconnect was indistinguishable
    from a finished turn."""
    from services.cowork_agent.engine import chat_state

    fake_dispatcher("multi_tool", gap=0.0, hang_after=3)
    stream_id, _ = await start_turn(app)
    s = SSESession(app, f"/api/chat/stream/{stream_id}")
    await s.start()
    await s.read_frames(timeout=0.4)
    await s.disconnect()
    await asyncio.sleep(0.05)

    turn = chat_state.turns[stream_id]
    assert turn.state == chat_state.RUNNING
    assert turn.finished_at is None
    assert turn.terminal_frame is None
    assert not turn.subscribers, "subscriber leaked"
    assert turn.task is not None and not turn.task.done(), "producer died with the connection"


async def test_reconnect_cursor_is_honoured_and_ids_never_rewind(app, fake_dispatcher):
    """`last_event_id` (query param, what the product client sends) and
    `Last-Event-ID` (header, what a native EventSource sends) are both lower
    bounds on what the client already rendered. Replay must resume strictly
    after the higher of the two, because the client assigns its `lastEventId`
    from any `id:` line it sees — re-sending a low id drags it backwards."""
    fake_dispatcher("multi_tool", gap=0.0, hang_after=6)
    stream_id, _ = await start_turn(app)

    first = SSESession(app, f"/api/chat/stream/{stream_id}")
    await first.start()
    got = await first.read_frames(timeout=0.5)
    ids = [int(f["id"]) for f in got if f["id"]]
    assert ids == list(range(1, len(ids) + 1)), f"ids not monotonic from 1: {ids}"
    last = ids[-1]
    await first.disconnect()

    for label, kwargs in (
        ("query param", {"query": f"last_event_id={last}".encode()}),
        ("header", {"headers": [(b"last-event-id", str(last).encode())]}),
    ):
        s = SSESession(app, f"/api/chat/stream/{stream_id}", **kwargs)
        await s.start()
        replayed = await s.read_frames(timeout=0.4)
        stale = [f for f in replayed if f["id"] and int(f["id"]) <= last]
        assert not stale, f"{label}: replayed frames the client already had: {stale}"
        await s.disconnect()


async def test_malformed_cursor_degrades_to_full_replay(app, fake_dispatcher):
    """A junk cursor must never become a 422 — the client would be stuck
    reconnecting into a hard error. Full replay is the safe degradation."""
    fake_dispatcher("multi_tool", gap=0.0, hang_after=4)
    stream_id, _ = await start_turn(app)
    s = SSESession(app, f"/api/chat/stream/{stream_id}")
    await s.start()
    await s.read_frames(timeout=0.4)
    await s.disconnect()

    bad = SSESession(app, f"/api/chat/stream/{stream_id}",
                     query=b"last_event_id=not-a-number",
                     headers=[(b"last-event-id", b"%%%")])
    await bad.start()
    frames = await bad.read_frames(timeout=0.4)
    assert bad.status == 200, bad.status
    assert [int(f["id"]) for f in frames if f["id"]][:1] == [1], frames
    await bad.disconnect()


async def test_cursor_ahead_of_the_turn_degrades_to_full_replay(app, fake_dispatcher):
    """A cursor HIGHER than anything the turn emitted must replay from the
    start, not open a silent hole.

    `evicted_past` only guards the low side and `frames_after` returns [] for
    anything past the ring, so an unclamped high cursor used to produce no
    replay, no `desync` and no error — the connection just went dead for the
    rest of the turn and the client saw only the final `done`.

    The reachable case is not malformed input: each turn restarts its id space
    at 1 while the client keeps `lastEventId` across turns, so the second and
    later connections of a NEW turn arrive carrying the PREVIOUS turn's cursor.
    `chat_stream` forces 0 for a turn's first connection only; every subsequent
    one takes the `turns.get` path, which is why the clamp lives in the
    subscriber.
    """
    fake_dispatcher("multi_tool", gap=0.0, hang_after=6)
    stream_id, _ = await start_turn(app)

    live = SSESession(app, f"/api/chat/stream/{stream_id}")
    await live.start()
    seen = await live.read_frames(timeout=0.5)
    assert seen, "fixture bug: nothing streamed"

    for label, kwargs in (
        # A stale cross-turn cursor, i.e. the StrictMode double-mount case.
        ("stale cross-turn", {"query": b"last_event_id=20"}),
        ("absurd", {"query": b"last_event_id=99999999999999999999"}),
        ("header", {"headers": [(b"last-event-id", b"9999")]}),
    ):
        s = SSESession(app, f"/api/chat/stream/{stream_id}", **kwargs)
        await s.start()
        frames = await s.read_frames(timeout=0.4)
        assert s.status == 200, f"{label}: {s.status}"
        assert frames, (
            f"{label}: cursor past the turn's last_id opened a silent hole — "
            f"no replay, no desync, no error"
        )
        assert [int(f["id"]) for f in frames if f["id"]][:1] == [1], (
            f"{label}: did not degrade to a full replay: {frames}"
        )
        await s.disconnect()
    await live.disconnect()


async def test_oversized_frame_still_reaches_a_connected_client(app, monkeypatch):
    """A frame larger than the whole ring budget must be DELIVERED, not evicted
    by its own append.

    The eviction loop used to stop only on an empty deque, so a single frame
    over TURN_BUFFER_CHARS popped itself and set `first_id` to its own id — a
    client that had been connected the entire time got a `desync` where its
    content should have been, and nothing in this repo consumes `desync`. The
    ring bounds replay depth; it must never cost a live client the answer.

    Reachable: subprocess_io.STREAM_LIMIT admits stream-json lines up to 1 MiB
    and claude_code turns one assistant text block into ONE text-delta.
    """
    from services.cowork_agent.engine import chat_state
    from services.cowork_agent.engine import dispatcher as dispatcher_mod

    big = "B" * (chat_state.TURN_BUFFER_CHARS + 1000)

    class Big:
        def __init__(self, name):
            pass

        async def stream(self, *a, **k):
            yield {"type": "token", "token": "before"}
            yield {"type": "token", "token": big}
            yield {"type": "token", "token": "after"}
            yield {"done": True, "native_session_id": "n1"}

    monkeypatch.setattr(dispatcher_mod, "AgentDispatcher", Big)
    stream_id, _ = await start_turn(app)
    s = SSESession(app, f"/api/chat/stream/{stream_id}")
    await s.start()
    frames = await s.read_until(lambda f: f["event"] == "done", timeout=10)

    assert not [f for f in frames if f["event"] == "desync"], (
        f"desync on a connection that never dropped: {[f['event'] for f in frames]}"
    )
    texts = [json.loads(f["data"])["text"] for f in frames if f["event"] == "text-delta"]
    assert texts == ["before", big, "after"], (
        f"oversized frame lost; delivered lengths {[len(t) for t in texts]}"
    )
    await s.disconnect()


async def test_adapter_lane_drains_to_subscribers_as_it_runs(app, monkeypatch):
    """An adapter generator that never awaits must not buffer the whole turn
    into the ring before any subscriber runs.

    openclaw's prefetch replay is exactly this shape — it re-chunks an
    already-complete answer with `for i in range(0, len(text), 4)` and no await
    in the loop — so `async for` ran it to completion without ever yielding to
    the event loop. The ring then evicted its own head and the FIRST,
    never-disconnected connection received `desync` + `done` and zero
    text-deltas. The answer here is far past both ring caps, so it can only
    arrive intact if the ring is a sliding window that readers drain live.
    """
    import routers.cowork_agent.chat as chat
    from services.cowork_agent.engine import chat_state

    session_id = "s" * 36
    answer = "z" * 40_000

    def make_gen(stream_id, stream_info):
        async def gen():
            eid = 1
            yield (f"id: {eid}\nevent: session-created\n"
                   f"data: {json.dumps({'session_id': session_id})}\n\n")
            for i in range(0, len(answer), 4):
                eid += 1
                yield (f"id: {eid}\nevent: text-delta\ndata: "
                       f"{json.dumps({'session_id': session_id, 'text': answer[i:i + 4]})}\n\n")
            eid += 1
            yield (f"id: {eid}\nevent: done\ndata: "
                   f"{json.dumps({'finish_reason': 'stop', 'session_id': session_id})}\n\n")

        return gen()

    monkeypatch.setattr(chat, "_adapter_sse_generator", make_gen)
    stream_id = "adapter-lane-stream"
    chat_state.active_streams[stream_id] = {"backend": "x", "session_id": session_id}

    s = SSESession(app, f"/api/chat/stream/{stream_id}")
    await s.start()
    frames = await s.read_until(lambda f: f["event"] == "done", timeout=20)

    assert not [f for f in frames if f["event"] == "desync"], (
        "the turn self-evicted before its only subscriber ever ran"
    )
    delivered = "".join(json.loads(f["data"])["text"]
                        for f in frames if f["event"] == "text-delta")
    assert delivered == answer, f"delivered {len(delivered)} of {len(answer)} chars"
    # Past TURN_BUFFER_FRAMES (4096) — proves the reader kept up rather than the
    # ring simply being big enough to hold everything.
    assert len(answer) // 4 > chat_state.TURN_BUFFER_FRAMES
    await s.disconnect()


async def test_ring_eviction_emits_desync_and_then_follows_live(app, fake_dispatcher,
                                                                monkeypatch):
    """When the replay ring has dropped frames the client never saw, say so and
    skip to live. Splicing the retained tail on instead would duplicate it on a
    client that has already cleared and refetched, AND still leave the evicted
    head as a hole."""
    from services.cowork_agent.engine import chat_state

    monkeypatch.setattr(chat_state, "TURN_BUFFER_FRAMES", 3)
    fake_dispatcher("multi_tool", gap=0.0, hang_after=8)
    stream_id, _ = await start_turn(app)

    s = SSESession(app, f"/api/chat/stream/{stream_id}")
    await s.start()
    await s.read_frames(timeout=0.5)
    await s.disconnect()

    turn = chat_state.turns[stream_id]
    assert turn.first_id > 0, "fixture bug: nothing was evicted"

    r = SSESession(app, f"/api/chat/stream/{stream_id}", query=b"last_event_id=1")
    await r.start()
    frames = await r.read_frames(timeout=0.4)
    assert frames and frames[0]["event"] == "desync", frames
    assert frames[0]["id"] is None, "desync must not move the client's cursor"
    assert len(frames) == 1, f"hole-punched tail replayed after desync: {frames}"
    await r.disconnect()


async def test_abort_is_state_only_and_the_producer_keeps_draining(app, fake_dispatcher):
    """The decision this change is built around. The client POSTs /api/chat/abort
    on component unmount and on session switch, not just from a Stop button, and
    the server cannot tell them apart — so abort DETACHES (marks the turn
    terminal, closes subscribers with a real terminal frame) and the producer
    goes on draining to completion, so the transcript and the adapter's usage
    roll-up still land. It must never cancel or kill anything."""
    import time as _time

    import httpx

    from services.cowork_agent.engine import chat_state

    fake = fake_dispatcher("multi_tool", gap=0.1)
    n_events = len(fake.timeline)
    stream_id, _ = await start_turn(app)
    s = SSESession(app, f"/api/chat/stream/{stream_id}")
    await s.start()
    seen = await s.read_frames(timeout=0.4)
    assert seen and not any(f["event"] == "done" for f in seen), seen

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/chat/abort", json={"stream_id": stream_id})
    assert r.status_code == 200
    aborted_at = _time.monotonic()

    turn = chat_state.turns[stream_id]
    assert turn.state == chat_state.ABORTED
    assert turn.task is not None and not turn.task.done(), "abort cancelled the producer"

    tail = await s.read_frames(timeout=1.0)
    assert any(f["event"] == "done" for f in tail), f"live subscriber left hanging: {tail}"
    assert json.loads(turn.terminal_frame.split("data: ")[1])["finish_reason"] == "abort"

    await asyncio.wait_for(asyncio.shield(turn.task), timeout=20)
    assert not turn.task.cancelled()
    assert turn.task.exception() is None
    elapsed = _time.monotonic() - aborted_at
    remaining = (n_events - len(seen)) * 0.1
    assert elapsed > remaining * 0.5, (
        f"producer stopped {elapsed:.2f}s after abort but had ~{remaining:.2f}s "
        f"of adapter left to drain — abort cut the turn short"
    )


async def test_janitor_timeout_still_leaves_a_terminal_frame(app, fake_dispatcher):
    """The max-runtime sweep force-finishes a wedged turn. If it did so without
    putting a terminal frame in the ring, a later reconnect would find a
    terminal turn with nothing to send, close with an empty 200, and the client
    would read that as an error and reconnect in a loop."""
    from services.cowork_agent.engine import chat_state

    fake_dispatcher("multi_tool", gap=0.0, hang_after=2)
    stream_id, _ = await start_turn(app)
    s = SSESession(app, f"/api/chat/stream/{stream_id}")
    await s.start()
    await s.read_frames(timeout=0.4)
    await s.disconnect()

    turn = chat_state.turns[stream_id]
    turn.created_at -= chat_state.TURN_MAX_RUNTIME + 1
    chat_state.sweep(chat_state._sweep_hook)
    assert turn.terminal
    assert turn.terminal_frame is not None

    r = SSESession(app, f"/api/chat/stream/{stream_id}")
    await r.start()
    frames = await r.read_frames(timeout=0.5)
    assert any(f["event"] == "done" for f in frames), frames
    await r.disconnect()


async def test_unclaimed_stream_record_is_swept(app, fake_dispatcher):
    """A POST /prompt whose GET never arrives used to leak an active_streams
    entry forever."""
    from services.cowork_agent.engine import chat_state

    fake_dispatcher("text_only", gap=0.0)
    stream_id, _ = await start_turn(app)
    assert stream_id in chat_state.active_streams
    chat_state.sweep()                       # stamps _first_seen, keeps it
    assert stream_id in chat_state.active_streams
    chat_state.active_streams[stream_id]["_first_seen"] -= chat_state.UNCLAIMED_STREAM_TTL + 1
    chat_state.sweep()
    assert stream_id not in chat_state.active_streams


async def test_concurrent_turns_keep_separate_id_spaces_and_tool_maps(app, fake_dispatcher):
    """Two overlapping turns must share no per-turn state: separate rings,
    separate monotonic id spaces, separate call_id -> tool correlation."""
    fake_dispatcher("multi_tool", gap=0.0)
    a, _ = await start_turn(app, text="a")
    b, _ = await start_turn(app, text="b")
    sa = SSESession(app, f"/api/chat/stream/{a}")
    sb = SSESession(app, f"/api/chat/stream/{b}")
    await sa.start()
    await sb.start()
    both = (await sa.read_until(lambda f: f["event"] == "done", timeout=5),
            await sb.read_until(lambda f: f["event"] == "done", timeout=5))

    for frames in both:
        results = [json.loads(f["data"]) for f in frames
                   if f["event"] in ("tool-result", "tool-error")]
        assert results
        for data in results:
            assert data.get("tool"), data
        ids = [int(f["id"]) for f in frames if f["id"]]
        assert ids == sorted(ids), f"ids out of order: {ids}"
        assert len(set(ids)) == len(ids), f"duplicate ids: {ids}"


async def test_stream_not_found_carries_no_id(app):
    """An error frame must not move the client's cursor — the old code emitted
    `id: 1` here, which dragged a client that had reached 47 back to 1."""
    s = SSESession(app, "/api/chat/stream/no-such-stream")
    await s.start()
    frames = await s.read_frames(timeout=0.5)
    assert frames and frames[0]["event"] == "error", frames
    assert frames[0]["id"] is None, frames


# ── Gate #6 — progress density ───────────────────────────────────────────────

@pytest.mark.xfail(
    strict=True,
    reason="docs §9 gate 6 / §3.1. STILL FAILING, but no longer for the "
           "original reason — do not read the old text ('tool_use and "
           "tool_result never become SSE frames') as current. Phase 2.1/2.2 "
           "landed: multi_tool now yields 20 informational frames, up from 0. "
           "The turn-lifecycle change then landed on top and did NOT move the "
           "number: re-measured after it, still 20 informational frames out of "
           "21 and still a 7.84 s worst gap. That is expected — the lifecycle "
           "change moves WHO emits frames and WHERE they are buffered, and the "
           "only frame type it adds is `heartbeat`, which this test filters out "
           "by construction. Same input events, same output events, same "
           "timeline. "
           "What remains is a 7.8 s residual gap that the parser cannot close "
           "by forwarding more events, because there are no more events to "
           "forward. Measured on the fixture's real timeline: the 7.77 s window "
           "(tool-result toolu_…0006 -> the next reasoning pulse) contains only "
           "three `system`/`subtype=thinking_tokens` records, and those carry "
           "no timestamp so they replay at delta 0 — i.e. they land at the "
           "START of the window and would not split it even if forwarded; the "
           "second 5.15 s window (lines 18->19) has no wire traffic at all. "
           "Closing gate #6 therefore needs a ROUTER-side informational "
           "keepalive on a timer, not more parser output — a deliberate design "
           "decision (it changes what an idle turn puts on the wire, and it "
           "interacts with gate #8's heartbeat budget), so it is left "
           "unimplemented rather than bolted on here.",
)
async def test_informational_frame_every_five_seconds(app, fake_dispatcher, monkeypatch):
    """No gap > 5 s between *informational* (non-heartbeat) frames on a
    multi-tool turn. Clock-scaled: the fixture's real turn was 83.8 s, replayed
    at 1/100, so the bound is 50 ms."""
    import routers.cowork_agent.chat as chat

    scale = 100.0
    monkeypatch.setattr(chat, "_KEEPALIVE_INTERVAL", 20 / scale)
    fake_dispatcher("multi_tool", scale=scale)  # replay the capture's real timing

    stream_id, _ = await start_turn(app)
    session = SSESession(app, f"/api/chat/stream/{stream_id}")
    await session.start()
    await session.read_until(lambda f: f["event"] == "done", timeout=10)

    informational = [f for f in session.frames if f["event"] != "heartbeat"]
    times = [f["at"] for f in informational]
    gaps = [b - a for a, b in zip(times, times[1:])]
    worst = max(gaps, default=0) * scale
    assert worst <= 5.0, (
        f"largest informational gap {worst:.1f}s (scaled) exceeds 5 s; "
        f"{len(informational)} informational frames out of {len(session.frames)}"
    )


# ── tool-step frames carry what the client keys off ──────────────────────────

async def test_tool_result_frames_carry_the_tool_name(app, fake_dispatcher):
    """Every `tool-result` must repeat the `tool` its `tool-call` announced.

    The wire record for a finished step identifies it by `tool_use_id` only, so
    a line-at-a-time parser cannot supply `tool` and the router used to emit a
    bare `{"call_id": ...}`. Two client integrations are keyed on `data.tool`
    inside the TOOL_RESULT handler and were therefore dead code:
    `use-sse.ts:336` re-fetches the workspace file tree after a
    write/edit/bash, and `use-sse.ts:322` routes todo results to the progress
    panel. The visible symptom was the workspace file list never refreshing
    after the agent wrote a file.

    Also asserts the value stays inside the client's allowlisted vocabulary —
    recovering the name must not smuggle a raw `mcp__<server>__<tool>` back on.
    """
    from services.cowork_agent.adapters.claude_code.streaming import (
        _GENERIC_TOOL, _TOOL_MAP,
    )

    allowed = {t for t, _ in _TOOL_MAP.values()} | {_GENERIC_TOOL[0]}

    fake_dispatcher("multi_tool", gap=0.0)
    stream_id, _ = await start_turn(app)
    session = SSESession(app, f"/api/chat/stream/{stream_id}")
    await session.start()
    frames = await session.read_until(lambda f: f["event"] == "done", timeout=5)

    started = {}
    for f in frames:
        if f["event"] == "tool-call":
            data = json.loads(f["data"])
            started[data["call_id"]] = data["tool"]

    results = [json.loads(f["data"]) for f in frames
               if f["event"] in ("tool-result", "tool-error")]
    assert results, "fixture produced no tool-result frames at all"

    for data in results:
        assert data.get("tool"), (
            f"tool-result for {data['call_id']} has no `tool`; "
            f"use-sse.ts:322/336 can never fire. Frame: {data}"
        )
        assert data["tool"] in allowed, f"non-allowlisted tool name on the wire: {data}"
        if data["call_id"] in started:
            assert data["tool"] == started[data["call_id"]], (
                "the step changed tool between start and finish"
            )

    # `arguments` and raw `output` are PII and must never appear on the SSE path.
    for f in frames:
        if f["event"].startswith("tool-"):
            assert "arguments" not in json.loads(f["data"]), f["data"]


# ── Gate #8 — old-client safety ──────────────────────────────────────────────

async def test_heartbeat_gap_never_exceeds_keepalive(app, fake_dispatcher, monkeypatch):
    """Gate #8. An unmodified client drops the connection after 45 s of silence
    (`constants.ts:532`). With `_KEEPALIVE_INTERVAL` scaled down by 100x and the
    adapter deliberately silent, the max inter-frame gap must stay within the
    keepalive interval — i.e. 20 s in production units, checked in 0.6 s.

    This is the test that would have caught §7.3: forwarding richer events
    resets the `wait_for` timer, heartbeats stop firing, and an old client's
    45 s watchdog trips. Run it BEFORE and AFTER any change to the event set.
    """
    import routers.cowork_agent.chat as chat

    scale = 100.0
    monkeypatch.setattr(chat, "_KEEPALIVE_INTERVAL", 20 / scale)
    fake_dispatcher("multi_tool", gap=0.0, hang_after=1)  # goes quiet immediately

    stream_id, _ = await start_turn(app)
    session = SSESession(app, f"/api/chat/stream/{stream_id}")
    await session.start()
    await session.read_frames(timeout=0.8, stop_on_close=False)
    await session.disconnect()

    assert len(session.frames) >= 3, f"no keepalives at all: {session.frames}"
    worst = max(session.gaps, default=0) * scale
    assert worst <= 20.0 * 1.25, f"max inter-frame gap {worst:.1f}s (scaled) > 20 s"


# ── Gate #9 — no silent failure ──────────────────────────────────────────────

async def test_adapter_error_surfaces_as_agent_error(app, fake_dispatcher, monkeypatch):
    """Gate #9, router half. When the adapter yields an error event the client
    must see `agent-error` carrying the message — not a bare `done`."""
    from services.cowork_agent.engine import dispatcher as dispatcher_mod

    class Failing:
        def __init__(self, name):
            pass

        async def stream(self, *a, **k):
            yield {"type": "error", "error": "claude exited with status 1: boom"}

    monkeypatch.setattr(dispatcher_mod, "AgentDispatcher", Failing)

    stream_id, _ = await start_turn(app)
    session = SSESession(app, f"/api/chat/stream/{stream_id}")
    await session.start()
    frames = await session.read_until(lambda f: f["event"] == "done", timeout=3)

    errors = [f for f in frames if f["event"] == "agent-error"]
    assert errors, f"error swallowed; frames were {[f['event'] for f in frames]}"
    assert "boom" in json.loads(errors[0]["data"])["error_message"]
