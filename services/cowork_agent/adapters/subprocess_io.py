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
import re
from collections import deque
from typing import Optional, Protocol, Set

__all__ = [
    "STREAM_LIMIT",
    "STDERR_CHUNK_SIZE",
    "STDERR_MAX_BYTES",
    "STDERR_TEXT_CAP",
    "REAP_TIMEOUT_SECONDS",
    "LARGE_LINE_NOTICE_BYTES",
    "StderrTail",
    "cap_text",
    "discard_stream",
    "drain_stderr",
    "large_line_notice",
    "overflow_notice",
    "reap",
    "redact_secrets",
]

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
