"""A fake adapter that replays a committed capture on a controllable clock.

This is the substitute for spawning `claude`. It gives the streaming tests:
  * deterministic event sequences (from the real corpus),
  * a timeline we can scale, so a 83.8 s turn runs in 0.8 s,
  * an explicit "still running" state, so a test can assert what the server
    says *while the turn is alive* — which is the entire false-`done` gate.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import pathlib

from services.cowork_agent.adapters.claude_code.streaming import parse_stream_line

CAPTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "captures"


class FakeDispatcher:
    """Drop-in for `services.cowork_agent.engine.dispatcher.AgentDispatcher`.

    Monkeypatch the *module attribute*, not the import site: `_dispatcher_sse`
    does `from ...dispatcher import AgentDispatcher` at call time, so patching
    `dispatcher.AgentDispatcher` is picked up on the next turn.
    """

    #: set by the factory below — list of (event_dict, delay_before_yield)
    timeline: list[tuple[dict, float]] = []
    hang_after: int | None = None

    #: observable state, for assertions
    started = asyncio.Event()
    finished = asyncio.Event()
    cancelled = asyncio.Event()

    def __init__(self, agent_name: str):
        self.agent_name = agent_name

    async def stream(self, question, session_id=None, **kwargs):
        type(self).started.set()
        try:
            for i, (event, delay) in enumerate(type(self).timeline):
                if type(self).hang_after is not None and i >= type(self).hang_after:
                    await asyncio.sleep(3600)  # turn is alive but silent
                if delay:
                    await asyncio.sleep(delay)
                yield event
            yield {"done": True, "native_session_id": "native-fake"}
            type(self).finished.set()
        except (GeneratorExit, asyncio.CancelledError):
            type(self).cancelled.set()
            raise

    async def ask(self, *a, **k):
        return {"message": ""}

    async def health(self):
        return {"ok": True}


def capture_timeline(name: str, *, scale: float = 1.0) -> list[tuple[dict, float]]:
    """(parsed_event, delay_before_it) pairs, with the capture's REAL timing.

    Every `assistant`/`user` record in a stream-json capture carries an ISO
    `timestamp`; records that don't (`system`, `rate_limit_event`, `result`)
    inherit the previous one, i.e. delay 0. Replaying those deltas divided by
    `scale` reproduces the shape of a real turn — including the 11.5 s silent
    stretches during the tool phase — in a fraction of the wall clock. Nothing
    else reproduces gate #6 honestly; a uniform gap makes the test pass for the
    wrong reason (verified: it XPASSed).
    """
    lines = (CAPTURES / f"{name}.jsonl").read_bytes().splitlines(keepends=True)
    out: list[tuple[dict, float]] = []
    prev: datetime.datetime | None = None
    pending = 0.0
    for raw in lines:
        stamp = json.loads(raw).get("timestamp")
        delay = 0.0
        if stamp:
            now = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            delay = (now - prev).total_seconds() if prev else 0.0
            prev = now
        event = parse_stream_line(raw)
        if event is None or event.get("type") in ("session_id", "result"):
            # Dropped by the adapter — but its *time* still elapses on the wire.
            pending += delay
            continue
        out.append((event, (delay + pending) / scale))
        pending = 0.0
    return out


def dispatcher_from_capture(name: str, *, scale: float = 1.0, gap: float | None = None,
                            hang_after=None):
    """Build a FakeDispatcher class replaying `name` through the REAL parser.

    Running the capture through the production parser (rather than hand-written
    event dicts) is deliberate: the streaming tests then break if the parser's
    output contract changes, which is exactly when the router needs re-checking.

    `scale` replays the capture's own timing compressed by that factor.
    `gap` overrides it with a fixed delay (only for tests that don't care).
    """
    timeline = capture_timeline(name, scale=scale)
    if gap is not None:
        timeline = [(event, gap) for event, _ in timeline]

    return type(
        "FakeDispatcherFrom" + name,
        (FakeDispatcher,),
        {
            "timeline": timeline,
            "hang_after": hang_after,
            "started": asyncio.Event(),
            "finished": asyncio.Event(),
            "cancelled": asyncio.Event(),
        },
    )
