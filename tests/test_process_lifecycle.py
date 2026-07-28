"""Subprocess-ownership gates — docs §9 #4, #9 (adapter half), #10, #11.

These drive the REAL `claude_code` adapter against a fake CLI binary
(`tests/support/fake_claude.py`). Real fork/exec, real pipes, real process
groups — because every claim in §4.4 and §7.5 is a claim about the OS:
`proc.kill()` not reaching a reparented grandchild, `start_new_session=True`
being required for `os.killpg`, `asyncio` not killing children on cancellation.
A mocked `create_subprocess_exec` cannot be wrong in the way production was.

Runtime for the whole file: under 5 s.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import signal
import sys

import pytest

FAKE_CLI = pathlib.Path(__file__).resolve().parent / "support" / "fake_claude.py"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@pytest.fixture
def pidfile(tmp_path, monkeypatch):
    """A `FAKE_PIDFILE` whose processes are reaped on teardown, pass OR fail.

    Cleanup written inline at the end of a test only runs when the test gets
    that far. `test_abort_kills_the_whole_process_group` is xfail(strict) and
    fails at its `assert abort is not None` — several statements before any
    kill — so every single run of this file used to leave a `sleep 300` and a
    `fake_claude.py` reparented to PID 1. Measured: `sleep 300` count went 7 ->
    8 per run, forever, on a long-lived dev box or CI runner.

    Teardown SIGKILLs the exact pids the fake CLI recorded, and deliberately
    does NOT use `os.killpg`: whether the fake CLI gets its own process group is
    precisely what gate #11 is testing, so on the failing (current) code path
    its group IS pytest's own — a killpg here would take down the test runner.
    """
    path = tmp_path / "pids"
    monkeypatch.setenv("FAKE_PIDFILE", str(path))
    yield path

    if not path.exists():
        return
    pids = []
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            pids.append(int(parts[1]))
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    # Reap so they do not linger as zombies of the pytest process.
    for pid in pids:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, PermissionError):
            pass


@pytest.fixture
def adapter(tmp_home, monkeypatch):
    """The real Adapter, wired to the fake CLI and a throwaway home."""
    from services.cowork_agent.adapters.claude_code.adapter import Adapter

    monkeypatch.setenv("FAKE_LINE_DELAY", "0.02")
    return Adapter({"cli_path": str(FAKE_CLI), "timeout": 30,
                    "cowork_root": str(tmp_home / "claude-cowork")})


@pytest.fixture
def timed_adapter(tmp_home, monkeypatch):
    """Factory for the real Adapter with a deliberately tiny wall clock.

    The clock is the only thing in this codebase that signals a process, so
    every test of it drives the REAL adapter against a real fake CLI. A mocked
    ``create_subprocess_exec`` has no process group to leave survivors in.
    """
    from services.cowork_agent.adapters.claude_code.adapter import Adapter

    monkeypatch.setenv("FAKE_LINE_DELAY", "0.02")

    def _make(stream_timeout_seconds: float, **config):
        return Adapter({
            "cli_path": str(FAKE_CLI),
            "timeout": 30,
            "stream_timeout_seconds": stream_timeout_seconds,
            "cowork_root": str(tmp_home / "claude-cowork"),
            **config,
        })

    return _make


def read_pids(pidfile) -> dict[str, int]:
    return {
        parts[0]: int(parts[1])
        for parts in (line.split() for line in pidfile.read_text().splitlines())
        if len(parts) == 2 and parts[1].isdigit()
    }


async def drain(gen, *, limit: int = 500) -> list[dict]:
    """Consume an adapter stream, ALWAYS closing the async generator.

    Without the `finally`, breaking out at `limit` — or the generator raising,
    which is exactly what the xfailing oversized-line gate does — leaves the
    generator suspended with a live `BaseSubprocessTransport`. It is then
    finalised by the GC after the event loop has closed, which is the
    "Exception ignored in: BaseSubprocessTransport.__del__ ... RuntimeError:
    Event loop is closed" traceback that pytest.ini's `filterwarnings` was
    silencing. `aclose()` runs the adapter's own cleanup while the loop is
    still alive, so there is nothing left to finalise.
    """
    out = []
    try:
        async for event in gen:
            out.append(event)
            if len(out) >= limit:
                break
    finally:
        await gen.aclose()
    return out


# ── Gate #4 — oversized line ─────────────────────────────────────────────────

@pytest.mark.parametrize("size", [100_000, 140_000, 300_000])
async def test_oversized_line_does_not_lose_the_turn(adapter, monkeypatch, size):
    """A single huge tool_result must cost at most that one step.

    The three sizes are not arbitrary: §2.5 measured CPython switching from the
    "separator IS found" recovery (which deletes exactly the bad line) to the
    "separator is NOT found" recovery (which clears the buffer and hands back a
    mid-line fragment) somewhere between 130 KB and 140 KB. A fix validated only
    at 100 KB is not a fix.
    """
    monkeypatch.setenv("FAKE_OVERSIZE", str(size))
    events = await drain(adapter.stream("hi", None, is_new_session=True))

    text = "".join(e.get("token", "") for e in events if e.get("type") == "token")
    assert "working" in text and "done" in text, (
        f"turn truncated at {size} B: got {len(events)} events, text={text!r}"
    )
    assert events[-1].get("done") is True, "stream did not terminate cleanly"

    warnings = [e for e in events if e.get("phase") == "warning"]
    assert len(warnings) == 1, f"expected exactly one overflow warning, got {len(warnings)}"


# ── Gate #9 — no silent failure (adapter half) ───────────────────────────────

async def test_nonzero_exit_emits_agent_error_with_stderr(adapter, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT_CODE", "1")
    monkeypatch.setenv("FAKE_CAPTURE", os.devnull)  # exit 1 having emitted nothing
    monkeypatch.setenv("FAKE_STDERR", "error: unknown option '--nonexistent-flag'\n")

    events = await drain(adapter.stream("hi", None, is_new_session=True))

    errors = [e for e in events if e.get("type") == "error"]
    assert errors, f"non-zero exit swallowed entirely; events were {events}"
    assert "unknown option" in errors[0]["error"], "stderr text not surfaced"


# ── Gate #10 — disconnect must NOT kill ──────────────────────────────────────

async def test_disconnect_does_not_kill_the_turn(adapter, pidfile, tmp_path, monkeypatch):
    """The single most dangerous regression available in this changeset.

    §7.5: the client disconnects routinely — 45 s heartbeat gap, 15 s stale
    check, 30 s backgrounded tab. v1's `finally: proc.kill()` would have
    SIGKILLed a live turn on every one of those. This test fails on that patch
    and passes today, which is exactly backwards from the other gates in this
    file — so it must be written BEFORE Phase 3 starts, not after.
    """
    progress = tmp_path / "progress"
    monkeypatch.setenv("FAKE_PROGRESS", str(progress))
    monkeypatch.setenv("FAKE_LINE_DELAY", "0.15")

    gen = adapter.stream("hi", None, is_new_session=True)

    async def consume():
        async for _ in gen:
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)                      # let it spawn and emit a line
    assert pidfile.exists(), "fake CLI never started"
    pid = int(pidfile.read_text().split()[1])

    task.cancel()                                 # == the SSE generator closing
    with pytest.raises(asyncio.CancelledError):
        await task
    await gen.aclose()

    before = progress.read_text().count("\n")
    await asyncio.sleep(0.5)
    after = progress.read_text().count("\n")

    assert pid_alive(pid), (
        "the turn was killed on disconnect — this converts 'late answer' into "
        "'destroyed answer' (§7.5). Never kill on generator teardown."
    )
    assert after > before, f"turn stopped producing after disconnect ({before} -> {after})"

    # No inline kill: the `pidfile` fixture owns teardown, so the process is
    # reaped even when an assertion above fails. That is the whole point.


# ── Gate #11 — abort MUST kill, and kill the whole group ─────────────────────

async def test_abort_kills_the_whole_process_group(adapter, pidfile, monkeypatch):
    """`proc.kill()` alone is not enough: a `sleep 300` grandchild reparents to
    init and survives (§4.4, reproduced). The fix is `start_new_session=True` +
    `os.killpg`, as `claude_code/remote_control.py:301,331,344` already does.
    This test asserts the GRANDCHILD is gone, not just the child."""
    monkeypatch.setenv("FAKE_GRANDCHILD", "1")
    monkeypatch.setenv("FAKE_LINE_DELAY", "1.0")

    gen = adapter.stream("hi", None, is_new_session=True)
    task = asyncio.create_task(drain(gen))
    try:
        await asyncio.sleep(0.6)

        lines = dict(l.split() for l in pidfile.read_text().splitlines())
        child, grandchild = int(lines["self"]), int(lines["grandchild"])

        abort = getattr(adapter, "abort", None)
        assert abort is not None, "adapter exposes no abort(); nothing can stop a turn"
        await abort()

        for _ in range(20):                        # 2 s budget, per gate #11
            if not pid_alive(child) and not pid_alive(grandchild):
                break
            await asyncio.sleep(0.1)

        assert not pid_alive(child), "child survived abort"
        assert not pid_alive(grandchild), (
            "grandchild survived abort — proc.kill() does not kill the tree; "
            "use start_new_session=True + os.killpg"
        )
    finally:
        # try/finally, not a trailing statement: this test is xfail(strict) and
        # currently fails at the `abort is not None` assert, so a trailing
        # `task.cancel()` never ran and the adapter's stream task stayed alive
        # holding a subprocess transport — which is what produced the
        # "Event loop is closed" unraisable at interpreter teardown. The
        # `pidfile` fixture kills the OS processes; this releases the task.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task          # let the cancellation actually land


# ── The wall clock — the one thing in this codebase that signals a process ───
#
# Everything below drives the REAL adapter against the real fake CLI with a
# deliberately tiny ``stream_timeout_seconds``. A change that introduces
# SIGKILL of a process group shipped with no tests for the mechanism it
# introduces; each of the failures these cover was reproduced first.


def test_wall_clock_default_is_not_the_run_timeout():
    """1800 s, and NOT ``config["timeout"]``.

    ``timeout`` is 300 s and bounds the non-streaming ``run()`` call. A routine
    24-tool turn measures 83.8 s and ten-minute turns are normal, so a 300 s
    wall clock would SIGKILL legitimate work several times an hour. The two
    values must never be conflated, so this asserts the config key is read and
    the fallback is the documented one.
    """
    from services.cowork_agent.adapters.claude_code.adapter import Adapter
    from services.cowork_agent.adapters.process_owner import (
        DEFAULT_STREAM_TIMEOUT_SECONDS,
    )
    from services.cowork_agent.registry.settings import load_agent_config

    assert DEFAULT_STREAM_TIMEOUT_SECONDS == 1800

    # No key at all, and a `timeout` that must NOT be picked up.
    assert Adapter({"timeout": 300}).stream_timeout_seconds() == 1800.0
    # `load_agent_config` writes None for a *_env key whose variable is unset;
    # that is the normal state, not a misconfiguration.
    assert Adapter({"stream_timeout_seconds": None}).stream_timeout_seconds() == 1800.0
    assert Adapter({"stream_timeout_seconds": 900}).stream_timeout_seconds() == 900.0
    # Non-positive disables rather than kills instantly.
    assert Adapter({"stream_timeout_seconds": 0}).stream_timeout_seconds() == 0.0

    for agent in ("claude_code", "codex"):
        cfg = load_agent_config(agent)
        assert cfg.get("stream_timeout_seconds_env") == "COWORK_STREAM_TIMEOUT_SECONDS", (
            f"{agent} has no env override; an operator whose legitimate turns "
            "exceed the cap would have to edit a git-tracked file"
        )


async def test_wall_clock_kills_members_that_ignore_sigterm(timed_adapter, pidfile):
    """Escalation must key on the GROUP being empty, not on the LEADER exiting.

    Reproduced before the fix: a depth-3 tree whose middle member SIG_IGNs
    SIGTERM returned ``outcome='terminated'`` 11 ms after the SIGTERM — because
    the leader died — and the SIGKILL was never sent. ``survivors={'l2':…,
    'l3':…}``, alive forever. Real ``claude`` tool subprocesses (node children
    with SIGTERM handlers, bash background jobs, MCP servers) are exactly those
    members.
    """
    import os as _os

    from services.cowork_agent.adapters.subprocess_io import KILL_GRACE_SECONDS

    adapter = timed_adapter(0.6)
    monkey = _os.environ
    monkey["FAKE_STUBBORN"] = "1"
    monkey["FAKE_HANG"] = "60"
    try:
        events = await drain(adapter.stream("hi", None, is_new_session=True))
    finally:
        monkey.pop("FAKE_STUBBORN", None)
        monkey.pop("FAKE_HANG", None)

    assert events[-1].get("done") is True, f"turn never terminated: {events}"

    pids = read_pids(pidfile)
    assert {"self", "stubborn", "stubborn_child"} <= set(pids), pids

    deadline = KILL_GRACE_SECONDS + 3.0
    for _ in range(int(deadline / 0.1)):
        if not any(pid_alive(p) for p in pids.values()):
            break
        await asyncio.sleep(0.1)

    survivors = {name: pid for name, pid in pids.items() if pid_alive(pid)}
    assert not survivors, (
        f"process group survived its own kill: {survivors}. SIGTERM→SIGKILL "
        "escalation must be driven by killpg(pgid, 0), not by the leader's "
        "returncode — a group outlives its leader."
    )


async def test_wall_clock_ends_the_turn_when_a_descendant_escaped_the_group(
    timed_adapter, pidfile
):
    """A kill that does not END THE TURN is not a bound at all.

    Reproduced before the fix, twice (a descendant that SIG_IGNs SIGTERM, and
    one that merely ``setsid``s — a daemonising tool subprocess): the group was
    signalled, the leader died, and the adapter stayed parked in
    ``iter_lines(proc.stdout)`` waiting for an EOF that a survivor holding the
    inherited write end can never deliver. 12 s after a 1 s wall clock the
    generator had produced one event and NO terminal ``done``; the turn stayed
    RUNNING until the 3600 s janitor sweep, with the leader already dead so no
    further output could ever arrive.

    ``killpg`` cannot reach the escapee by construction — that is the point.
    The requirement is on the TURN, not on the escapee.
    """
    import os as _os
    import time as _time

    adapter = timed_adapter(0.6)
    _os.environ["FAKE_ESCAPEE"] = "1"
    _os.environ["FAKE_HANG"] = "60"
    try:
        started = _time.monotonic()
        events = await asyncio.wait_for(
            drain(adapter.stream("hi", None, is_new_session=True)), timeout=10
        )
        elapsed = _time.monotonic() - started
    finally:
        _os.environ.pop("FAKE_ESCAPEE", None)
        _os.environ.pop("FAKE_HANG", None)

    assert events[-1].get("done") is True, f"no terminal frame: {events}"
    assert elapsed < 8, f"turn took {elapsed:.1f}s to end after a 0.6 s wall clock"

    pids = read_pids(pidfile)
    for _ in range(20):
        if not pid_alive(pids["self"]):
            break
        await asyncio.sleep(0.1)
    assert not pid_alive(pids["self"]), "the CLI itself was not killed"


async def test_timeout_reaches_the_user_as_an_error_frame(timed_adapter, pidfile):
    """The notice must ride a channel the product client actually renders.

    Measured against the shipped client: ``model-loading`` is a BOOLEAN there
    (``client.on(MODEL_LOADING, (_data, id) => … setModelLoading(true))``,
    use-sse.ts:201) — the label is discarded and rendered nowhere outside the
    debug UI. A 30-minute turn SIGKILLed with a partial answer therefore reached
    the user as a silently truncated reply with ``finish_reason: "stop"``.
    ``type: "error"`` becomes ``agent-error``, which is a toast.
    """
    import os as _os

    adapter = timed_adapter(0.6)
    _os.environ["FAKE_HANG"] = "60"
    _os.environ["FAKE_HANG_AFTER"] = "2"        # answer started, `result` never sent
    try:
        events = await asyncio.wait_for(
            drain(adapter.stream("hi", None, is_new_session=True)), timeout=10
        )
    finally:
        _os.environ.pop("FAKE_HANG", None)
        _os.environ.pop("FAKE_HANG_AFTER", None)

    assert any(e.get("type") == "token" for e in events), (
        f"fixture bug: expected a partial answer before the deadline: {events}"
    )
    errors = [e for e in events if e.get("type") == "error"]
    assert errors, (
        "the timeout was not reported on a channel the client renders; "
        f"events were {events}"
    )
    assert "time limit" in errors[0]["error"], errors
    assert "incomplete" in errors[0]["error"], (
        "a partial answer must be described as incomplete"
    )
    assert not any(
        e.get("phase") == "warning" and "time limit" in e.get("label", "")
        for e in events
    ), "the timeout still rides the discarded model-loading channel"
    assert events[-1].get("done") is True


async def test_a_turn_that_finished_at_the_deadline_is_not_called_truncated(
    timed_adapter, pidfile
):
    """"The clock ran out" is not "we stopped your turn".

    Reproduced before the fix: a CLI that emitted a complete answer and a
    ``result`` event, exited 0, and left a short-lived grandchild holding
    stdout produced ``{'phase':'warning','label':'The answer is incomplete …'}``
    on a turn that was neither incomplete nor stopped — and no signal had been
    sent. The ordinary boundary race (last byte just before the deadline, EOF
    just after) has the same shape.
    """
    import os as _os

    adapter = timed_adapter(0.6)
    _os.environ["FAKE_GRANDCHILD"] = "1"      # holds stdout, outlives the CLI
    try:
        events = await asyncio.wait_for(
            drain(adapter.stream("hi", None, is_new_session=True)), timeout=10
        )
    finally:
        _os.environ.pop("FAKE_GRANDCHILD", None)

    text = "".join(e.get("token", "") for e in events if e.get("type") == "token")
    assert "working" in text and "done" in text, f"answer lost: {events}"
    assert not [e for e in events if e.get("type") == "error"], (
        f"a complete turn was reported as failed/stopped: {events}"
    )
    assert not [e for e in events if "time limit" in str(e.get("label", ""))], (
        f"a complete turn was reported as truncated: {events}"
    )
    assert events[-1].get("done") is True


async def test_abort_reasons_never_reach_a_signal(adapter, pidfile, monkeypatch):
    """CONSTRAINT: POST /api/chat/abort is state-only, at the adapter seam.

    The client fires that endpoint on component UNMOUNT and on SESSION SWITCH,
    not only from a Stop button, and the server cannot tell them apart. This
    asserts the structural guarantee rather than a string comparison: the
    adapter registers NOTHING on the turn handle, so no abort reason — present
    or future, "client", "timeout" or anything a later change invents — has a
    path to a signal.
    """
    from services.cowork_agent.engine.chat_state import TurnHandle

    monkeypatch.setenv("FAKE_LINE_DELAY", "0.15")

    handle = TurnHandle()
    gen = adapter.stream("hi", None, is_new_session=True, turn_handle=handle)
    task = asyncio.create_task(drain(gen))
    try:
        await asyncio.sleep(0.4)
        pid = read_pids(pidfile)["self"]

        assert handle._killers == [], (
            "a killer is registered on the turn handle — POST /api/chat/abort "
            "fires that handle on session switch and would SIGKILL live work"
        )

        for reason in ("client", "timeout", "stream_timeout"):
            TurnHandle().abort(reason)
        handle.abort("client")
        await asyncio.sleep(0.4)

        assert pid_alive(pid), "an abort reason reached a signal"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_watchdog_disarms_on_normal_completion(timed_adapter, pidfile):
    """A finished turn must not leave a live clock behind."""
    import services.cowork_agent.adapters.process_owner as process_owner

    made: list = []
    original = process_owner.StreamWatchdog.__init__

    def spy(self, *args, **kwargs):
        original(self, *args, **kwargs)
        made.append(self)

    process_owner.StreamWatchdog.__init__ = spy
    try:
        adapter = timed_adapter(30)
        events = await drain(adapter.stream("hi", None, is_new_session=True))
    finally:
        process_owner.StreamWatchdog.__init__ = original

    assert events[-1].get("done") is True
    assert len(made) == 1, made
    watchdog = made[0]
    assert not watchdog.expired
    await asyncio.sleep(0.05)          # let the cancellation land
    assert watchdog._task.done(), (
        "the wall clock is still armed after the stream completed"
    )


async def test_watchdog_stays_armed_when_torn_down_over_a_live_child(
    timed_adapter, pidfile, monkeypatch
):
    """The janitor's max-runtime cancel must not remove the only bound.

    ``chat_state.sweep()`` cancels a wedged turn's producer WITHOUT signalling
    anything — that is deliberate. Cancelling runs the adapter's ``finally``
    over a child that is still working (measured: its progress file grew from 8
    to 20 lines after the cancel). Disarming the wall clock there would leave
    that orphan unbounded. Latent at the default pairing (1800 < 3600) and live
    for any operator who raises ``stream_timeout_seconds``, so the invariant is
    asserted here rather than inferred from two constants.
    """
    import services.cowork_agent.adapters.process_owner as process_owner

    made: list = []
    original = process_owner.StreamWatchdog.__init__

    def spy(self, *args, **kwargs):
        original(self, *args, **kwargs)
        made.append(self)

    process_owner.StreamWatchdog.__init__ = spy
    monkeypatch.setenv("FAKE_LINE_DELAY", "0.15")
    try:
        adapter = timed_adapter(30)
        gen = adapter.stream("hi", None, is_new_session=True)

        async def consume():
            async for _ in gen:
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.4)
        task.cancel()                              # == the janitor's sweep
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await gen.aclose()
    finally:
        process_owner.StreamWatchdog.__init__ = original

    assert len(made) == 1, made
    watchdog = made[0]
    pid = read_pids(pidfile)["self"]
    assert pid_alive(pid), "fixture bug: teardown was supposed to leave it running"
    assert not watchdog._task.done(), (
        "the wall clock was disarmed over a live child — the janitor cancel "
        "path would then leave it running with no bound at all"
    )
    watchdog._task.cancel()                        # test-local cleanup only


async def test_http_abort_does_not_signal_the_real_child(app, tmp_home, pidfile, monkeypatch):
    """End-to-end, with real pids: POST /api/chat/abort must not kill anything.

    The adapter-level ``abort()`` capability exists and the gate above proves it
    kills a whole process group. This proves the HTTP endpoint is NOT wired to
    it — through the real router, the real dispatcher and the real claude_code
    adapter spawning a real process.
    """
    import httpx

    from services.cowork_agent.engine import chat_state

    monkeypatch.setenv("CLAUDE_CLI_PATH", str(FAKE_CLI))
    monkeypatch.setenv("FAKE_LINE_DELAY", "0.3")
    monkeypatch.setenv("FAKE_GRANDCHILD", "1")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/chat/prompt",
                         json={"text": "hello", "agent_name": "claude_code"})
        assert r.status_code == 200, r.text
        stream_id = r.json()["stream_id"]

        # The producer only starts when a subscriber connects. Held open in a
        # background task rather than read inline: this test is about what
        # happens WHILE a subscriber is attached, and breaking out of
        # ``aiter_lines`` mid-response does not reliably unwind an ASGI
        # streaming response.
        async def subscribe():
            async with c.stream("GET", f"/api/chat/stream/{stream_id}") as sse:
                async for _ in sse.aiter_lines():
                    pass

        sub = asyncio.create_task(subscribe())
        try:
            for _ in range(50):
                if pidfile.exists() and "self" in read_pids(pidfile):
                    break
                await asyncio.sleep(0.1)

            pids = read_pids(pidfile) if pidfile.exists() else {}
            assert "self" in pids and "grandchild" in pids, f"CLI never spawned: {pids}"

            r = await c.post("/api/chat/abort", json={"stream_id": stream_id})
            assert r.status_code == 200, r.text

            await asyncio.sleep(0.6)

            turn = chat_state.turns.get(stream_id)
            assert turn is not None and turn.state == chat_state.ABORTED
            assert pid_alive(pids["self"]), (
                "POST /api/chat/abort SIGKILLed the CLI. The client fires this "
                "on component unmount and on session switch — this destroys "
                "live work."
            )
            assert pid_alive(pids["grandchild"]), (
                "the abort endpoint reached the process group"
            )
            assert turn.task is not None and not turn.task.done(), (
                "abort cancelled the producer"
            )
        finally:
            # Teardown only. The `pidfile` fixture also reaps, but the child has
            # to die while the loop is still alive or its transport is finalised
            # after the loop closes — the "Event loop is closed" unraisable.
            for pid in pids.values():
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
            sub.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sub
            turn = chat_state.turns.get(stream_id)
            if turn is not None and turn.task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(asyncio.shield(turn.task), timeout=5)
            chat_state.turns.pop(stream_id, None)
            await asyncio.sleep(0.1)
