"""
Chunked, limit-free line reader for subprocess stdout.

WHY THIS EXISTS
---------------
``async for line in proc.stdout`` (and ``proc.stdout.readline()``) is bounded by
:class:`asyncio.StreamReader`'s ``limit`` (64 KiB by default). When a single
line exceeds it, CPython raises out of the iterator and the *whole* read loop
unwinds — for a streaming agent turn that means the remaining output, the final
``result`` record, the usage rollup and the answer itself are all lost. Real
measurements on this project: a live agent stdout line of 124,257 B, and an
on-disk corpus with lines up to 165,547 B. This is routine, not pathological.

CPython has *two* recovery behaviours here and which one fires is timing
dependent (it depends on how much has landed in the buffer when the read
happens), not size deterministic:

  * ``ValueError: Separator is found, but chunk is longer than limit`` —
    ``readuntil`` has already deleted exactly the offending line, so the buffer
    boundary is clean.
  * ``LimitOverrunError``/``Separator is NOT found`` — ``readline`` calls
    ``buffer.clear()`` and the *next* read starts MID-LINE, so the next thing
    you get back is a fragment of the oversized line.

That ambiguity is why neither of the obvious patches works:

  * Raising ``limit=`` is just a bigger buffer. CPython pauses the transport at
    2x ``limit``, so a big limit turns a stall into a memory sink, and a line
    above the new limit still kills the turn.
  * "Catch ValueError, discard the next line to resynchronise" throws away a
    VALID line in the clean case — and the line right after an oversized
    ``tool_result`` is typically the assistant's answer.

THE FIX
-------
Stop calling ``readline()``. ``StreamReader.read(n)`` has no line-length limit
at all: it returns whatever bytes are available, up to ``n``. So we read fixed
size chunks and split on ``b"\\n"`` ourselves. Oversize then becomes *our* state
machine rather than CPython's, and resynchronisation is exact by construction —
we always know whether we are mid-line, so a dropped line is dropped whole and
its neighbours are always intact.

CONTRACT
--------
``iter_lines(reader)`` is an async generator of ``bytes``:

  * yields complete lines with the trailing ``\\n`` (and a preceding ``\\r``, if
    any) removed;
  * a line longer than ``max_line`` is DROPPED WHOLE — never truncated into
    invalid JSON, never yielded as a fragment;
  * ``on_overflow`` is invoked exactly ONCE per oversized line (not once per
    chunk read: a 1 MiB line at 64 KiB chunks would otherwise log 16 times);
  * memory is bounded by ``max_line`` regardless of input size — the internal
    buffer never exceeds ``max_line`` because a line is released the moment it
    crosses the ceiling rather than when it ends. Measured: a 40 MB line with
    the 8 MiB ceiling peaks at 8.39 MB of Python heap, the same as a 12 MB one.
    The one transient 2x is a line that is *kept* at exactly the ceiling, where
    the buffer and the ``bytes`` copy handed to the caller coexist (measured
    16.08 MB for a kept 8 MiB line) — unavoidable when yielding contiguous
    bytes, and still a fixed bound rather than an input-driven one;
  * a final line with no trailing newline is still yielded;
  * ``b""`` (EOF), ``b"\\r\\n"``, empty lines and a chunk boundary landing
    between the ``\\r`` and the ``\\n`` are all handled;
  * it never raises. A read error is treated as end-of-stream. Exceptions from
    ``on_overflow`` are swallowed. ``BaseException`` (``CancelledError``,
    ``GeneratorExit``) deliberately still propagates, so generator teardown
    stays ordinary teardown.

DELIBERATELY NOT IN SCOPE: this module never touches the process. It holds no
reference to it, does not wait on it, does not judge its return code and does
not kill it. It only turns a byte stream into lines. Process lifetime belongs
to the caller.

Agent-agnostic and lives in core alongside ``cli_status.py``, the existing
precedent for a shared adapter utility: nothing here names or knows about any
particular agent backend.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional, Protocol

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "MAX_LINE_BYTES",
    "LineOverflow",
    "iter_lines",
]

# Read granularity. Matches the default StreamReader limit purely so the syscall
# pattern is familiar; it is NOT a line-length bound — a line may span any number
# of chunks. Tuning it trades syscalls against per-read copy size and has no
# effect on correctness.
DEFAULT_CHUNK_SIZE = 64 * 1024

# Hard ceiling on a single line. Above this the line is discarded whole. Sized
# far above anything observed (largest real line seen: 165,547 B ≈ 0.16 MiB) so
# it only ever fires on genuinely runaway output, while still capping worst-case
# buffering at a bounded, survivable amount.
MAX_LINE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class LineOverflow:
    """One oversized line was dropped.

    ``dropped_bytes`` is the full length of the discarded line's payload
    (newline excluded), including the part already buffered when the ceiling was
    crossed — i.e. what the line actually was, not what we happened to be
    holding. ``at_eof`` is True when the stream ended before the line was
    terminated, so ``dropped_bytes`` is all there ever was of it.
    """

    dropped_bytes: int
    max_line: int
    at_eof: bool = False


class _Readable(Protocol):
    async def read(self, n: int = -1) -> bytes: ...


async def iter_lines(
    reader: Optional[_Readable],
    *,
    max_line: int = MAX_LINE_BYTES,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    on_overflow: Optional[Callable[[LineOverflow], None]] = None,
) -> AsyncIterator[bytes]:
    """Yield complete lines from ``reader`` without any line-length limit.

    Drop-in for ``async for raw_line in proc.stdout``. ``reader`` may be None
    (``proc.stdout`` is Optional when stdout was not piped), in which case this
    yields nothing.

    ``on_overflow`` fires once per dropped line. Because it can also fire on the
    very last line before EOF, a caller that turns overflows into user-visible
    frames must drain whatever the callback collected AFTER the loop as well as
    inside it::

        pending: list[LineOverflow] = []
        async for raw in iter_lines(proc.stdout, on_overflow=pending.append):
            while pending:
                yield _warn(pending.pop(0))
            ...
        while pending:
            yield _warn(pending.pop(0))

    Callers that only want a log line can pass a logger call directly.
    """
    if reader is None:
        return
    if max_line < 1:
        max_line = 1
    if chunk_size < 1:
        chunk_size = DEFAULT_CHUNK_SIZE

    def _report(dropped: int, at_eof: bool) -> None:
        if on_overflow is None:
            return
        try:
            on_overflow(LineOverflow(dropped_bytes=dropped, max_line=max_line, at_eof=at_eof))
        except Exception:
            # A misbehaving reporter must not cost the caller its stream. This
            # generator's whole reason to exist is that one bad line costs one
            # line, not the turn.
            pass

    # Bytes of the current line seen so far, only while that line is still under
    # the ceiling. Never exceeds ``max_line``, which is what bounds memory.
    buf = bytearray()
    # True while we are discarding the tail of a line that already blew the
    # ceiling. This is the whole resync mechanism: we know exactly where the
    # dropped line ends, so the next line starts clean.
    dropping = False
    dropped_len = 0

    while True:
        try:
            chunk = await reader.read(chunk_size)
        except Exception:
            # Broken pipe, closed transport, a reader that misbehaves under
            # cancellation — all indistinguishable from "no more output" as far
            # as line assembly is concerned. Flush what we have and stop.
            chunk = b""

        if not chunk:
            break

        start = 0
        n = len(chunk)
        while start < n:
            nl = chunk.find(b"\n", start)

            if nl == -1:
                # No newline in the remainder: it is a partial line. Buffer it,
                # or account it against the line we are already dropping.
                rest_len = n - start
                if dropping:
                    dropped_len += rest_len
                elif len(buf) + rest_len > max_line:
                    # Crossing the ceiling mid-line. Switch to drop mode and
                    # release the buffer immediately — this is what keeps peak
                    # memory at max_line instead of at the true line length.
                    dropped_len = len(buf) + rest_len
                    buf.clear()
                    dropping = True
                else:
                    buf += chunk[start:n]
                break

            seg_len = nl - start

            if dropping:
                # End of the oversized line. Report it once, here, and resume
                # clean at nl + 1.
                _report(dropped_len + seg_len, False)
                dropping = False
                dropped_len = 0
                start = nl + 1
                continue

            if len(buf) + seg_len > max_line:
                # The line ends inside this chunk but is still too long: drop it
                # whole without ever entering drop mode.
                _report(len(buf) + seg_len, False)
                buf.clear()
                start = nl + 1
                continue

            if buf:
                buf += chunk[start:nl]
                line = bytes(buf)
                buf.clear()
            else:
                # Fast path: the whole line lives in this chunk, so slice it out
                # directly rather than round-tripping through the buffer.
                line = chunk[start:nl]

            start = nl + 1
            yield line[:-1] if line.endswith(b"\r") else line

    # EOF. A trailing line with no newline is still a line.
    if dropping:
        _report(dropped_len, True)
    elif buf:
        line = bytes(buf)
        buf.clear()
        yield line[:-1] if line.endswith(b"\r") else line
