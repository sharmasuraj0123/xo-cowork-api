"""Per-turn user prompts for one Claude Code session (Space detail view).

Argus deliberately never ships prompt text in the aggregate sessions payload;
this capability serves it lazily, one session at a time, straight from the
session's own transcript at ``~/.claude/projects/<encoded-cwd>/<id>.jsonl``.
Only top-level user prompts are returned: tool results, sidechain (sub-agent)
records, meta records, and injected tag blocks (``<system-reminder>``,
``<command-name>``, ...) are filtered out.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

SOURCE_ID = "claude_code"
SOURCE_LABEL = "Claude Code"

MAX_PROMPTS = 200          # newest prompts kept; detail card stays bounded
MAX_PROMPT_CHARS = 4000    # per-prompt text cap; the card is a summary,
                           # not a transcript browser
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Injected blocks that ride along inside a typed message. They are stripped so
# the human text they surround survives; a message that is nothing but tags is
# not human input and is dropped.
_TAG_BLOCK_RE = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-stderr)>"
    r".*?</\1>",
    re.DOTALL,
)
# Slash commands arrive fully tag-wrapped; render them as "/name args".
_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def _projects_dir() -> Path:
    configured = (os.getenv("CLAUDE_PROJECTS_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude" / "projects"


def _native_session_id(session_id: str) -> str:
    """Argus namespaces ids as ``claude_code:<native>``; accept both forms."""
    native = session_id.split(":", 1)[1] if ":" in session_id else session_id
    if not _SESSION_ID_RE.fullmatch(native):
        raise ValueError(f"invalid session id: {session_id!r}")
    return native


def _find_transcript(native_id: str) -> Path:
    """Locate ``<native_id>.jsonl`` under any encoded project directory.

    The encoded-cwd directory name cannot be derived from the session id
    alone, so scan one directory level. Newest match wins if a session file
    somehow exists in several project folders.
    """
    root = _projects_dir()
    if not root.is_dir():
        raise FileNotFoundError(
            f"Claude Code projects directory not found at {root}"
        )
    matches = [
        candidate
        for entry in root.iterdir()
        if entry.is_dir()
        and (candidate := entry / f"{native_id}.jsonl").is_file()
    ]
    if not matches:
        raise FileNotFoundError(
            f"No transcript found for session {native_id!r} under {root}"
        )
    return max(matches, key=lambda path: path.stat().st_mtime_ns)


def _clean_text(raw: str) -> str | None:
    """Reduce one text payload to the human-typed part, or None.

    Injected blocks (system reminders, local command output) are stripped so
    the typed text around them survives; a slash command renders as
    ``/name args``; whatever is still fully tag-wrapped or an interrupt marker
    was never typed and is dropped.
    """
    text = _TAG_BLOCK_RE.sub("", raw)
    command = _COMMAND_NAME_RE.search(text)
    if command:
        name = command.group(1).strip()
        args_match = _COMMAND_ARGS_RE.search(text)
        args = args_match.group(1).strip() if args_match else ""
        combined = (name + (" " + args if args else "")).strip()
        return combined or None
    text = text.strip()
    if not text or text.startswith("<") or text.startswith("[Request interrupted"):
        return None
    return text


def _prompt_text(record: dict) -> str | None:
    """Extract typed prompt text from one ``type: "user"`` record, or None.

    Excluded: sub-agent sidechains, meta records, and tool_result-only
    content. Injected tag payloads are stripped by :func:`_clean_text`.
    """
    if record.get("isMeta") or record.get("isSidechain"):
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return _clean_text(content)
    if isinstance(content, list):
        parts = [
            cleaned
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
            and isinstance(item.get("text"), str)
            and (cleaned := _clean_text(item["text"]))
        ]
        return "\n".join(parts) or None
    return None


def collect_session_prompts(session_id: str) -> dict:
    """One entry per exchange: a turn starts at a typed human prompt and owns
    every assistant reply and tool call until the next typed prompt."""
    native_id = _native_session_id(session_id)
    transcript = _find_transcript(native_id)

    prompts: list[dict] = []
    with transcript.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # An active session can end mid-line; earlier records stand.
                continue
            if not isinstance(record, dict):
                continue
            kind = record.get("type")
            if kind == "assistant":
                # Attribute the reply to the exchange in progress. Sidechain
                # (sub-agent) activity is listed separately on the detail page.
                if record.get("isSidechain") or not prompts:
                    continue
                current = prompts[-1]
                current["responses"] += 1
                message = record.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, list):
                    current["tool_uses"] += sum(
                        1 for item in content
                        if isinstance(item, dict) and item.get("type") == "tool_use"
                    )
                continue
            if kind != "user":
                continue
            text = _prompt_text(record)
            if text is None:
                continue
            prompts.append({
                "timestamp": record.get("timestamp"),
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
