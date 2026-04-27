from __future__ import annotations

import json


def parse_stream_line(raw: bytes) -> dict | None:
    """
    Decode one raw line from Claude Code's stream-json output.
    Returns a normalised event dict or None to skip.
    """
    try:
        line = raw.decode("utf-8").strip()
    except (UnicodeDecodeError, AttributeError):
        return None

    if not line:
        return None

    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None

    etype = event.get("type", "")

    if etype == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta":
            text = delta.get("text", "")
            if text:
                return {"type": "token", "token": text}
        return None

    if etype == "assistant":
        message = event.get("message", {})
        parts = []
        for block in message.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    parts.append(text)
        if parts:
            return {"type": "token", "token": "".join(parts)}
        return None

    if etype == "result":
        if event.get("is_error"):
            return {"type": "error", "error": event.get("result", "Claude Code error")}
        return {
            "type": "result",
            "result": event.get("result", ""),
            "session_id": event.get("session_id"),
        }

    if etype == "text":
        text = event.get("text", "") or event.get("content", "")
        if text:
            return {"type": "token", "token": text}
        return None

    if etype == "error":
        return {"type": "error", "error": event.get("error", event.get("message", "unknown error"))}

    return None
