"""
Antigravity (agy) sessions capability.

agy tees its sessions into xo-projects (``.xo/sessions/sessionslist.json``, tagged
``backend:"antigravity"``), so the generic project-tied scan applies —
``USES_PROJECT_SESSIONS = True``. The native message store is the per-conversation
transcript ``brain/<nativeSessionId>/.system_generated/logs/transcript_full.jsonl``
(keyed by conversation uuid, not by an encoded cwd path like claude_code).

The listing hooks (``enrich_project_session`` / ``resolve_native_file`` /
``list_native_sessions``) and read hooks (``owns_session`` / ``get_messages`` /
``set_session_directory``) are what ``engine/sessions_io`` calls instead of
branching on the backend name.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.adapters.antigravity import transcript as _t
from services.cowork_agent.adapters.antigravity.paths import transcript_path
from services.cowork_agent.engine.sessions_io import find_session_file, _resolve_index_path
from services.cowork_agent.helpers import strip_workspace_preamble
from services.cowork_agent.project_layout import xo_projects_root

USES_PROJECT_SESSIONS = True


# ── Listing hooks ─────────────────────────────────────────────────────────────


def resolve_native_file(meta: dict, session_id: str) -> Path | None:
    """Locate the agy transcript for a project-tied session (by conversation uuid)."""
    native = (meta or {}).get("nativeSessionId") or ""
    if not native:
        return None
    path = transcript_path(native)
    return path if path.exists() else None


def enrich_project_session(meta: dict, key: str, default_agent: str):
    """Return ``(time_created, title, effective_agent)`` by reading the transcript.

    ``time_created`` = first step's ``created_at``; ``title`` = first user prompt
    (truncated). Either override may be None (caller keeps its defaults)."""
    time_created = None
    title = None
    native = (meta or {}).get("nativeSessionId") or ""
    if native:
        steps = _t.read_steps(native)
        if steps:
            time_created = _t.created_at_iso(steps[0])
            for turn in _t.iter_turns(steps):
                if turn["role"] == "user" and turn["text"]:
                    title = turn["text"][:80]
                    break
    return time_created, title, default_agent


def list_native_sessions() -> list[dict]:
    """agy has no non-project native session store — all sessions are project-tee'd."""
    return []


# ── Read hooks ────────────────────────────────────────────────────────────────


def owns_session(session_id: str) -> bool:
    """agy sessions are detected via the sessionslist ``backend`` tag, not a scan."""
    return False


# Tool-call arg fields that name the thing being acted on (checked in order).
_TARGET_ARG_FIELDS = (
    "AbsolutePath", "TargetFile", "DirectoryPath", "SearchDirectory",
    "SearchPath", "CommandLine", "Url", "Query",
)
# Non-bulky arg fields worth surfacing as the tool "input" (skip file bodies etc.).
_INPUT_ARG_FIELDS = (
    "AbsolutePath", "TargetFile", "DirectoryPath", "SearchDirectory",
    "SearchPath", "CommandLine", "Cwd", "Query", "Description", "Instruction",
)
# agy step types that carry a tool RESULT, paired to a preceding tool_call by order.
_RESULT_TYPES = {
    "VIEW_FILE", "LIST_DIRECTORY", "RUN_COMMAND", "CODE_ACTION",
    "GREP_SEARCH", "CODEBASE_SEARCH", "GENERIC",
}
_MAX_TOOL_OUTPUT = 8000


def _clean_result(content: str) -> str:
    """Trim agy's result-step header (Created/Completed At) and cap size so a tool
    chip's output panel reads as the file/output body, not a metadata dump."""
    if not content:
        return ""
    lines = content.splitlines()
    while lines and (
        lines[0].startswith("Created At:")
        or lines[0].startswith("Completed At:")
        or not lines[0].strip()
    ):
        lines.pop(0)
    out = "\n".join(lines).strip()
    if len(out) > _MAX_TOOL_OUTPUT:
        out = out[:_MAX_TOOL_OUTPUT] + f"\n… (truncated, {len(out)} chars total)"
    return out


def _tool_part_data(call: dict, ts: str) -> dict:
    """Clean tool part from an agy tool_call: a human title (``toolAction``), the
    target path/command as ``input``, ``output`` filled later from its result
    step. This is what makes a ``view_file`` chip actually show the file."""
    name = call.get("name") or "tool"
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    title = args.get("toolAction") or args.get("toolSummary") or name
    target = next((str(args[k]) for k in _TARGET_ARG_FIELDS if args.get(k)), None)
    input_clean = {k: args[k] for k in _INPUT_ARG_FIELDS if args.get(k)}
    return {
        "type": "tool", "tool": name, "call_id": "",
        "state": {
            "status": "completed", "input": input_clean, "output": None,
            "metadata": {"target": target} if target else None,
            "title": title,
            "time_start": ts, "time_end": ts, "time_compacted": None,
        },
    }


def _convert(session_id: str, path: Path) -> list[dict]:
    """Convert an agy ``transcript_full.jsonl`` into xo-cowork MessageResponse
    dicts (same shape ``engine/messages.convert_messages`` produces).

    Walks raw steps so tool calls can be paired with their result step (agy has no
    linking id, so pairing is by order/FIFO): one agy prompt → one assistant
    message whose parts are collapsed reasoning + tool chips (title/path/output) +
    the final text answer."""
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

    def _iso(ts_ms: int | None) -> str:
        return (
            datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
            if ts_ms else datetime.now(timezone.utc).isoformat()
        )

    messages: list[dict] = []
    counter = [0]
    pending: list[dict] = []              # assistant part-data dicts for this turn
    pending_ts: list[str | None] = [None]
    awaiting: list[dict] = []             # FIFO of tool parts awaiting result output

    def _flush_assistant() -> None:
        if not pending:
            return
        counter[0] += 1
        mid = f"{session_id}_m{counter[0]}"
        ts = pending_ts[0] or datetime.now(timezone.utc).isoformat()
        parts = [
            {"id": f"{mid}_p{i}", "message_id": mid, "session_id": session_id,
             "time_created": ts, "data": data}
            for i, data in enumerate(pending)
        ]
        messages.append({
            "id": mid, "session_id": session_id, "time_created": ts,
            "data": {"role": "assistant", "model_id": None, "provider_id": None,
                     "cost": None, "tokens": None, "finish": "stop", "error": None},
            "parts": parts,
        })
        pending.clear()
        pending_ts[0] = None
        awaiting.clear()

    for step in steps:
        stype = step.get("type")
        ts = _iso(_t.created_at_ms(step))

        if stype == "USER_INPUT":
            _flush_assistant()
            # Strip the frontend's "> **Project context**" preamble so the user
            # bubble shows only what the user typed (parity with claude_code).
            text = strip_workspace_preamble(
                _t.strip_user_request(step.get("content") or "")
            ).strip()
            if not text:
                continue
            counter[0] += 1
            mid = f"{session_id}_m{counter[0]}"
            messages.append({
                "id": mid, "session_id": session_id, "time_created": ts,
                "data": {"role": "user"},
                "parts": [{
                    "id": f"{mid}_p0", "message_id": mid, "session_id": session_id,
                    "time_created": ts, "data": {"type": "text", "text": text},
                }],
            })

        elif stype == "PLANNER_RESPONSE":
            if pending_ts[0] is None:
                pending_ts[0] = ts
            content = step.get("content")
            content = content.strip() if isinstance(content, str) else ""
            tool_calls = step.get("tool_calls") or []
            if content:
                # Final answer (content, no tools) → visible text; reasoning that
                # precedes tool calls → collapsed.
                pending.append({
                    "type": "text" if not tool_calls else "reasoning",
                    "text": content,
                })
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                data = _tool_part_data(call, ts)
                pending.append(data)
                awaiting.append(data)          # same object; result fills its output

        elif stype in _RESULT_TYPES:
            # Pair to the oldest tool call still awaiting a result (FIFO — agy emits
            # one result per call, in order). Fills the chip's output → clicking
            # the tool shows the file/dir/command output.
            if awaiting:
                awaiting.pop(0)["state"]["output"] = _clean_result(step.get("content") or "")

    _flush_assistant()
    return messages


def get_messages(session_id: str) -> list:
    """Converted messages for an agy session (empty if no transcript)."""
    path = find_session_file(session_id)
    if not path:
        return []
    return _convert(session_id, path)


# ── Directory update ──────────────────────────────────────────────────────────


def _persist_session_directory(session_id: str, directory: str) -> bool:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    def _try_index(index_path: Path) -> bool:
        if not index_path.exists():
            return False
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception:
            return False
        for meta in index_data.values():
            if not isinstance(meta, dict) or meta.get("sessionId") != session_id:
                continue
            history = meta.get("directoryHistory") or []
            history.append({"directory": directory, "selectedAt": now_ms})
            meta["directoryHistory"] = history[-200:]
            meta["directory"] = directory
            meta["updatedAt"] = now_ms
            index_path.write_text(
                json.dumps(index_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return True
        return False

    projects_root = xo_projects_root()
    if projects_root.exists():
        for agent_dir in projects_root.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            idx_path = _resolve_index_path(agent_dir / ".xo" / "sessions")
            if idx_path and _try_index(idx_path):
                return True
    return False


def set_session_directory(session_id: str, directory: str) -> dict | None:
    if _persist_session_directory(session_id, directory):
        return {"ok": True, "session_id": session_id, "directory": directory}
    return None


__all__ = [
    "USES_PROJECT_SESSIONS", "resolve_native_file", "enrich_project_session",
    "list_native_sessions", "owns_session", "get_messages", "set_session_directory",
]
