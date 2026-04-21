"""
Convert OpenClaw's JSONL message records into the xo-cowork `MessageResponse`
shape consumed by the frontend.

Entry point: `convert_messages(session_id, records)`. Everything else here is
a helper that emits the text/reasoning/tool-call/tool-result `parts` array the
UI expects.
"""

from services.cowork_agent.helpers import iso_now, short_id


def _normalize_content(msg: dict) -> str:
    """Extract text from a message's content for deduplication comparison."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if b.get("type") == "text")
    return ""


def convert_messages(session_id: str, records: list[dict]) -> list[dict]:
    """Convert OpenClaw JSONL message records to xo-cowork MessageResponse format."""
    messages = []
    last_user_content: str | None = None

    for record in records:
        if record.get("type") != "message":
            continue

        msg = record.get("message", {})
        role = msg.get("role", "")
        record_id = record.get("id", short_id())
        timestamp = record.get("timestamp", iso_now())

        if role == "toolResult":
            _attach_tool_result(messages, msg)
            continue

        if role == "user":
            # Deduplicate consecutive user messages with identical content
            # (OpenClaw's bootstrap re-appends the user message after context loading)
            content_text = _normalize_content(msg)
            if content_text and content_text == last_user_content:
                continue
            last_user_content = content_text

            parts = _convert_user_parts(record_id, session_id, timestamp, msg)
            messages.append({
                "id": record_id,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {"role": "user"},
                "parts": parts,
            })

        elif role == "assistant":
            parts = _convert_assistant_parts(record_id, session_id, timestamp, msg)
            usage = msg.get("usage", {})
            cost = usage.get("cost", {})
            cost_total = cost.get("total") if isinstance(cost, dict) else cost

            messages.append({
                "id": record_id,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {
                    "role": "assistant",
                    "model_id": msg.get("model"),
                    "provider_id": msg.get("provider"),
                    "cost": cost_total,
                    "tokens": {
                        "input": usage.get("input", 0),
                        "output": usage.get("output", 0),
                        "reasoning": 0,
                        "cache_read": usage.get("cacheRead", 0),
                        "cache_write": usage.get("cacheWrite", 0),
                    } if usage else None,
                    "finish": _map_stop_reason(msg.get("stopReason")),
                    "error": None,
                },
                "parts": parts,
            })

    return messages


def _convert_user_parts(msg_id, session_id, timestamp, msg):
    parts = []
    for block in msg.get("content", []):
        if block.get("type") == "text":
            parts.append({
                "id": f"{msg_id}_p{len(parts)}",
                "message_id": msg_id,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {"type": "text", "text": block["text"]},
            })
    return parts


def _convert_assistant_parts(msg_id, session_id, timestamp, msg):
    parts = []
    for block in msg.get("content", []):
        btype = block.get("type")

        if btype == "text":
            text = block.get("text", "")
            if text.startswith("[["):
                closing = text.find("]]")
                if closing != -1:
                    text = text[closing + 2:].strip()
            parts.append({
                "id": f"{msg_id}_p{len(parts)}",
                "message_id": msg_id,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {"type": "text", "text": text},
            })

        elif btype == "thinking":
            thinking_text = block.get("thinking", "")
            if thinking_text:
                parts.append({
                    "id": f"{msg_id}_p{len(parts)}",
                    "message_id": msg_id,
                    "session_id": session_id,
                    "time_created": timestamp,
                    "data": {"type": "reasoning", "text": thinking_text},
                })

        elif btype == "toolCall":
            parts.append({
                "id": f"{msg_id}_p{len(parts)}",
                "message_id": msg_id,
                "session_id": session_id,
                "time_created": timestamp,
                "data": {
                    "type": "tool",
                    "tool": block.get("name", "unknown"),
                    "call_id": block.get("id", ""),
                    "state": {
                        "status": "completed",
                        "input": block.get("arguments", {}),
                        "output": None,
                        "metadata": None,
                        "title": block.get("name", "tool"),
                        "time_start": timestamp,
                        "time_end": timestamp,
                        "time_compacted": None,
                    },
                },
            })

    return parts


def _attach_tool_result(messages, tool_result_msg):
    tool_call_id = tool_result_msg.get("toolCallId", "")
    result_content = tool_result_msg.get("content", [])
    output_text = ""
    for block in result_content:
        if block.get("type") == "text":
            output_text += block.get("text", "")

    for msg in reversed(messages):
        if msg["data"].get("role") != "assistant":
            continue
        for part in msg["parts"]:
            if (
                part["data"].get("type") == "tool"
                and part["data"].get("call_id") == tool_call_id
            ):
                part["data"]["state"]["output"] = output_text
                if tool_result_msg.get("isError"):
                    part["data"]["state"]["status"] = "error"
                return


def _map_stop_reason(reason):
    mapping = {
        "stop": "stop",
        "toolUse": "tool_use",
        "length": "length",
        "error": "error",
    }
    return mapping.get(reason or "", None)
