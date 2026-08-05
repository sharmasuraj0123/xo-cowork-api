"""
Antigravity (agy) transcript + log parsing — shared by adapter / sessions /
usage / visualizer.

Because agy has **no JSON output mode**, the authoritative record of a run is the
event-sourced ``transcript_full.jsonl`` under
``brain/<conversation-uuid>/.system_generated/logs/`` (NOT stdout, which is
human-readable narrative interleaved with task noise). The conversation id is
never printed; it is recovered from the ``--log-file`` (``conversation=<uuid>``),
with fallbacks to ``cache/last_conversations.json``, the workspace-indexed
``conversation_summaries.db``, and finally newest-brain-dir.

Event envelope (verified, agy v1.1.2): ``step_index`` (int), ``source`` ∈
{USER_EXPLICIT, MODEL, SYSTEM}, ``type``, ``status`` ∈ {RUNNING, DONE},
``created_at`` (RFC3339, whole-second UTC), optional ``content``, ``tool_calls``,
``error``. The **final answer** is the last ``PLANNER_RESPONSE`` that carries
``content`` and **no** ``tool_calls`` — byte-identical to what ``-p`` printed.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlparse

from services.cowork_agent.adapters.antigravity.paths import (
    AGY_HOME,
    BRAIN_DIR,
    LAST_CONVERSATIONS,
    transcript_path,
)

# The agy-maintained SQLite index of conversations (one row per conversation),
# keyed by ``conversation_id`` with a ``workspace_uris`` JSON array and a
# ``last_modified_time``. Derived locally from the state ROOT (no paths.py edit).
_SUMMARIES_DB: Path = AGY_HOME / "conversation_summaries.db"

_CONV_RE = re.compile(r"conversation=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


# ── Conversation-id resolution ────────────────────────────────────────────────


def conversation_id_from_log(log_path: str | Path | None) -> str | None:
    """Extract ``conversation=<uuid>`` from an agy ``--log-file`` (first match)."""
    if not log_path:
        return None
    p = Path(log_path)
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _CONV_RE.search(text)
    return m.group(1) if m else None


def conversation_id_for_cwd(cwd: str | Path) -> str | None:
    """Fallback locator: ``cache/last_conversations.json[<abs cwd>]``."""
    try:
        data = json.loads(LAST_CONVERSATIONS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    val = data.get(str(Path(cwd).resolve()))
    return val if isinstance(val, str) and val else None


def _path_key(p: str | Path) -> str | None:
    """Normalize a filesystem path to a comparable key (resolved when possible)."""
    if not p:
        return None
    try:
        pp = Path(p)
    except (TypeError, ValueError):
        return None
    try:
        return str(pp.resolve())
    except OSError:
        return str(pp)


def _workspace_uri_to_path_key(uri: Any) -> str | None:
    """Normalize a ``workspace_uris`` entry to a comparable path key.

    Entries are ``file://…`` URIs (e.g. ``file:///tmp/ws``) but a bare path is
    tolerated too. Returns None for anything unparseable."""
    if not isinstance(uri, str) or not uri:
        return None
    if uri.startswith("file:"):
        raw = unquote(urlparse(uri).path)
        return _path_key(raw) if raw else None
    return _path_key(uri)


def conversation_id_from_summaries(
    cwd: str | Path, db_path: str | Path | None = None
) -> str | None:
    """Fallback locator: the newest conversation in ``conversation_summaries.db``
    whose ``workspace_uris`` includes ``cwd``.

    agy maintains an indexed summaries DB with one row per conversation;
    ``workspace_uris`` is a JSON array of ``file://`` launch-workspace URIs. We
    return the ``conversation_id`` of the most-recent matching row (by
    ``last_modified_time``) — robust under concurrent sessions, where the racy
    ``newest_conversation_id()`` could return an unrelated workspace's run.

    Opened read-only; returns None on absence / lock / parse / schema errors
    (never raises)."""
    if not cwd:
        return None
    path = _SUMMARIES_DB if db_path is None else Path(db_path)
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    target = _path_key(cwd)
    if target is None:
        return None
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        cur = con.execute(
            "SELECT conversation_id, workspace_uris FROM conversation_summaries "
            "ORDER BY last_modified_time DESC"
        )
        for cid, workspace_uris in cur:
            if not isinstance(cid, str) or not cid:
                continue
            if not isinstance(workspace_uris, str):
                continue
            try:
                uris = json.loads(workspace_uris)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(uris, list):
                continue
            if any(_workspace_uri_to_path_key(u) == target for u in uris):
                return cid
    except sqlite3.Error:
        return None
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass
    return None


def newest_conversation_id() -> str | None:
    """Last-resort locator: the most recently modified ``brain/<uuid>/`` dir."""
    if not BRAIN_DIR.is_dir():
        return None
    dirs = [d for d in BRAIN_DIR.iterdir() if d.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime).name


def resolve_conversation_id(
    log_path: str | Path | None = None, cwd: str | Path | None = None
) -> str | None:
    """Best-effort conversation-id resolution, most→least reliable:

        log-file → last_conversations[cwd] → summaries.db[workspace=cwd] → newest.

    The workspace-scoped summaries.db lookup sits ahead of the racy
    newest-brain-dir last resort so concurrent sessions resolve to the right
    conversation for ``cwd``."""
    return (
        conversation_id_from_log(log_path)
        or (conversation_id_for_cwd(cwd) if cwd else None)
        or (conversation_id_from_summaries(cwd) if cwd else None)
        or newest_conversation_id()
    )


# ── Transcript reading ────────────────────────────────────────────────────────


def read_steps(conversation_id: str) -> list[dict]:
    """Parse ``transcript_full.jsonl`` for a conversation into a list of step dicts.

    Returns ``[]`` if the transcript is absent or unreadable (never raises).
    """
    path = transcript_path(conversation_id)
    if not path.is_file():
        return []
    steps: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    steps.append(obj)
    except OSError:
        return []
    return steps


def strip_user_request(content: str) -> str:
    """Return the raw prompt from a ``USER_INPUT`` step's ``content``.

    agy wraps the prompt as ``<USER_REQUEST>…</USER_REQUEST>`` followed by
    metadata and a ``<USER_SETTINGS_CHANGE>`` block; we return only the request
    body (or the whole content if the wrapper is absent)."""
    if not content:
        return ""
    m = _USER_REQUEST_RE.search(content)
    return (m.group(1) if m else content).strip()


def final_answer(conversation_id: str, steps: list[dict] | None = None) -> str | None:
    """The last ``PLANNER_RESPONSE`` with ``content`` and no ``tool_calls``.

    This equals what ``agy -p`` printed to stdout. Returns None if the model
    never emitted a tool-less response (e.g. it errored mid-run)."""
    if steps is None:
        steps = read_steps(conversation_id)
    for step in reversed(steps):
        if step.get("type") != "PLANNER_RESPONSE":
            continue
        if step.get("tool_calls"):
            continue
        content = step.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


# ── Timestamp helpers ─────────────────────────────────────────────────────────


def created_at_ms(step: dict) -> int | None:
    """``created_at`` (RFC3339 whole-second UTC) → epoch milliseconds, or None."""
    ts = step.get("created_at")
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def created_at_iso(step: dict) -> str | None:
    ms = created_at_ms(step)
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# ── Turn iteration (for message conversion + usage) ───────────────────────────


def iter_tool_names(step: dict) -> Iterator[str]:
    """Tool names from a ``PLANNER_RESPONSE``'s ``tool_calls``."""
    for call in step.get("tool_calls") or []:
        if isinstance(call, dict):
            name = call.get("name")
            if isinstance(name, str) and name:
                yield name


def iter_turns(steps: list[dict]) -> Iterator[dict[str, Any]]:
    """Yield a normalized turn per meaningful step, in order:

        {"role": "user", "text": str, "ts_ms": int|None}
        {"role": "assistant", "text": str, "tool_names": [str],
         "final": bool, "ts_ms": int|None}

    A ``user`` turn comes from a ``USER_INPUT`` step; an ``assistant`` turn from
    each ``PLANNER_RESPONSE`` that carries content and/or tool_calls. ``final`` is
    True for a content-only PLANNER_RESPONSE (the answer)."""
    for step in steps:
        stype = step.get("type")
        if stype == "USER_INPUT":
            text = strip_user_request(step.get("content") or "")
            if text:
                yield {"role": "user", "text": text, "ts_ms": created_at_ms(step)}
        elif stype == "PLANNER_RESPONSE":
            content = (step.get("content") or "").strip() if isinstance(step.get("content"), str) else ""
            tool_names = list(iter_tool_names(step))
            if not content and not tool_names:
                continue
            yield {
                "role": "assistant",
                "text": content,
                "tool_names": tool_names,
                "final": bool(content) and not tool_names,
                "ts_ms": created_at_ms(step),
            }


__all__ = [
    "conversation_id_from_log", "conversation_id_for_cwd",
    "conversation_id_from_summaries", "newest_conversation_id",
    "resolve_conversation_id", "read_steps", "strip_user_request", "final_answer",
    "created_at_ms", "created_at_iso", "iter_tool_names", "iter_turns",
]
