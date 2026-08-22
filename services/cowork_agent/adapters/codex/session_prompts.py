"""Per-turn user prompts for one Codex session (Space detail view).

The aggregate telemetry payload never retains prompt text; this capability
serves it lazily for a single session the user has opened. The thread's
rollout JSONL (resolved through the state DB, path-checked to stay inside
``<CODEX_HOME>/sessions``) is scanned for ``event_msg``/``user_message``
events — the user's typed messages. Injected tag blocks
(``<environment_context>``, ``<user_instructions>``, ...) are filtered out.
"""

from __future__ import annotations

import json
import re

from .session_telemetry import (
    _codex_home,
    _connect_ro,
    _find_state_db,
    _safe_rollout,
)

SOURCE_ID = "codex"
SOURCE_LABEL = "Codex"

MAX_PROMPTS = 200
MAX_PROMPT_CHARS = 4000
MAX_ROLLOUT_BYTES = 256 * 1024 * 1024  # refuse to scan a pathological log

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_EVENT_TYPES = ("user_message", "agent_message", "function_call", "custom_tool_call")
_MARKERS = tuple(
    marker
    for event_type in _EVENT_TYPES
    for marker in (
        f'"type":"{event_type}"'.encode(),
        f'"type": "{event_type}"'.encode(),
    )
)


def _rollout_path_for(session_id: str):
    root = _codex_home()
    state_path = _find_state_db(root)
    connection = _connect_ro(state_path)
    try:
        row = connection.execute(
            "select rollout_path from threads where id=?", (session_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise FileNotFoundError(f"Codex session {session_id!r} not found")
    rollout = _safe_rollout(root, str(row[0] or ""))
    if rollout is None:
        raise FileNotFoundError(
            f"Rollout log for Codex session {session_id!r} is unavailable"
        )
    return rollout


def collect_session_prompts(session_id: str) -> dict:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"invalid session id: {session_id!r}")
    rollout = _rollout_path_for(session_id)
    if rollout.stat().st_size > MAX_ROLLOUT_BYTES:
        raise ValueError(
            f"Rollout log for Codex session {session_id!r} is too large to scan"
        )

    # One entry per exchange: a turn starts at a typed user message and owns
    # every agent reply and tool call until the next typed message.
    prompts: list[dict] = []
    with rollout.open("rb") as handle:
        for raw_line in handle:
            if not any(marker in raw_line for marker in _MARKERS):
                continue
            try:
                event = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Active rollouts can end in a partial line while the API is
                # reading. Earlier complete events remain trustworthy.
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            payload_type = payload.get("type")
            event_type = event.get("type")
            if event_type == "response_item" and payload_type in (
                "function_call", "custom_tool_call"
            ):
                if prompts:
                    prompts[-1]["tool_uses"] += 1
                continue
            if event_type != "event_msg":
                continue
            if payload_type == "agent_message":
                if prompts:
                    prompts[-1]["responses"] += 1
                continue
            if payload_type != "user_message":
                continue
            message = payload.get("message")
            if not isinstance(message, str):
                continue
            text = message.strip()
            if not text or text.startswith("<"):
                continue
            prompts.append({
                "timestamp": event.get("timestamp"),
                "text": text[:MAX_PROMPT_CHARS],
                "truncated": len(text) > MAX_PROMPT_CHARS,
                "responses": 0,
                "tool_uses": 0,
            })

    total = len(prompts)
    prompts = prompts[-MAX_PROMPTS:]
    for turn, prompt in enumerate(prompts, start=total - len(prompts) + 1):
        prompt["turn"] = turn
    return {
        "source": {"id": SOURCE_ID, "label": SOURCE_LABEL},
        "session_id": session_id,
        "supported": True,
        "total_prompts": total,
        "capped": total > len(prompts),
        "prompts": prompts,
    }
