"""Subprocess I/O hygiene shared by every CLI-backed adapter.

Companion to ``stream_lines.py``: that module turns a byte stream into lines,
this one covers the *plumbing around* the stream — how much the transport is
allowed to buffer, how stderr is kept from wedging the child, and how a dropped
line is described to a human. Like ``cli_status.py`` it is agent-agnostic: it
names no backend and holds no per-agent knowledge.

WHY A 1 MiB SPAWN LIMIT
-----------------------
``asyncio.create_subprocess_exec(limit=...)`` sizes the ``StreamReader`` buffer.
It is NOT the fix for oversized lines — ``iter_lines`` is, by reading with
``read(n)`` which has no line-length bound at all. The limit only decides how
much the event loop will buffer before it pauses the transport, and CPython
pauses at *twice* ``limit``. Since nothing in this codebase caps the number of
concurrent turns, every megabyte here is a megabyte authorised per live stream,
so a "generous" 32 MiB limit is really a 64 MiB-per-turn memory licence. 1 MiB
is ~6x the largest line ever observed on this project (165,547 B) and bounds the
worst case at something a box can survive.

WHY STDERR MUST BE DRAINED
--------------------------
A piped-but-never-read stderr is a loaded gun, measured twice on this project:

  * the child blocks on ``write(2)`` once the ~64 KiB pipe buffer fills, which
    looks exactly like a hung agent; and
  * ``proc.wait()`` NEVER RETURNS after the child is signalled while stderr is
    piped and undrained — measured still hanging 5 s after SIGKILL with
    ``returncode == -9``; with a drainer running it returned immediately.

The second one is why draining has to be in place *before* anything in this
codebase learns how to kill a turn.

``StderrTail`` therefore reads BYTES (never lines — a line reader here would
carry the very 64 KiB defect it exists to remove) into a bounded ``deque``, in
a background task that deliberately OUTLIVES the caller's generator: an orphaned
turn (client gone, producer still running) still has a child that will wedge if
nobody is reading. It ends by itself at stderr EOF, so it cannot leak, and it
never raises into the turn — it is never awaited on the hot path.

WHY STDOUT MUST BE DRAINED TOO, ONCE THE CONSUMER IS GONE
---------------------------------------------------------
Everything above is equally true of stdout, and draining stderr alone does not
save the child: measured against the real adapter, a client that disconnects
mid-turn leaves the producer running until it has written 2 * ``STREAM_LIMIT``
to stdout, at which point CPython pauses the transport, the child blocks in
``write(2)`` and the turn is wedged forever — with the stderr drainer running
throughout. ``discard_stream`` is the stdout counterpart: once the caller stops
reading but the child is still alive, it consumes and throws away the rest so
the orphan can actually finish (and so a later ``proc.wait()`` can return).
Deliberately retains nothing — nobody is left to show it to.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
from collections import deque
from typing import Optional, Protocol, Set

__all__ = [
    "STREAM_LIMIT",
    "STDERR_CHUNK_SIZE",
    "STDERR_MAX_BYTES",
    "STDERR_TEXT_CAP",
    "REAP_TIMEOUT_SECONDS",
    "LARGE_LINE_NOTICE_BYTES",
    "KILL_GRACE_SECONDS",
    "KILL_CONFIRM_SECONDS",
    "EXIT_POLL_INTERVAL",
    "NON_SIGNALLING_OUTCOMES",
    "StderrTail",
    "cap_text",
    "capture_pgid",
    "close_child_stdout",
    "discard_stream",
    "drain_stderr",
    "large_line_notice",
    "outcome_signalled",
    "overflow_notice",
    "reap",
    "redact_secrets",
    "stdout_eof_received",
    "stream_timeout_notice",
    "terminate_process_group",
    "wait_exited",
]

logger = logging.getLogger(__name__)

# StreamReader buffer for a spawned CLI. See module docstring for why this is
# deliberately modest rather than generous.
STREAM_LIMIT = 1024 * 1024

# stderr drain granularity and retention: at most STDERR_MAX_BYTES are ever
# held, oldest discarded first — a CLI's fatal message is at the end, and the
# whole point is that nothing here grows with child output.
#
# The bound is in BYTES, not in read() calls. That distinction is load-bearing:
# ``StreamReader.read(n)`` returns whatever is currently buffered, so for a CLI
# that flushes one line per write a "chunk" is one LINE. Bounding a deque by
# element count therefore retained ~128 bytes rather than the documented 512 KiB
# (measured), and a fatal message followed by a 30-frame stack trace lost the
# fatal line entirely — defeating the error surfacing this module exists for.
STDERR_CHUNK_SIZE = 64 * 1024
STDERR_MAX_BYTES = 512 * 1024

# How much of the retained tail is ever handed to a caller (and therefore to a
# user-visible error frame).
STDERR_TEXT_CAP = 2000

# How long ``reap`` waits for a child to be collected before giving up on it.
REAP_TIMEOUT_SECONDS = 5.0

# ── termination timings (see ``terminate_process_group``) ────────────────────
#
# Grace between SIGTERM and SIGKILL. Measured SIGTERM→exit latency on this box:
# node (the real CLI runtime) 2.3–5.7 ms with or without a handler, 1.20 s with a
# 1.2 s busy cleanup handler; python 2.6 ms default disposition, 57.6 ms for a
# handler doing a 20 MB write + fsync. The GROUP's slowest member governs, not
# the leader (the leader always died in ~7 ms), and a 1.4 s descendant cleanup
# completed inside this grace with zero survivors. Everything that needed more
# than 1.5 s was synthetic (hardcoded multi-second sleeps in a handler).
#
# This is NOT a correctness knob: correctness comes from the SIGKILL, which
# landed in ~2 ms in every trial including one where all three group members
# SIG_IGN'd SIGTERM. Grace only buys the CLI a chance to flush. Do not grow it —
# it is pure added latency on a path that has already waited half an hour.
KILL_GRACE_SECONDS = 1.5
# Bound on the post-SIGKILL confirmation wait, purely so a caller's timeout task
# cannot wedge. SIGKILL is not refusable; this only caps the observation.
KILL_CONFIRM_SECONDS = 2.0
# Poll granularity for ``wait_exited``. 10 ms: fine enough that a 1.5 s grace is
# not measurably distorted, coarse enough to cost nothing.
EXIT_POLL_INTERVAL = 0.01


# ── secret scrubbing ─────────────────────────────────────────────────────────
#
# stderr is forwarded into a user-visible error frame, and stderr is exactly
# where a CLI dumps its configuration on an auth failure: `env
# CLAUDE_CODE_OAUTH_TOKEN=sk-ant-...`, `Authorization: Bearer ...`. CLAUDE.md's
# coding standards forbid surfacing tokens/secrets/credentials, and a length cap
# is not a filter. These patterns are deliberately narrow — they replace only
# the VALUE, never the surrounding diagnostic text, because the message has to
# stay useful ("unknown option '--nonexistent-flag'" must survive untouched).

# NAME=value / NAME: value where NAME looks like a credential holder.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_.-]*(?:token|secret|password|passwd|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|credential|auth)[A-Z0-9_.-]*)\s*([=:])\s*(\"?[^\s\"']+\"?)"
)
# Bare token shapes, which appear with no key name at all.
_SECRET_LITERALS = [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9._-]{8,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+"),
]

REDACTED = "[redacted]"


def redact_secrets(text: str) -> str:
    """Replace credential-shaped values in ``text``. Never raises.

    Best effort by construction: an allowlist is impossible for arbitrary CLI
    output, so this removes the shapes that are actually observed leaking and
    leaves everything else legible. It is a control, not a guarantee — the real
    guarantee is that adapters do not log stderr anywhere else.
    """
    if not text:
        return text
    try:
        # Literals FIRST. The assignment rule stops at the first whitespace, so
        # on `Authorization: Bearer <jwt>` it would redact the word "Bearer" and
        # leave the token standing. Removing the token shapes up front means the
        # assignment rule only ever sees an already-scrubbed value.
        out = text
        for pattern in _SECRET_LITERALS:
            out = pattern.sub(REDACTED, out)
        return _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    except Exception:
        # Scrubbing must never be the thing that breaks an error path; a failure
        # here means we cannot vouch for the text, so drop it rather than leak.
        return REDACTED


def cap_text(text: str, max_chars: int = STDERR_TEXT_CAP) -> str:
    """Tail-preserving length cap, safe to apply more than once.

    Keeps the END: the fatal line is the last thing a CLI writes. The result is
    at most ``max_chars`` characters INCLUDING the leading ellipsis, which is
    what makes it idempotent — a previously capped string is already within the
    bound, so a second application cannot shave the last character off the very
    line the truncation exists to preserve.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return "…" + text[-(max_chars - 1):]


class _Readable(Protocol):
    async def read(self, n: int = -1) -> bytes: ...


# asyncio keeps only a weak reference to a running task, so a fire-and-forget
# drainer with no other referent can be garbage collected mid-flight — which
# would silently reintroduce the stall this module exists to prevent. Holding a
# strong reference until the task completes is the documented remedy.
_LIVE_DRAINERS: Set["asyncio.Task[None]"] = set()


class StderrTail:
    """Background reader for a child's stderr, retaining a byte-bounded tail.

    Construction starts the pump immediately. The caller keeps the object only
    to read :meth:`text` later; it must NOT cancel it in a ``finally`` — see the
    module docstring on orphaned turns.

    ``max_bytes=0`` retains nothing and turns this into a pure discarder, which
    is what :func:`discard_stream` uses for an abandoned stdout.
    """

    def __init__(
        self,
        reader: Optional[_Readable],
        *,
        chunk_size: int = STDERR_CHUNK_SIZE,
        max_bytes: int = STDERR_MAX_BYTES,
    ) -> None:
        self._chunks: deque[bytes] = deque()
        self._retained = 0
        self._max_bytes = max(0, max_bytes)
        self._chunk_size = chunk_size if chunk_size > 0 else STDERR_CHUNK_SIZE
        self._task: Optional["asyncio.Task[None]"] = None
        if reader is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (only reachable from sync test code): there is
            # nothing to drain into, and this must never be the thing that
            # breaks a turn.
            return
        self._task = loop.create_task(self._pump(reader))
        _LIVE_DRAINERS.add(self._task)
        self._task.add_done_callback(_LIVE_DRAINERS.discard)

    async def _pump(self, reader: _Readable) -> None:
        while True:
            try:
                chunk = await reader.read(self._chunk_size)
            except Exception:
                # Broken pipe / closed transport / a reader misbehaving during
                # teardown are all just "no more stderr". Never propagate: this
                # task is not awaited, so an escaping exception would surface as
                # an unretrieved-exception log and nothing else.
                return
            if not chunk:
                return  # EOF — the child closed stderr (normally: it exited).
            self._retain(chunk)

    def _retain(self, chunk: bytes) -> None:
        """Append ``chunk``, then drop from the FRONT until within the budget.

        Reading is never throttled by this — the child must keep draining
        regardless of how much we keep — so a zero budget still consumes the
        stream, it just holds none of it.
        """
        if self._max_bytes <= 0:
            return
        if len(chunk) > self._max_bytes:
            # One write larger than the whole budget: keep its tail, since that
            # is where a fatal message would be.
            chunk = chunk[-self._max_bytes:]
        self._chunks.append(chunk)
        self._retained += len(chunk)
        while self._retained > self._max_bytes and len(self._chunks) > 1:
            self._retained -= len(self._chunks.popleft())

    @property
    def done(self) -> bool:
        return self._task is None or self._task.done()

    async def settle(self, timeout: float = 1.0) -> None:
        """Wait briefly for the pump to reach EOF, WITHOUT cancelling it.

        ``proc.wait()`` can return a hair before the drainer has consumed the
        last stderr write, so a caller that wants the final message gives it a
        moment. ``asyncio.wait`` (not ``wait_for``) precisely because it leaves
        the task running when the timeout expires.
        """
        if self._task is None or self._task.done():
            return
        await asyncio.wait({self._task}, timeout=timeout)

    def text(self, *, max_chars: int = STDERR_TEXT_CAP) -> str:
        """Decoded, secret-scrubbed, capped tail of what was drained.

        ``errors="replace"`` is not optional: a CLI can and does emit non-UTF-8
        on stderr, and a ``UnicodeDecodeError`` raised out of an error path
        would replace the real failure with a codec message.
        """
        raw = b"".join(self._chunks)
        if not raw:
            return ""
        text = raw.decode("utf-8", "replace").strip()
        return cap_text(redact_secrets(text), max_chars)


def drain_stderr(reader: Optional[_Readable], **kwargs) -> StderrTail:
    """Start a bounded background drain of ``reader``. Never raises."""
    return StderrTail(reader, **kwargs)


async def reap(
    proc: "asyncio.subprocess.Process",
    *,
    abandoned: bool = False,
    timeout: float = REAP_TIMEOUT_SECONDS,
) -> Optional[int]:
    """Wait for ``proc``, bounded, and NEVER signal it. Returns the exit status.

    ``None`` means it is still running and we stopped waiting.

    The bound exists because ``await proc.wait()`` sits on the only path to a
    caller's terminal event. When a caller stops reading early — a per-line
    timeout — the child is by definition still running, and an unbounded wait
    there turns "hung turn" into "hung turn that also reported a timeout": the
    error frame goes out and the stream then never terminates. ``abandoned``
    says so explicitly and skips the wait altogether rather than paying it.

    Either way stdout is handed to a background discarder, because a child we
    have stopped reading blocks in ``write(2)`` and wedges forever otherwise.

    NOT A KILL PATH, deliberately, and it must not become one: an orphaned turn
    is allowed to run to completion. Giving up on the wait abandons our interest
    in the process; it does not stop the process.
    """
    if not abandoned:
        try:
            return await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
    if proc.returncode is None:
        discard_stream(proc.stdout)
    return proc.returncode


async def wait_exited(
    proc: "asyncio.subprocess.Process",
    timeout: float,
    *,
    interval: float = EXIT_POLL_INTERVAL,
) -> bool:
    """Poll ``proc.returncode`` until it is set or ``timeout`` elapses.

    Deliberately NOT ``proc.wait()``. Entered with ``returncode is None`` — which
    is exactly the state of a process you have just signalled — ``wait()``
    registers an exit waiter that is only resolved once EVERY pipe has
    disconnected. Measured on this box, immediately after a ``killpg``:

        stderr drained    → wait() returned in 0.0008–0.0011 s
        stderr undrained  → wait() HUNG (>5 s, every trial) with the pid already
                            gone and ``returncode`` already -9

    The mechanism is flow control: at kill time the StreamReader held 131,103
    buffered bytes with ``_paused=True``, so the transport never saw EOF and
    ``connection_lost`` never fired. A grandchild that escaped the group and
    holds the pipe open reproduces the same hang even WITH a drainer running.
    Since killing is now routine, the kill path must not depend on drainage:
    polling ``returncode`` observed rc=-9 in 0.0060 s with the pipe still full
    and paused.

    Returns True if the process has exited, False on timeout.
    """
    if proc.returncode is not None:
        return True
    deadline = asyncio.get_running_loop().time() + max(timeout, 0.0)
    while proc.returncode is None:
        if asyncio.get_running_loop().time() >= deadline:
            return proc.returncode is not None
        await asyncio.sleep(interval)
    return True


async def terminate_process_group(
    proc: "asyncio.subprocess.Process | None",
    *,
    pgid: Optional[int] = None,
    grace: float = KILL_GRACE_SECONDS,
    reason: str = "unspecified",
) -> str:
    """SIGTERM → grace → SIGKILL the process GROUP led by ``proc``. Never raises.

    THE ONLY KILL PATH IN THIS CODEBASE. ``reap`` above is, and stays,
    non-killing; the adapters' ``finally`` blocks are, and stay, non-killing. A
    client disconnect must never reach this function — a disconnect is routine
    (45 s heartbeat gap, 15 s stale check, 30 s backgrounded tab) and killing on
    one converts "late answer" into "destroyed answer".

    PRECONDITION: ``proc`` was spawned with ``start_new_session=True``, which
    makes the child a session and group leader so that ``proc.pid`` IS its pgid.
    Without that flag the child shares the API SERVER's process group — measured
    here, ``child pgid == server pgid``.

    WHY ``os.killpg(proc.pid, …)`` AND NEVER ``os.killpg(os.getpgid(proc.pid), …)``
    -----------------------------------------------------------------------------
    With the flag the lookup is redundant; without it the lookup returns the
    server's own group and the call is a suicide note. Demonstrated, not argued:
    a sacrificial stand-in server that was its own group leader ran the
    ``getpgid`` form against a child spawned without the flag and died with exit
    code -9; the line after the call never executed. ``proc.pid`` is the only
    value that is either correct or harmlessly ESRCH — it can never resolve to
    the server's group. (``claude_code/remote_control.py:331,344`` ships the
    ``getpgid`` form. It is safe there only because its own spawn sets the flag.
    Do not copy it.)

    WHY THE GROUP AND NOT ``proc.kill()``
    -------------------------------------
    ``proc.kill()`` signals the direct child only. Measured: a ``sleep 300``
    grandchild reparents to init and survives (2 of 3 survived on a depth-3
    tree), and it keeps the inherited stdout/stderr open so ``proc.wait()``
    never returns. ``killpg`` on a depth-3 tree left 0 of 3 survivors, including
    when every member SIG_IGN'd SIGTERM.

    WHY "THE LEADER EXITED" IS NOT "THE GROUP IS GONE"
    -------------------------------------------------
    Both the entry guard and the SIGTERM→SIGKILL escalation used to key on the
    LEADER's ``returncode``, and both were wrong for the same reason: a process
    group outlives its leader. Reproduced here, twice:

      * leader forks a ``sleep 300`` (inheriting stdout) and exits 0 →
        ``returncode`` is 0, ``killpg(pid, 0)`` is still VALID (the group is
        non-empty), and the old entry guard returned "already-exited" having
        signalled nothing. The turn then hung on stdout forever.
      * depth-3 tree where only the MIDDLE process ``SIG_IGN``s SIGTERM → the
        leader died in 11 ms, the old code returned "terminated" and never sent
        the SIGKILL; 2 of 3 members survived indefinitely. Real ``claude`` tool
        subprocesses (node children with SIGTERM handlers, bash background jobs,
        MCP servers) are exactly those members.

    So liveness is asked of the GROUP (``killpg(pgid, 0)``), not of the leader,
    everywhere below. That is safe against pid recycling in the leader-exited
    case only when the pgid was captured while the leader was ALIVE — which is
    what ``pgid`` is for; see :func:`capture_pgid`. Without it, a leader that
    has exited is treated as gone, exactly as before.

    Returns a short outcome string for logging/tests; ``outcome_signalled``
    turns it into "did a signal actually leave this process". Idempotent; safe
    on an already-dead process, safe with ``None``, safe called concurrently.
    """
    if proc is None:
        return "already-exited"

    # LAYER 0 — liveness / pid-recycling guard.
    # asyncio uses PidfdChildWatcher and AUTO-REAPS: measured 0.05 s after a
    # child exited, with wait() never called, the /proc entry was already gone
    # and the pid free for reuse. "Hold the handle to reserve the pid" is true
    # for subprocess.Popen and FALSE here. ``returncode`` is set within
    # 0.49–1.65 µs of the pid being released (8 trials), so it is a tight, sound
    # proxy for "this pid is still mine". Signalling a pid we have already seen
    # exit risks hitting a stranger's entire group after pid wraparound.
    #
    # …EXCEPT when we hold a pgid captured while the leader was alive. A pgid is
    # reserved by the kernel for as long as the group is non-empty (the pid
    # number cannot be handed to a new process while any task still names it as
    # its pgid), so "captured pgid + group still answers signal 0" identifies
    # OUR group with the same confidence ``returncode`` gives for the leader.
    if proc.returncode is not None:
        if pgid is None or pgid != proc.pid or not _group_alive(pgid):
            return "already-exited"
        return await _signal_group_until_empty(
            pgid, proc=None, grace=grace, reason=reason, orphaned=True
        )

    pid = proc.pid
    if not pid or pid <= 1:
        logger.error("refusing to signal implausible pid %r (reason=%s)", pid, reason)
        return "bad-pid"

    own = os.getpgid(0)

    # LAYER 1 — belt: never signal our own group, whatever else is true.
    if pid == own:
        logger.error(
            "refusing to signal own process group %s (reason=%s)", pid, reason
        )
        return "self-group"

    # LAYER 2 — braces: prove ``start_new_session`` actually took effect. If the
    # child is not its own group leader then its group is somebody else's —
    # ours, most likely — and a killpg would take down the API server and every
    # other user's live turn with it. ``actual != pid`` is strictly stronger
    # than ``actual == own``: it also refuses a third party's group.
    try:
        actual = os.getpgid(pid)
    except ProcessLookupError:
        # The leader exited in the microseconds since the returncode check. Its
        # group can still be non-empty — same orphaned-daemon case as LAYER 0.
        if pgid is not None and pgid == pid and _group_alive(pgid):
            return await _signal_group_until_empty(
                pgid, proc=None, grace=grace, reason=reason, orphaned=True
            )
        return "already-exited"
    except PermissionError as exc:                       # pragma: no cover
        logger.error("cannot resolve pgid of %s: %s (reason=%s)", pid, exc, reason)
        return "pgid-unresolvable"

    if actual != pid or actual == own:
        logger.error(
            "child %s is not its own group leader (pgid=%s, server pgid=%s); "
            "start_new_session did not take effect — refusing killpg and "
            "falling back to a single-process terminate (reason=%s)",
            pid, actual, own, reason,
        )
        # Degraded fallback: the direct child only, never a group signal. This
        # leaks any descendants as orphans, which is the correct trade — leaking
        # two children beats SIGKILLing the API server.
        try:
            proc.terminate()
        except (ProcessLookupError, PermissionError):
            return "not-group-leader"
        if not await wait_exited(proc, grace):
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError):
                pass
            await wait_exited(proc, KILL_CONFIRM_SECONDS)
        return "not-group-leader"

    return await _signal_group_until_empty(
        pid, proc=proc, grace=grace, reason=reason, orphaned=False
    )


def _group_alive(pgid: int) -> bool:
    """Does a process group with this id still have members?

    ``killpg(pgid, 0)`` is the only question the kernel answers about a group's
    membership, and it is the RIGHT question: it stays true while any member
    lives, whether or not that member is the leader. ``EPERM`` means the group
    exists and is not ours to signal, which is still "alive".
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                              # pragma: no cover
        return True
    return True


async def _wait_group_gone(
    pgid: int,
    timeout: float,
    *,
    interval: float = EXIT_POLL_INTERVAL,
) -> bool:
    """Poll until the process group is empty or ``timeout`` elapses.

    The replacement for ``wait_exited(proc, …)`` on the kill path. ``wait_exited``
    watches ONE process — the leader — and the whole point of a group signal is
    the members that are not the leader. A zombie leader that asyncio has not
    reaped yet also keeps the group non-empty for a few milliseconds; that only
    costs a poll or two, and signals to a zombie are discarded by the kernel, so
    the worst case is a redundant SIGKILL.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout, 0.0)
    while True:
        if not _group_alive(pgid):
            return True
        if loop.time() >= deadline:
            return not _group_alive(pgid)
        await asyncio.sleep(interval)


async def _signal_group_until_empty(
    pgid: int,
    *,
    proc: "asyncio.subprocess.Process | None",
    grace: float,
    reason: str,
    orphaned: bool,
) -> str:
    """SIGTERM → grace → SIGKILL, escalating on GROUP emptiness. Never raises.

    ``orphaned`` only changes the wording of the logs: it means the leader had
    already exited and the group survived it, which is the case worth spotting
    in production (a tool left a daemon behind).
    """
    what = "orphaned process group" if orphaned else "process group"

    # ProcessLookupError here is normal, not an error: the last member may have
    # exited microseconds before the signal landed.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "already-exited"
    except PermissionError as exc:                       # pragma: no cover
        # Logged and dropped, never widened into a different kill. Degrading to
        # ``os.kill(pid, …)`` here would mask a wrong-group resolution.
        logger.error("not permitted to signal group %s: %s (reason=%s)", pgid, exc, reason)
        return "not-permitted"

    logger.info("sent SIGTERM to %s %s (reason=%s)", what, pgid, reason)

    if await _wait_group_gone(pgid, grace):
        if proc is not None:
            # The group is empty; the leader's returncode may still be a poll
            # behind. Bounded, and never ``proc.wait()`` — see ``wait_exited``.
            await wait_exited(proc, EXIT_POLL_INTERVAL * 10)
        return "terminated"

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except PermissionError as exc:                       # pragma: no cover
        logger.error("not permitted to SIGKILL group %s: %s (reason=%s)", pgid, exc, reason)
        return "not-permitted"

    logger.warning(
        "%s %s still had members %.1fs after SIGTERM; sent SIGKILL (reason=%s)",
        what.capitalize(), pgid, grace, reason,
    )
    gone = await _wait_group_gone(pgid, KILL_CONFIRM_SECONDS)
    if proc is not None:
        await wait_exited(proc, EXIT_POLL_INTERVAL * 10)
    if not gone:
        # SIGKILL is not refusable, so this is uninterruptible sleep or a pid
        # namespace we cannot reach. Say so instead of reporting a clean kill —
        # the one diagnostic an operator has for a leaked tree.
        logger.error(
            "%s %s still has members after SIGKILL (reason=%s)", what.capitalize(), pgid, reason,
        )
        return "killed-survivors"
    return "killed"


def capture_pgid(proc: "asyncio.subprocess.Process | None") -> Optional[int]:
    """Record a child's process group WHILE THE CHILD IS STILL ALIVE.

    Call this immediately after the spawn. It exists for one reason: once the
    leader exits, ``os.getpgid(pid)`` can no longer tell you whether
    ``start_new_session=True`` took effect, and a pid whose process never led a
    group can be recycled into a stranger's pgid. Capturing while the leader is
    alive turns "the leader exited but the group has not" — the orphaned-daemon
    case that used to hang a turn forever — into a case that is safe to signal.

    Returns the pgid, or ``None`` when the group must not be trusted (the flag
    did not take effect, or the group is ours). ``None`` is not an error; it
    degrades ``terminate_process_group`` to exactly its old behaviour.
    """
    if proc is None or proc.returncode is not None:
        return None
    pid = proc.pid
    if not pid or pid <= 1:
        return None
    try:
        actual = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return None
    if actual != pid or actual == os.getpgid(0):
        logger.error(
            "child %s is not its own group leader (pgid=%s, server pgid=%s); "
            "start_new_session did not take effect",
            pid, actual, os.getpgid(0),
        )
        return None
    return actual


#: Outcomes of ``terminate_process_group`` in which NO signal left this process.
#: The distinction is user-visible: "the clock ran out" is not "we stopped your
#: turn", and a turn that finished on its own microseconds before the deadline
#: must not be reported as truncated.
NON_SIGNALLING_OUTCOMES = frozenset({
    "already-exited", "bad-pid", "self-group", "pgid-unresolvable", "not-permitted",
})


def outcome_signalled(outcome: Optional[str]) -> bool:
    """Did ``terminate_process_group`` actually deliver a signal?"""
    return outcome is not None and outcome not in NON_SIGNALLING_OUTCOMES


def stdout_eof_received(proc: "asyncio.subprocess.Process | None") -> bool:
    """Has the child's stdout reader seen EOF (all write ends closed)?

    True means the reader will finish on its own once its buffer is delivered,
    so nobody needs to force anything. False after a kill means some survivor
    still holds the inherited write end.
    """
    reader = getattr(proc, "stdout", None)
    if reader is None:
        return True
    return bool(getattr(reader, "_eof", False))


def close_child_stdout(proc: "asyncio.subprocess.Process | None") -> bool:
    """Close OUR read end of the child's stdout, synthesising EOF for the reader.

    THE ONLY THING THAT CAN END A READ LOOP WHOSE WRITER WILL NEVER CLOSE.
    Reproduced against the real adapter: after the wall clock killed the group,
    a descendant that had escaped it (``start_new_session`` in a Bash tool — a
    dev server, an MCP server) still held the inherited stdout write end, so
    ``async for … in iter_lines(proc.stdout)`` never saw EOF. The turn produced
    no timeout notice, no terminal ``done``, and stayed RUNNING until the 3600 s
    janitor sweep — with the leader already dead, so no further output could
    ever arrive. Measured: still blocked 12 s after a 1 s wall clock.

    Called ONLY after a kill, and only once the reader has been given a chance
    to see a real EOF (see ``StreamWatchdog``), because closing the pipe
    discards whatever the kernel still holds in it.

    Never raises. Returns True if a close/EOF was actually applied.
    """
    if proc is None:
        return False
    candidates = []
    subprocess_transport = getattr(proc, "_transport", None)
    getter = getattr(subprocess_transport, "get_pipe_transport", None)
    if getter is not None:
        try:
            candidates.append(getter(1))
        except Exception:                                # pragma: no cover
            pass
    reader = getattr(proc, "stdout", None)
    candidates.append(getattr(reader, "_transport", None))

    for transport in candidates:
        if transport is None:
            continue
        try:
            transport.close()
        except Exception:                                # pragma: no cover
            continue
        return True

    # Last resort when the private transport layout ever changes under us: feed
    # the EOF straight into the reader. Wakes the parked ``readline`` the same
    # way ``pipe_connection_lost`` would.
    if reader is not None and not getattr(reader, "_eof", False):
        try:
            reader.feed_eof()
            return True
        except Exception:                                # pragma: no cover
            pass
    return False


def discard_stream(reader: Optional[_Readable], **kwargs) -> StderrTail:
    """Consume and throw away the rest of ``reader``, in the background.

    For the stream nobody is listening to any more — an abandoned stdout after
    the client disconnected. Retains nothing (see the module docstring): the
    point is only that the child does not block in ``write(2)`` and can reach
    its own exit.
    """
    kwargs.setdefault("max_bytes", 0)
    return StderrTail(reader, **kwargs)


# ── the two line-size thresholds, and why there are two ──────────────────────
#
# ``stream_lines.MAX_LINE_BYTES`` (8 MiB) is the DROP ceiling: above it a line is
# discarded whole because holding it is the bigger harm. It is deliberately far
# above anything real (largest line ever observed: 165,547 B), so in practice it
# never fires.
#
# This second, much lower value is a NOTICE threshold and drops nothing. 64 KiB
# is the boundary that used to be fatal — it is asyncio's default StreamReader
# limit, i.e. exactly the size at which ``readline()`` raised ValueError and took
# the whole turn with it. Lines above it are real and recurring (19-21 files in
# the capture corpus exceed it; a live stdout line measured 124,257 B), and they
# are now streamed intact.
#
# Why notify at all if nothing is lost: this is the failure mode that used to be
# silent and fatal, and one line at this size means a tool dumped an enormous
# result — worth one transient notice, on the turn where it happens. What it must
# NOT do is drop the line to "prove" the point: an oversized record is usually a
# ``user``/``tool_result`` (its step would then never resolve in the UI) or an
# ``assistant`` record, which IS the answer. Dropping at 64 KiB would delete the
# very content the chunked reader exists to save.
LARGE_LINE_NOTICE_BYTES = 64 * 1024


def overflow_notice(dropped_bytes: int, max_line: int, at_eof: bool = False) -> str:
    """Human-readable text for one line that was DROPPED for exceeding the ceiling.

    Deliberately says the turn continued: the whole design is that one bad line
    costs one line, and a message that reads like a failure would undo that.
    """
    detail = f"{dropped_bytes:,} bytes, limit {max_line:,}"
    if at_eof:
        detail += "; output ended mid-line"
    return f"Skipped one oversized output line ({detail}) — the rest of the turn continued."


def large_line_notice(size: int) -> str:
    """Human-readable text for one very large line that was KEPT.

    Phrased as what is happening, not as damage, because there is none: the line
    was streamed in full. Compare ``overflow_notice``, which reports real loss.
    """
    return f"Handling an unusually large output line ({size:,} bytes) — streamed intact."


def _humanize_seconds(seconds: float) -> str:
    """Render a limit the way an operator would say it. 1800 -> "30 minutes".

    Sub-minute values are only ever produced by tests and by a deliberately
    tightened config, but they must not round to "0 seconds" — a message that
    reports the wrong limit is worse than one that reports an awkward one.
    """
    if seconds >= 3600 and seconds % 3600 == 0:
        hours = int(seconds // 3600)
        return f"{hours} hour" + ("s" if hours != 1 else "")
    if seconds >= 60:
        minutes = seconds / 60 if seconds % 60 else seconds // 60
        # Rounded BEFORE the plural is chosen, or 61 s renders "1 minutes":
        # round(61/60, 1) is 1.0, and the pluralisation has to agree with the
        # number the user is actually shown, not with the one before rounding.
        minutes = round(minutes, 1)
        return f"{minutes:g} minute" + ("s" if minutes != 1 else "")
    value = round(seconds, 2)
    return f"{value:g} second" + ("s" if value != 1 else "")


def stream_timeout_notice(seconds: float, *, partial: bool = False) -> str:
    """Human-readable text for a turn ended by the wall-clock timeout.

    THE POINT OF THIS FUNCTION is that a timeout must not reach the user as
    "was terminated by SIGTERM". That phrasing is correct and useless: it is how
    an operator kill, an OOM kill and this timeout all look from inside the
    adapter, which is exactly why the adapter cannot describe it and the caller
    that scheduled the kill must. Whoever knows WHY the process died owns the
    message.
    """
    limit = _humanize_seconds(seconds)
    if partial:
        return (
            f"The answer is incomplete — this turn was stopped after reaching "
            f"its time limit of {limit}."
        )
    return (
        f"This turn was stopped after reaching its time limit of {limit} "
        f"without producing a response."
    )
