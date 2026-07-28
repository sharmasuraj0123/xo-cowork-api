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

@pytest.mark.xfail(
    strict=True,
    reason="docs §9 gate 4 / §2: adapter.py:438 spawns without limit=, so a "
           "200 KB stdout line raises ValueError out of `async for raw_line in "
           "proc.stdout` and the turn — including the final `result` — is lost. "
           "Phase 0.2 must make this pass.",
)
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

@pytest.mark.xfail(
    strict=True,
    reason="docs §9 gate 9 / §4.1(b): stream() never inspects proc.returncode "
           "and never drains stderr, so a CLI that exits 1 produces `[{'done': "
           "True}]` and nothing else. Phase 0.3 must make this pass.",
)
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

@pytest.mark.xfail(
    strict=True,
    reason="docs §9 gate 11 / §4.4: chat_state stores no process handle, so "
           "/api/chat/abort cannot signal the process even in principle. "
           "Phase 3.2/3.3 must make this pass.",
)
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
