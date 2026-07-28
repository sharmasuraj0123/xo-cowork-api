"""Process ownership for adapters that spawn a CLI. Agent-agnostic.

``subprocess_io`` knows how to END a process group; this module knows WHICH
process group, WHEN, and — the part that matters most — when NOT to.

WHAT OWNS A KILL
----------------
Exactly one thing: the per-stream wall-clock watchdog armed by
:meth:`ProcessOwnerMixin.arm_stream_watchdog`, after
``stream_timeout_seconds`` (default 1800) of a single turn. Nothing else in
this codebase signals a process.

WHAT MUST NEVER REACH A KILL — read before wiring anything new
--------------------------------------------------------------
* ``POST /api/chat/abort``. The client fires it on component UNMOUNT and on
  SESSION SWITCH, not only from a Stop button, and the server cannot tell those
  apart. It calls ``turn.handle.abort("client")``, which is STATE-ONLY.
  Deliberately, NOTHING in this module registers a killer on a ``TurnHandle``:
  an earlier draft registered one gated on a single reason string, and four
  reviewers reached the same conclusion — the gate was the only thing standing
  between the abort endpoint's handle and a SIGKILL of the user's process
  group, for zero functional benefit (the watchdog already terminates
  directly). A guarantee that holds because a code path does not exist beats
  one that holds because a string comparison is correct.
* A client disconnect. The producer task outlives the connection; a subscriber
  going away closes no generator that owns a process.
* ``chat_state.sweep()``'s ``TURN_MAX_RUNTIME`` path, which calls
  ``handle.abort("timeout")``. That is a separate, older, DELIBERATELY
  NON-KILLING mechanism (it cancels the producer task so the adapter's
  ``finally`` can hand stdout to a background discarder and let the child finish
  on its own). It cannot double-fire with the wall clock: the sweep fires at
  3600 s of turn age, the watchdog at ``stream_timeout_seconds`` of a single
  stream. Note :meth:`StreamWatchdog.disarm` — the wall clock is NOT cancelled
  on a teardown that leaves the child running, precisely so that this path
  cannot strip an orphan of its only bound.

WHAT THIS WALL CLOCK IS, PRECISELY
----------------------------------
A TOTAL-RUNTIME CAP ON ONE STREAM, not an inactivity timer. It is armed once at
spawn and nothing in either adapter's read loop resets it, so a turn that has
been streaming tool results continuously for 29:59 is terminated at 30:00
exactly as readily as one that has been silent since minute one. That is the
honest description; do not read the default as "no plausible turn is still
alive". Ten-minute turns are normal here and long agentic refactors exist, so
an operator whose legitimate turns exceed the cap should raise
``stream_timeout_seconds`` (env: ``COWORK_STREAM_TIMEOUT_SECONDS``) rather than
discover it as truncated answers. An idle-based clock would kill the wedged
case without touching the healthy one; it is the better mechanism and it is not
what this is.

ONE ADAPTER INSTANCE ≈ ONE TURN
-------------------------------
``AgentDispatcher.__init__`` constructs a fresh adapter through
``get_adapter``; nothing caches instances. ``_run_dispatcher_turn`` builds one
dispatcher per turn, so the live-process set below holds that turn's child and
no other user's. That is what makes a no-argument :meth:`abort` well defined.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from services.cowork_agent.adapters.subprocess_io import (
    KILL_CONFIRM_SECONDS,
    KILL_GRACE_SECONDS,
    capture_pgid,
    close_child_stdout,
    outcome_signalled,
    stdout_eof_received,
    terminate_process_group,
)

__all__ = [
    "DEFAULT_STREAM_TIMEOUT_SECONDS",
    "EOF_GRACE_SECONDS",
    "STREAM_TIMEOUT_REASON",
    "ProcessOwnerMixin",
    "StreamWatchdog",
    "ensure_stdout_eof",
]

logger = logging.getLogger(__name__)

# Log/diagnostic label for the one thing that is allowed to signal. It is NOT an
# abort reason: no ``TurnHandle`` killer exists, so no abort reason from any
# caller can reach a signal (see the module docstring).
STREAM_TIMEOUT_REASON = "stream_timeout"

# Wall clock for ONE stream, in seconds. Deliberately NOT ``config["timeout"]``
# (=300), which bounds the non-streaming ``run()`` call: a routine 24-tool turn
# measures 83.8 s and ten-minute turns are normal, so 300 s here would SIGKILL
# legitimate work several times an hour.
#
# 1800 is a cap on TOTAL stream runtime, healthy or wedged alike — see the
# module docstring. Operators raise it with COWORK_STREAM_TIMEOUT_SECONDS.
DEFAULT_STREAM_TIMEOUT_SECONDS = 1800

# After the group has been signalled, how long the reader is given to reach a
# real EOF before its pipe is force-closed. A kill normally produces EOF within
# microseconds (every writer died); this window only matters when a descendant
# escaped the group and still holds the inherited write end, and it exists so
# that the last bytes the CLI managed to write are still delivered rather than
# discarded with the pipe.
EOF_GRACE_SECONDS = 2.0


async def ensure_stdout_eof(
    proc: "asyncio.subprocess.Process",
    *,
    label: object = "",
    reason: str = "",
    grace: float = EOF_GRACE_SECONDS,
) -> bool:
    """Guarantee a signalled child's stdout reader can reach EOF.

    A kill does not end a TURN. The adapters' read loop ends at stdout EOF, and
    EOF needs EVERY holder of the inherited write end to be gone — which a
    descendant that escaped the process group is not. Reproduced against the
    real adapter: 12 s after a 1 s wall clock the generator was still parked in
    ``iter_lines``, had emitted no timeout notice and no terminal ``done``, and
    the turn stayed RUNNING until the 3600 s janitor sweep.

    Waits for a genuine EOF first — after a successful group kill every writer
    is gone and it arrives in microseconds — so the ordinary path discards
    nothing that was still in the pipe. Only a stdout held open by a survivor
    reaches the close. Returns True if the pipe had to be force-closed.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(grace, 0.0)
    while True:
        if stdout_eof_received(proc):
            return False
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0.05)
    if close_child_stdout(proc):
        logger.warning(
            "stdout of terminated process group %s was still held open by a "
            "survivor; closed our read end to end the turn (reason=%s)",
            label, reason,
        )
        return True
    return False                                         # pragma: no cover


class StreamWatchdog:
    """A one-shot wall clock that terminates one stream's process group.

    Armed only AFTER the process handle exists. That ordering is load-bearing:
    measured, a killer that fired while the spawn coroutine was still in flight
    found an empty handle, took a ``if proc:`` early return, and SILENTLY SKIPPED
    THE KILL — the spawn then completed and ran to completion unbounded. The
    failure mode is a missed kill, not an exception, so it is invisible.

    THREE THINGS IT DOES, IN ORDER, AND WHY EACH IS NEEDED
    -----------------------------------------------------
    1. Terminate the process GROUP (``pgid`` captured at arm time, while the
       child was alive — that is what lets the group still be reached after its
       leader has exited, the orphaned-daemon case).
    2. Record the OUTCOME. "The clock ran out" is not "we stopped your turn": if
       the process had already finished, nothing was signalled and the user must
       not be told their complete answer was truncated. :attr:`signalled` is the
       flag the adapters branch on; :attr:`expired` alone is not.
    3. Force EOF on stdout if the reader has not seen one. Without this the kill
       does not end the turn at all — reproduced: a descendant that escaped the
       group held the inherited stdout write end and the adapter stayed parked
       in ``iter_lines`` 12 s after a 1 s wall clock, emitting no notice and no
       terminal ``done``.
    """

    __slots__ = ("_proc", "_pgid", "_timeout", "_grace", "_reason", "_task",
                 "expired", "outcome", "signalled")

    def __init__(
        self,
        proc: "asyncio.subprocess.Process",
        timeout: float,
        *,
        grace: float = KILL_GRACE_SECONDS,
        reason: str = STREAM_TIMEOUT_REASON,
    ) -> None:
        self._proc = proc
        # Captured NOW, while the child is certainly alive. See
        # ``subprocess_io.capture_pgid``: after the leader exits, the same
        # question can no longer be answered safely.
        self._pgid = capture_pgid(proc)
        self._timeout = timeout
        self._grace = grace
        self._reason = reason
        self.expired = False
        self.outcome: Optional[str] = None
        self.signalled = False
        self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        try:
            await asyncio.sleep(self._timeout)
        except asyncio.CancelledError:
            return
        # Flag BEFORE signalling: the stream loop reads it to decide whether to
        # describe the ending as a timeout instead of "terminated by SIGTERM",
        # and the SIGTERM can produce EOF on stdout before this coroutine is
        # scheduled again.
        self.expired = True
        logger.warning(
            "stream exceeded %gs wall clock; terminating process group %s",
            self._timeout, self._pgid if self._pgid is not None else self._proc.pid,
        )
        try:
            self.outcome = await terminate_process_group(
                self._proc, pgid=self._pgid, grace=self._grace, reason=self._reason
            )
        except Exception:                                # pragma: no cover
            # terminate_process_group is documented never to raise; belt and
            # braces so a watchdog can never surface as an unhandled task
            # exception on a background loop.
            logger.exception("stream timeout termination failed")
            self.outcome = "error"
        self.signalled = outcome_signalled(self.outcome)

        # Logged, not discarded. Every diagnostic for a leaked process group
        # runs through this line: the warning above says what we were about to
        # do, and without this an operator cannot tell a completed kill from a
        # refused one ("already-exited" sends no signal at all).
        logger.log(
            logging.WARNING if self.outcome in ("killed-survivors", "error") else logging.INFO,
            "stream timeout termination of group %s finished: %s",
            self._pgid if self._pgid is not None else self._proc.pid, self.outcome,
        )

        if self.signalled:
            await self._force_stdout_eof()

    async def _force_stdout_eof(self) -> None:
        await ensure_stdout_eof(
            self._proc,
            label=self._pgid if self._pgid is not None else self._proc.pid,
            reason=self._reason,
        )

    # Worst case for ``_run`` after it fires: SIGTERM grace + SIGKILL
    # confirmation + the stdout EOF grace, plus a margin so that a slow loop
    # cannot make ``settled`` report a stale "nothing was signalled" on a turn
    # that is in fact being killed.
    SETTLE_TIMEOUT = KILL_GRACE_SECONDS + KILL_CONFIRM_SECONDS + EOF_GRACE_SECONDS + 2.0

    async def settled(self, timeout: float = SETTLE_TIMEOUT) -> bool:
        """Wait (bounded) for a fired watchdog to finish, then report the truth.

        Returns True only if a signal ACTUALLY LEFT this process. The adapters
        use the result, not ``expired``, to decide whether to tell the user the
        turn was stopped — a turn whose CLI exited on its own microseconds
        before the deadline is complete, and reporting it as truncated is a
        false claim of data loss.
        """
        if not self.expired:
            return False
        if not self._task.done():
            try:
                await asyncio.wait({self._task}, timeout=max(timeout, 0.0))
            except asyncio.CancelledError:
                raise
        return self.signalled

    def disarm(self) -> None:
        """Cancel the clock — but only when there is nothing left to bound.

        NO-OP ONCE ``expired`` IS SET. After it fires, the task is no longer a
        clock: it is running the SIGTERM→SIGKILL escalation and then the stdout
        EOF guarantee. Cancelling there would abandon a process that ignored
        SIGTERM with the SIGKILL never sent, and could leave the read loop
        parked forever.

        NO-OP WHILE THE CHILD IS STILL RUNNING, for a subtler reason. This is
        called from the adapters' ``finally``, and the one thing that closes
        those generators early is ``chat_state.sweep()``'s max-runtime cancel —
        which deliberately sends no signal and hands stdout to a background
        discarder. Disarming there would strip a live, unwatched child of the
        ONLY bound it has left. Measured on an instrumented run: the child kept
        working (its progress file grew from 8 to 20 lines) after the producer
        was cancelled. Leaving the clock armed costs one pending task and kills
        that orphan at the same deadline it would have had anyway.

        The normal path — the CLI exited, the stream ended — does disarm here.
        """
        if self.expired:
            return
        if self._proc.returncode is None:
            logger.debug(
                "stream teardown with the child still running; leaving the wall "
                "clock armed as its only bound (pid=%s)", self._proc.pid,
            )
            return
        if not self._task.done():
            self._task.cancel()


class ProcessOwnerMixin:
    """Adds process ownership — tracking, ``abort()``, and the wall clock.

    Mixed into an adapter that spawns a CLI. Adapters that own no process
    (httpx / temp-file lanes) must NOT mix this in: there is nothing to own.
    """

    #: Live children of THIS adapter instance, keyed by pid, with the pgid
    #: captured while each was alive.
    _live_procs: dict[int, tuple["asyncio.subprocess.Process", Optional[int]]]

    def _owned(self) -> dict[int, tuple["asyncio.subprocess.Process", Optional[int]]]:
        procs = getattr(self, "_live_procs", None)
        if procs is None:
            procs = {}
            self._live_procs = procs
        return procs

    # ── tracking ────────────────────────────────────────────────────────────

    def track_process(
        self,
        proc: "asyncio.subprocess.Process",
        pgid: Optional[int] = None,
    ) -> Optional[int]:
        """Start owning ``proc``. Captures its pgid while it is alive.

        The capture is the whole value of doing this at spawn time: once the
        leader has exited, nothing can prove afterwards that its pid was ever a
        pgid, and signalling an unproven one risks a stranger's group.
        """
        if pgid is None:
            pgid = capture_pgid(proc)
        self._owned()[proc.pid] = (proc, pgid)
        return pgid

    def release_process(self, proc: Optional["asyncio.subprocess.Process"]) -> None:
        """Stop tracking. NEVER signals — this runs in the stream's ``finally``,
        which is also the client-disconnect path."""
        if proc is not None:
            self._owned().pop(proc.pid, None)

    # ── configuration ───────────────────────────────────────────────────────

    def stream_timeout_seconds(self) -> float:
        """Resolve the wall clock, in seconds. 0 disables it.

        ``None`` is not a misconfiguration: ``load_agent_config`` writes ``None``
        for a ``*_env`` key whose environment variable is unset, which is the
        normal state for ``stream_timeout_seconds_env``. Treating it as invalid
        logged a warning on every single turn.
        """
        raw = getattr(self, "config", {}).get(
            "stream_timeout_seconds", DEFAULT_STREAM_TIMEOUT_SECONDS
        )
        if raw is None or raw == "":
            return float(DEFAULT_STREAM_TIMEOUT_SECONDS)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "invalid stream_timeout_seconds=%r; using %s",
                raw, DEFAULT_STREAM_TIMEOUT_SECONDS,
            )
            return float(DEFAULT_STREAM_TIMEOUT_SECONDS)
        if value <= 0:
            # A non-positive value disables the wall clock rather than killing
            # instantly — the safe reading of a misconfiguration. It also means
            # NOTHING bounds the child; that is the operator's explicit choice.
            return 0.0
        return value

    # ── the wall clock ──────────────────────────────────────────────────────

    def arm_stream_watchdog(
        self,
        proc: "asyncio.subprocess.Process",
        **_ignored,
    ) -> Optional[StreamWatchdog]:
        """Start this stream's wall clock. Returns ``None`` when it is disabled.

        ``**_ignored`` swallows keyword arguments a caller may still pass (an
        earlier draft took ``turn_handle=`` here to register a killer on the
        turn's abort handle; that killer is gone — see the module docstring —
        and passing the handle is now inert rather than an error).
        """
        # Tracked FIRST and unconditionally: ownership is not conditional on the
        # wall clock being enabled, and ``abort()`` with no argument means "every
        # child this adapter instance owns".
        self.track_process(proc)
        timeout = self.stream_timeout_seconds()
        if timeout <= 0:
            logger.warning(
                "stream wall clock disabled by configuration; nothing bounds "
                "this turn's process group (pid=%s)", proc.pid,
            )
            return None
        return StreamWatchdog(proc, timeout)

    # ── the capability ──────────────────────────────────────────────────────

    async def abort(
        self,
        proc: Optional["asyncio.subprocess.Process"] = None,
        *,
        reason: str = "adapter-abort",
        grace: float = KILL_GRACE_SECONDS,
    ) -> list[str]:
        """Terminate the process GROUP of ``proc``, or of every live child.

        A CAPABILITY, not a route. Exposing it does NOT mean the HTTP abort
        endpoint may call it — that endpoint stays state-only (see the module
        docstring). Its only production caller is the wall-clock watchdog.

        Idempotent, safe on an already-dead process, safe to call twice,
        never raises. Returns one outcome string per process signalled.
        """
        if proc is not None:
            targets = [(proc, self._owned().get(proc.pid, (proc, None))[1])]
        else:
            targets = list(self._owned().values())
        outcomes: list[str] = []
        for target, pgid in targets:
            outcomes.append(
                await terminate_process_group(
                    target, pgid=pgid, grace=grace, reason=reason
                )
            )
            # A kill only ends the TURN if the read loop can reach EOF; a
            # descendant that escaped the group keeps the inherited stdout write
            # end open. Same guarantee the watchdog makes, for the same reason.
            if outcome_signalled(outcomes[-1]):
                await ensure_stdout_eof(target, label=pgid or target.pid, reason=reason)
            self.release_process(target)
        return outcomes
