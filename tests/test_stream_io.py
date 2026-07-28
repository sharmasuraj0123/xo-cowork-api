"""Direct unit tests for the shared subprocess-I/O core.

`stream_lines.py` and `subprocess_io.py` are load-bearing for every CLI-backed
adapter on both planes, and all of their coverage was indirect: two adapter
tests that between them exercise only the KEPT path (lines under the drop
ceiling) and one non-zero exit. Nothing protected the DROP path, the
resynchronisation state machine, or any of the `StderrTail` bounds.

The bounds especially: `StderrTail` retention was written as a cap on the number
of `read()` calls, which for a CLI that flushes one line per write means "the
last 8 lines" — 128 bytes measured, against a documented 512 KiB — so a fatal
message followed by a stack trace lost the fatal message. The existing adapter
test writes stderr exactly once and therefore could not see it. `chunked_reader`
below reproduces that shape deliberately.

Everything here is pure: a scripted async reader, no subprocess, no event loop
beyond pytest-asyncio.
"""
from __future__ import annotations

import asyncio

import pytest

from services.cowork_agent.adapters.stream_lines import (
    DEFAULT_CHUNK_SIZE,
    LineOverflow,
    iter_lines,
)
from services.cowork_agent.adapters.subprocess_io import (
    REDACTED,
    STDERR_MAX_BYTES,
    STDERR_TEXT_CAP,
    StderrTail,
    cap_text,
    discard_stream,
    drain_stderr,
    reap,
    redact_secrets,
)


# ── scripted readers ─────────────────────────────────────────────────────────


class ScriptedReader:
    """Hands back pre-baked chunks, one per ``read()``, then EOF.

    Mirrors `asyncio.StreamReader.read(n)`, which returns whatever is currently
    buffered rather than exactly ``n`` bytes — the property that makes "one
    chunk" mean "one flushed write" in practice.
    """

    def __init__(self, chunks, *, fail_after: int | None = None):
        self._chunks = list(chunks)
        self._i = 0
        self._fail_after = fail_after
        self.reads = 0

    async def read(self, n: int = -1) -> bytes:
        self.reads += 1
        if self._fail_after is not None and self.reads > self._fail_after:
            raise ConnectionResetError("transport closed")
        if self._i >= len(self._chunks):
            return b""
        chunk = self._chunks[self._i]
        self._i += 1
        return chunk


def sliced(payload: bytes, size: int) -> ScriptedReader:
    """A reader that hands ``payload`` back in fixed-size slices."""
    return ScriptedReader([payload[i:i + size] for i in range(0, len(payload), size)])


async def collect(reader, **kw):
    overflows: list[LineOverflow] = []
    kw.setdefault("on_overflow", overflows.append)
    lines = [ln async for ln in iter_lines(reader, **kw)]
    return lines, overflows


# ── iter_lines: the kept path ────────────────────────────────────────────────


async def test_splits_lines_and_strips_terminators():
    lines, _ = await collect(ScriptedReader([b"a\nbb\nccc\n"]))
    assert lines == [b"a", b"bb", b"ccc"]


async def test_final_line_without_newline_is_still_yielded():
    lines, _ = await collect(ScriptedReader([b"a\nno trailing newline"]))
    assert lines == [b"a", b"no trailing newline"]


async def test_empty_lines_are_preserved():
    lines, _ = await collect(ScriptedReader([b"a\n\n\nb\n"]))
    assert lines == [b"a", b"", b"", b"b"]


async def test_crlf_is_stripped():
    lines, _ = await collect(ScriptedReader([b"a\r\nb\r\n"]))
    assert lines == [b"a", b"b"]


async def test_crlf_split_across_the_chunk_boundary():
    """The \\r lands at the end of one read and the \\n at the start of the next.

    A naive splitter leaves a trailing \\r on the line, which for JSON-per-line
    output means a parse failure on a perfectly good record.
    """
    lines, _ = await collect(ScriptedReader([b"hello\r", b"\nworld\r\n"]))
    assert lines == [b"hello", b"world"]


async def test_line_spanning_many_chunks_is_reassembled():
    payload = b"x" * 250_000
    lines, overflows = await collect(sliced(payload + b"\n", 4096))
    assert lines == [payload]
    assert overflows == []


async def test_reader_none_yields_nothing():
    lines, _ = await collect(None)
    assert lines == []


async def test_read_error_is_treated_as_end_of_stream():
    """A broken pipe mid-turn must flush what we have, not raise into the turn."""
    reader = ScriptedReader([b"kept\npartial"], fail_after=1)
    lines, _ = await collect(reader)
    assert lines == [b"kept", b"partial"]


# ── iter_lines: the drop path ────────────────────────────────────────────────


async def test_oversized_line_is_dropped_whole_and_neighbours_survive():
    """The whole design: one bad line costs one line, exactly."""
    payload = b"before\n" + b"y" * 500 + b"\nafter\n"
    lines, overflows = await collect(ScriptedReader([payload]), max_line=100)
    assert lines == [b"before", b"after"]
    assert len(overflows) == 1
    assert overflows[0].dropped_bytes == 500
    assert overflows[0].max_line == 100
    assert overflows[0].at_eof is False


async def test_overflow_reported_once_even_when_split_over_many_chunks():
    """Not once per read: a 1 MiB line at 64 KiB chunks would log 16 times."""
    payload = b"z" * 5000 + b"\ntail\n"
    lines, overflows = await collect(sliced(payload, 16), max_line=100)
    assert lines == [b"tail"]
    assert len(overflows) == 1
    assert overflows[0].dropped_bytes == 5000


async def test_overflow_at_eof_is_flagged():
    """Stream ends before the oversized line is terminated."""
    lines, overflows = await collect(ScriptedReader([b"q" * 400]), max_line=50)
    assert lines == []
    assert len(overflows) == 1
    assert overflows[0].at_eof is True
    assert overflows[0].dropped_bytes == 400


async def test_resync_is_exact_after_a_drop():
    """The line after an oversized tool_result is typically the answer itself."""
    payload = b"a\n" + b"w" * 300 + b"\nb\n" + b"v" * 300 + b"\nc\n"
    lines, overflows = await collect(sliced(payload, 7), max_line=64)
    assert lines == [b"a", b"b", b"c"]
    assert len(overflows) == 2


async def test_memory_is_bounded_by_max_line_not_by_input():
    """The buffer is released the moment the ceiling is crossed, not at line end."""
    huge = b"h" * 400_000
    lines, overflows = await collect(sliced(huge + b"\nsmall\n", 8192), max_line=1024)
    assert lines == [b"small"]
    assert overflows[0].dropped_bytes == 400_000


async def test_a_failing_on_overflow_callback_does_not_cost_the_stream():
    def boom(_overflow):
        raise RuntimeError("reporter exploded")

    lines = [
        ln async for ln in iter_lines(
            ScriptedReader([b"x" * 300 + b"\nkept\n"]), max_line=50, on_overflow=boom
        )
    ]
    assert lines == [b"kept"]


async def test_no_on_overflow_callback_is_fine():
    lines = [ln async for ln in iter_lines(ScriptedReader([b"x" * 300 + b"\nk\n"]), max_line=50)]
    assert lines == [b"k"]


@pytest.mark.parametrize("max_line,chunk_size", [(0, 64), (-5, 64), (100, 0), (100, -1)])
async def test_degenerate_bounds_are_clamped_not_crashed(max_line, chunk_size):
    reader = ScriptedReader([b"ab\ncd\n"])
    lines = [
        ln async for ln in iter_lines(reader, max_line=max_line, chunk_size=chunk_size)
    ]
    # max_line is clamped to >= 1 and chunk_size to the default; either way the
    # generator terminates and never raises.
    assert isinstance(lines, list)


async def test_default_chunk_size_is_not_a_line_length_bound():
    """A line far larger than the read granularity is still delivered intact."""
    payload = b"m" * (DEFAULT_CHUNK_SIZE * 3)
    lines, overflows = await collect(sliced(payload + b"\n", DEFAULT_CHUNK_SIZE))
    assert lines == [payload]
    assert overflows == []


# ── StderrTail: the bounds ───────────────────────────────────────────────────


async def test_retention_is_bounded_by_bytes_not_by_read_count():
    """Regression: the deque was capped at 8 ELEMENTS.

    A CLI that flushes one line per write turns that into "the last 8 lines" —
    measured at 123 bytes against a documented 512 KiB — so the fatal message a
    CLI writes FIRST, before its stack trace, was silently discarded and the
    user saw eight frames of noise instead of the error.
    """
    lines = [b"FATAL: the real error\n"] + [b"    at frame %d\n" % i for i in range(30)]
    tail = drain_stderr(ScriptedReader(lines))
    await tail.settle()

    text = tail.text()
    assert "FATAL: the real error" in text, f"fatal line lost; retained {text!r}"
    assert "at frame 29" in text, "tail lost"


async def test_retention_never_exceeds_the_byte_budget():
    chunk = b"E" * 8192
    tail = StderrTail(ScriptedReader([chunk] * 500), max_bytes=64 * 1024)
    await tail.settle()
    retained = sum(len(c) for c in tail._chunks)
    assert retained <= 64 * 1024, retained
    # ...and it really did keep something near the budget, not almost nothing.
    assert retained > 32 * 1024, retained


async def test_a_single_write_larger_than_the_budget_keeps_its_tail():
    tail = StderrTail(ScriptedReader([b"A" * 5000 + b"FATAL-AT-THE-END"]), max_bytes=1000)
    await tail.settle()
    assert tail.text().endswith("FATAL-AT-THE-END")
    assert sum(len(c) for c in tail._chunks) <= 1000


async def test_oldest_bytes_are_dropped_first():
    tail = StderrTail(
        ScriptedReader([b"oldest\n", b"middle\n", b"newest\n"]), max_bytes=14
    )
    await tail.settle()
    text = tail.text()
    assert "newest" in text
    assert "oldest" not in text


async def test_default_budget_is_the_documented_one():
    tail = StderrTail(ScriptedReader([b"x"]))
    assert tail._max_bytes == STDERR_MAX_BYTES


async def test_pump_ends_at_eof_so_it_cannot_leak():
    tail = drain_stderr(ScriptedReader([b"a\n", b"b\n"]))
    await tail.settle()
    assert tail.done is True


async def test_settle_does_not_cancel_a_pump_that_is_still_running():
    """`settle` uses asyncio.wait, not wait_for, precisely so a slow child keeps
    being drained after the caller gives up waiting for it."""

    class SlowReader:
        def __init__(self):
            self.done_reading = False

        async def read(self, n: int = -1) -> bytes:
            if self.done_reading:
                return b""
            await asyncio.sleep(0.15)
            self.done_reading = True
            return b"late but arrived\n"

    tail = drain_stderr(SlowReader())
    await tail.settle(timeout=0.01)
    assert tail.done is False, "settle should not have waited this out"
    assert tail._task is not None and not tail._task.cancelled()

    await tail.settle(timeout=1.0)
    assert "late but arrived" in tail.text()


async def test_a_reader_that_raises_is_just_end_of_stream():
    tail = drain_stderr(ScriptedReader([b"kept\n"], fail_after=1))
    await tail.settle()
    assert tail.done is True
    assert tail.text() == "kept"


async def test_reader_none_is_inert():
    tail = drain_stderr(None)
    assert tail.done is True
    assert tail.text() == ""


async def test_text_is_capped_and_keeps_the_end():
    tail = drain_stderr(ScriptedReader([b"A" * 5000 + b"\nFATAL: last line here"]))
    await tail.settle()
    text = tail.text()
    assert len(text) <= STDERR_TEXT_CAP
    assert text.endswith("FATAL: last line here"), "tail truncation lost the fatal line"


async def test_text_decodes_invalid_utf8_instead_of_raising():
    """A UnicodeDecodeError here would replace the real failure with a codec
    message — and, on Plane A, skip the terminal event entirely."""
    tail = drain_stderr(ScriptedReader([b"fatal: \xff\xfe\x80 bad bytes \xc3\x28\n"]))
    await tail.settle()
    text = tail.text()
    assert "fatal:" in text
    assert "bad bytes" in text


# ── discard_stream ───────────────────────────────────────────────────────────


async def test_discard_stream_consumes_everything_and_retains_nothing():
    """The abandoned-stdout case: the point is that the child can keep writing,
    not that we keep what it wrote."""
    reader = ScriptedReader([b"y" * 65536] * 40)
    tail = discard_stream(reader)
    await tail.settle()
    assert tail.done is True
    assert tail.text() == ""
    assert sum(len(c) for c in tail._chunks) == 0
    assert reader.reads >= 40, "stream was not actually drained"


# ── reap ─────────────────────────────────────────────────────────────────────


class FakeProc:
    def __init__(self, returncode=None, wait_delay=0.0):
        self.returncode = returncode
        self._delay = wait_delay
        self.stdout = ScriptedReader([b""])
        self.waited = False

    async def wait(self):
        self.waited = True
        await asyncio.sleep(self._delay)
        self.returncode = 0
        return 0


async def test_reap_returns_the_exit_status_on_the_normal_path():
    proc = FakeProc()
    assert await reap(proc) == 0
    assert proc.waited is True


async def test_reap_gives_up_without_signalling_when_the_child_outlives_the_wait():
    proc = FakeProc(wait_delay=5)
    assert await reap(proc, timeout=0.05) is None
    assert proc.returncode is None, "reap must never terminate the process"


async def test_reap_abandoned_does_not_wait_at_all():
    """After a per-line timeout the child is running by definition; waiting for
    it is what left the SSE turn hanging forever."""
    proc = FakeProc(wait_delay=60)
    assert await reap(proc, abandoned=True) is None
    assert proc.waited is False


# ── cap_text ─────────────────────────────────────────────────────────────────


def test_cap_text_leaves_short_text_alone():
    assert cap_text("short", 100) == "short"


def test_cap_text_keeps_the_end_and_respects_the_bound():
    capped = cap_text("A" * 3000 + "FATAL: aborting.", 2000)
    assert len(capped) <= 2000
    assert capped.endswith("FATAL: aborting."), "the last line is the whole point"
    assert capped.startswith("…")


def test_cap_text_is_idempotent():
    """Regression: `text()` capped to 2001 chars and the caller then applied a
    HEAD slice of 2000, shaving the final character off the fatal line the tail
    truncation exists to preserve."""
    once = cap_text("B" * 5000 + "last char matters!", 2000)
    assert cap_text(once, 2000) == once


@pytest.mark.parametrize("cap", [0, -1])
def test_cap_text_with_a_non_positive_cap_is_a_no_op(cap):
    assert cap_text("unchanged", cap) == "unchanged"


def test_cap_text_degenerate_cap_of_one():
    assert cap_text("abcdef", 1) == "…"


# ── redact_secrets ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [
    "env CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-SUPERSECRETVALUE",
    "ANTHROPIC_API_KEY=abcd1234efgh5678",
    'GITHUB_TOKEN: "ghp_0123456789abcdefghijklmnopqrstuvwx"',
    "Authorization: Bearer eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM",
    "aws access_key = AKIAIOSFODNN7EXAMPLE",
    "password: hunter2hunter2",
])
def test_credential_shapes_are_removed(raw):
    out = redact_secrets(raw)
    assert REDACTED in out
    for leak in ("SUPERSECRETVALUE", "abcd1234efgh5678", "ghp_0123456789abcdefghijklmnopqrstuvwx",
                 "SflKxwRJSM", "AKIAIOSFODNN7EXAMPLE", "hunter2hunter2"):
        assert leak not in out


@pytest.mark.parametrize("raw", [
    "error: unknown option '--nonexistent-flag'",
    "FATAL: could not open config, aborting.",
    "config at /home/coder/.claude/.credentials.json",
    "Traceback (most recent call last):\n  File \"x.py\", line 3",
    "",
])
def test_ordinary_diagnostics_survive_untouched(raw):
    """The message has to stay useful — an over-eager scrubber that eats the
    actual error is no better than no error at all."""
    assert redact_secrets(raw) == raw


def test_redaction_keeps_the_key_name_so_the_error_still_reads():
    out = redact_secrets("env CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-SUPERSECRET")
    assert "CLAUDE_CODE_OAUTH_TOKEN" in out
    assert "SUPERSECRET" not in out


async def test_stderr_tail_text_is_scrubbed_end_to_end():
    tail = drain_stderr(ScriptedReader([
        b"Error: auth failed\n",
        b"env CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-SUPERSECRET\n",
    ]))
    await tail.settle()
    text = tail.text()
    assert "auth failed" in text
    assert "SUPERSECRET" not in text
