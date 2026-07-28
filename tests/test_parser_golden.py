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
    """Task-subagent events carry `parent_tool_use_id`. Their *tool activity* is
    useful progress; their *text* is not this turn's answer. Locks §7.2's
    `from_subagent` rule so a future refactor cannot silently splice a
    subagent's monologue into the user's reply."""
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
    ]
    for raw in junk + capture_lines("multi_tool"):
        out = parse_stream_line(raw)
        assert out is None or isinstance(out, dict), raw[:60]
