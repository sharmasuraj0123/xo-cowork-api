"""SSE contract gates — docs §9 #5, #6, #8, #9.

All of these run against the real ASGI app with a fake adapter. No `claude`
process, no socket, no network. Whole file runs in about a second.

`test_no_false_done_on_reconnect` is EXPECTED TO FAIL on `cowork-bug-fixes`
before Phase 1.1 lands. That is the point: it is the executable statement of the
bug. Mark it xfail(strict=True) so it flips to a failure the day it is fixed
without the fix and the test-deletion arriving in the same commit.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.support.fake_agent import dispatcher_from_capture
from tests.support.sse import SSESession


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

@pytest.mark.xfail(
    strict=True,
    reason="docs §9 gate 5 / §4.2: done_event.set() lives in the generator's "
           "finally (chat.py:330,349), so it fires on client disconnect. "
           "Phase 1.1 must make this pass.",
)
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


# ── Gate #6 — progress density ───────────────────────────────────────────────

@pytest.mark.xfail(
    strict=True,
    reason="docs §9 gate 6 / §3.1: tool_use and tool_result never become SSE "
           "frames today, so a multi-tool turn is silent apart from heartbeats. "
           "Phase 2.1/2.2 must make this pass.",
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
