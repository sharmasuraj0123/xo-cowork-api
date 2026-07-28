"""Characterisation ("golden") tests for the claude_code stream parser.

WRITE THIS BEFORE TOUCHING THE PARSER. It records what the parser does *today*,
byte for byte, over the whole committed capture corpus. Any parser change that
alters user-visible text then shows up as a diff in a reviewable JSON file
instead of as a support ticket.

Regenerate deliberately:

    XO_UPDATE_GOLDEN=1 venv/bin/pytest tests/test_parser_golden.py
    git diff tests/fixtures/golden/     # <- read every line of this

Gate #7 of docs §9 ("no double-render") is `test_text_is_byte_identical_to_golden`.
It is the reason this file exists: on `claude 2.1.220` the CLI emits the answer
**twice** when `--include-partial-messages` is on — once as `stream_event`/
`text_delta` and once as `assistant`/`text`, and the two are byte-identical
(verified on `partial_messages.jsonl`: 160 B == 160 B). A parser that reads both
sources renders every sentence twice. Only a golden catches that, because both
the buggy and the correct parser look perfectly reasonable in review.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from services.cowork_agent.adapters.claude_code.streaming import parse_stream_line

GOLDEN_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "golden"
UPDATE = os.environ.get("XO_UPDATE_GOLDEN") == "1"

# Events the adapter consumes internally and never forwards to SSE
# (adapter.py: `session_id` -> index patch + continue, `result` -> usage + continue).
INTERNAL_TYPES = {"session_id", "result"}


def project(lines: list[bytes], **kwargs) -> dict:
    """Reduce a capture to the shape a client would actually observe."""
    frames, text = [], []
    for raw in lines:
        event = parse_stream_line(raw, **kwargs) if kwargs else parse_stream_line(raw)
        if event is None:
            continue
        if event.get("type") == "token":
            text.append(event.get("token", ""))
        if event.get("type") not in INTERNAL_TYPES:
            frames.append(event)
    return {
        "n_input_lines": len(lines),
        "n_sse_frames": len(frames),
        "text": "".join(text),
        "frames": frames,
    }


def load_golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text())


def save_golden(name: str, data: dict) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    (GOLDEN_DIR / f"{name}.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    )


def test_golden_projection(capture):
    """Gate #7 (whole-projection form). Every frame the client sees, locked."""
    name, lines = capture
    actual = project(lines)
    if UPDATE:
        save_golden(name, actual)
        pytest.skip(f"golden updated for {name}")
    expected = load_golden(name)
    assert actual["text"] == expected["text"], (
        f"{name}: assistant text changed.\n"
        f"  golden {len(expected['text'])} B, actual {len(actual['text'])} B\n"
        f"  ratio  {len(actual['text']) / max(1, len(expected['text'])):.2f}x "
        f"(2.00x == the v1 double-render bug)"
    )
    assert actual["frames"] == expected["frames"], f"{name}: SSE frame sequence changed"


def test_text_is_byte_identical_to_golden(capture):
    """Gate #7, stated the way docs §9 states it: concatenated user-visible text
    must be byte-identical to today's output. Split out from the frame
    comparison on purpose — Phase 2 *intends* to add tool-call/tool-result
    frames, so the frame list will legitimately change; the text must not."""
    name, lines = capture
    if UPDATE:
        pytest.skip("update mode")
    assert project(lines)["text"] == load_golden(name)["text"]


def test_no_double_render_with_partial_messages(capture_lines):
    """The specific 2.00x regression, asserted directly against the wire fact.

    With `--include-partial-messages` the CLI emits the answer twice. This test
    fails on any parser that reads both `stream_event.text_delta` *and*
    `assistant.text`, without needing a golden to compare against."""
    lines = capture_lines("partial_messages")

    delta_text, assistant_text = [], []
    for raw in lines:
        event = json.loads(raw)
        if event.get("type") == "stream_event":
            inner = event.get("event") or {}
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta":
                    delta_text.append(delta.get("text", ""))
        elif event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content", []):
                if block.get("type") == "text":
                    assistant_text.append(block.get("text", ""))

    # The premise. If this ever fails, the CLI changed and §7.2's whole
    # `partial_messages` switch needs rethinking — which is worth knowing.
    assert "".join(delta_text) == "".join(assistant_text) != ""
    assert not any(json.loads(l).get("type") == "content_block_delta" for l in lines), (
        "top-level content_block_delta appeared; streaming.py:36's branch is no "
        "longer dead code and the envelope-unwrap assumption needs revisiting"
    )

    # The parser must pick exactly one of the two identical sources.
    rendered = project(lines)["text"]
    assert rendered == "".join(delta_text), (
        f"expected {len(''.join(delta_text))} B of text, got {len(rendered)} B "
        f"({len(rendered) / max(1, len(''.join(delta_text))):.2f}x)"
    )


def test_subagent_text_is_not_this_turns_answer(capture_lines):
    """Corpus half: no subagent text from `subagent_task.jsonl` reaches the answer.

    NOTE THE LIMIT. This assertion is currently vacuous as a test of the
    `from_subagent` rule and must not be mistaken for coverage of it: the only
    subagent record in the fixture carrying a `text` block is line 8, a `user`
    message, and no parser has ever rendered `user` text. Verified by running
    both the pre-change and post-change parsers over the corpus — rendered text
    is byte-identical (147 B) under both, so every assertion below passes on a
    parser with the rule deleted. It is kept because it locks the real data;
    the rule itself is covered by `test_subagent_text_is_suppressed_on_every_
    text_branch` below, with synthetic records.
    """
    lines = capture_lines("subagent_task")
    sub = [l for l in lines if json.loads(l).get("parent_tool_use_id")]
    assert len(sub) >= 3, "fixture no longer exercises the subagent path"

    rendered = project(lines)["text"]
    for raw in sub:
        for block in (json.loads(raw).get("message") or {}).get("content", []):
            if block.get("type") == "text" and len(block.get("text", "")) > 40:
                assert block["text"] not in rendered, (
                    "subagent text leaked into the turn's answer"
                )


# Every shape the parser can turn into a `token`. If a branch is added, add it
# here — that is the point of enumerating them rather than testing one.
SUBAGENT_TEXT_SHAPES = {
    "assistant": {
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "SUBAGENT-MONOLOGUE"}]},
    },
    "stream_event": {
        "type": "stream_event",
        "event": {"type": "content_block_delta",
                  "delta": {"type": "text_delta", "text": "SUBAGENT-MONOLOGUE"}},
    },
    "content_block_delta": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": "SUBAGENT-MONOLOGUE"},
    },
    "text": {"type": "text", "text": "SUBAGENT-MONOLOGUE"},
}


@pytest.mark.parametrize("shape", sorted(SUBAGENT_TEXT_SHAPES))
def test_subagent_text_is_suppressed_on_every_text_branch(shape):
    """§7.2's `from_subagent` rule, tested where the corpus cannot reach.

    A Task subagent's tool activity is useful progress; its *text* is the
    subagent's own monologue and splicing it into the reply corrupts the
    answer. `parent_tool_use_id` is the only signal, and it can arrive on any
    of the four shapes that produce a `token`. Three of the four were
    unreachable on claude 2.1.220 — which is exactly why they went unguarded:
    the guard was applied where the corpus proved it mattered and nowhere else,
    so the invariant held by luck, not by construction.

    Each shape is asserted twice — with and without the parent id — so the test
    fails both if suppression stops working AND if it starts over-suppressing
    ordinary top-level text.
    """
    record = SUBAGENT_TEXT_SHAPES[shape]
    kwargs = {"partial_messages": True} if shape == "stream_event" else {}

    plain = parse_stream_line(json.dumps(record).encode(), **kwargs)
    assert plain == {"type": "token", "token": "SUBAGENT-MONOLOGUE"}, (
        f"{shape}: control case — top-level text must still render, got {plain!r}"
    )

    sub = parse_stream_line(
        json.dumps({**record, "parent_tool_use_id": "toolu_parent01"}).encode(), **kwargs
    )
    assert sub is None, (
        f"{shape}: subagent text leaked as {sub!r} — it would be concatenated "
        f"into the user-visible answer"
    )


def test_subagent_tool_activity_is_still_forwarded():
    """The other half of the rule: suppress the subagent's *words*, keep its
    *work*. A Task can run for minutes; dropping its tool frames too would make
    the turn look frozen, which is the bug this whole change set exists to fix.
    """
    started = parse_stream_line(json.dumps({
        "type": "assistant",
        "parent_tool_use_id": "toolu_parent01",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_sub01", "name": "Read", "input": {}}]},
    }).encode())
    assert started == {"type": "tool-call", "tool": "read",
                       "call_id": "toolu_sub01", "title": "Reading files"}

    finished = parse_stream_line(json.dumps({
        "type": "user",
        "parent_tool_use_id": "toolu_parent01",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_sub01", "content": "ok"}]},
    }).encode())
    assert finished == {"type": "tool-result", "call_id": "toolu_sub01"}


def test_malformed_input_never_raises(capture_lines):
    """The parser is the only thing between a hostile/corrupt stdout line and a
    500. Cheap, and it has caught real crashes in every codebase that has it."""
    junk = [
        b"", b"\n", b"   \n", b"not json\n", b"{}\n", b"[]\n", b'"a string"\n',
        b"null\n", b"123\n", b'{"type":"assistant"}\n',
        b'{"type":"assistant","message":{"content":"not-a-list"}}\n',
        b'{"type":"assistant","message":{"content":[null,3,"x"]}}\n',
        b'{"type":"user","message":{"content":"plain prompt echo"}}\n',
        b'{"type":"result"}\n',
        b"\xff\xfe invalid utf8\n",
        # ── the three that actually escaped, each verified end-to-end ────────
        # 1. json.loads raises ValueError, NOT JSONDecodeError, on an integer
        #    literal of >= 4300 digits (CPython sys.int_max_str_digits). ~4.3 KB
        #    — an eighth of the 64 KiB readline limit, so it reaches the parser
        #    through the real pipe. Caught only `json.JSONDecodeError` before.
        b'{"type":"text","text":' + b"1" * 5000 + b"}\n",
        b"1" * 5000 + b"\n",
        # 2. RecursionError (a RuntimeError, so not a ValueError either) from
        #    the C scanner at ~9998 levels. ~20 KB, also under the limit. Depth
        #    of a structured MCP tool_result is a third-party server's choice.
        b"[" * 12000 + b"]" * 12000 + b"\n",
        # 3. Wrong-typed `text`: every other nesting level was guarded, so
        #    `"".join(parts)` raised TypeError and took the turn with it.
        b'{"type":"assistant","message":{"content":[{"type":"text","text":5}]}}\n',
        b'{"type":"assistant","message":{"content":[{"type":"text","text":["a"]}]}}\n',
        b'{"type":"text","text":5}\n',
        b'{"type":"stream_event","event":{"type":"content_block_delta",'
        b'"delta":{"type":"text_delta","text":7}}}\n',
        # non-bytes and non-dict odds and ends
        b'{"type":"rate_limit_event","rate_limit_info":"not-a-dict"}\n',
        b'{"type":"user","message":{"content":[{"type":"tool_result"}]}}\n',
        b'{"type":"assistant","message":{"content":[{"type":"tool_use"}]}}\n',
        b'{"type":"result","errors":[{"message":5},null,7]}\n',
    ]
    for raw in junk + capture_lines("multi_tool"):
        for partial in (False, True):
            out = parse_stream_line(raw, partial_messages=partial)
            assert out is None or isinstance(out, dict), raw[:60]


def test_no_token_frame_ever_carries_a_non_string():
    """A `token` whose value is not a str does not crash — it corrupts.

    chat.py serialises it verbatim and the client does `streamingText + text`,
    so `{"text": 5}` splices a literal "5" into the visible answer. That is
    worse than a crash because nothing reports it. Covers all four branches
    that can emit a token.
    """
    non_strings = [5, 5.5, True, ["a"], {"a": 1}]
    for value in non_strings:
        for record in (
            {"type": "text", "text": value},
            {"type": "content_block_delta",
             "delta": {"type": "text_delta", "text": value}},
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": value}]}},
            {"type": "stream_event",
             "event": {"type": "content_block_delta",
                       "delta": {"type": "text_delta", "text": value}}},
        ):
            out = parse_stream_line(json.dumps(record).encode(), partial_messages=True)
            if out is not None and out.get("type") == "token":
                assert isinstance(out["token"], str), (
                    f"{record['type']} emitted a {type(out['token']).__name__} "
                    f"token: {out!r}"
                )


@pytest.mark.parametrize(
    "status,expect_frame",
    [
        ("allowed", False),
        # THE bug: `allowed_warning` means "you are close to your limit, and
        # your request went through" — it is in the same enum as allowed and
        # rejected (15 occurrences in the 2.1.220 binary, sitting next to
        # "You're close to your usage limit"). `status != "allowed"` classified
        # it as a block, so every turn of every user near their cap opened with
        # a false "Rate limited — waiting" banner while the turn streamed fine.
        ("allowed_warning", False),
        ("rejected", True),
        # Unknown/future values stay silent: a wrong banner on every turn is
        # worse than a missing one, and a real block still surfaces via `result`.
        ("some_future_status", False),
        ("", False),
        (None, False),
        (123, False),
    ],
)
def test_rate_limit_only_reports_actual_blocks(status, expect_frame):
    record = {"type": "rate_limit_event",
              "rate_limit_info": {"status": status, "rateLimitType": "five_hour"}}
    out = parse_stream_line(json.dumps(record).encode())
    assert (out is not None) is expect_frame, (
        f"status={status!r} -> {out!r}"
    )
