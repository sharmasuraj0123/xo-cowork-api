"""Antigravity (agy) visualizer source — tails the agy transcripts of
xo-project sessions and emits normalised activity events.

Loaded by ``services.cowork_agent.visualizer.source_loader.load_source_module``
when ``AGENT_NAME=antigravity``. The class name ``Source`` is the loader
contract; ``name`` must equal the adapter directory name.

Discovery mirrors claude_code's adapter-row path: every ``antigravity`` row in a
project's ``sessionslist.json`` names a ``nativeSessionId`` (agy conversation
uuid) → its transcript
``brain/<uuid>/.system_generated/logs/transcript_full.jsonl``. Each new step maps
to an :class:`Event`:

    USER_INPUT                          → MessageObserved(role="user")
    PLANNER_RESPONSE (content, no tools)→ MessageObserved(role="assistant")
    PLANNER_RESPONSE.tool_calls[]       → ToolUseObserved(tool=<name>)  (+ FileTouched for writes)

A single :class:`SessionFirstSeen` is emitted per conversation. **UsageObserved
is intentionally not emitted** — agy's tokens are client-side estimates stored in
the SQLite DB, not per-turn in the transcript, so token telemetry is owned by the
``usage`` capability, not the live feed (see docs/ANTIGRAVITY_ADAPTER.md §5).

Presence: agy runs as a short-lived subprocess per prompt (no persistent
``<pid>.json`` presence file like claude_code), so ``poll_presence`` returns an
empty snapshot — a valid "no live sessions" answer.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, Optional

from services.cowork_agent.adapters.antigravity import transcript as _t
from services.cowork_agent.adapters.antigravity.paths import transcript_path
from services.cowork_agent.visualizer.discovery import iter_sessionslist_rows
from services.cowork_agent.visualizer.ingest import jsonl_tail
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    FileTouched,
    MessageObserved,
    SessionFirstSeen,
    ToolUseObserved,
)
from services.cowork_agent.project_layout import xo_projects_root

logger = logging.getLogger(__name__)

_BACKEND = "antigravity"

# agy tools that mutate a file → surface as FileTouched.
_WRITE_TOOLS = {
    "write_to_file": False, "create_file": True,
    "replace_file_content": False, "multi_replace_file_content": False,
}
_TARGET_ARGS = ("TargetFile", "AbsolutePath", "target_file", "file_path")


class Source:
    """Visualizer source for the antigravity backend."""

    name = _BACKEND

    def __init__(self, offsets: Optional[jsonl_tail.OffsetStore] = None) -> None:
        self.offsets = offsets or jsonl_tail.OffsetStore()
        self._sessions_seen: set[str] = set()

    # ── Public protocol ─────────────────────────────────────────────────

    def poll_events(self) -> Iterator[Event]:
        for project_id, native, path in self._discover():
            yield from self._tail_one(project_id, native, path)
        try:
            self.offsets.flush()
        except Exception as exc:
            logger.warning("antigravity source: offset flush failed: %s", exc)

    def poll_presence(self) -> list[dict]:
        # agy has no persistent per-process presence file; runs are ephemeral
        # subprocesses. Empty list is a valid "no sessions open" snapshot.
        return []

    # ── Discovery ───────────────────────────────────────────────────────

    def _discover(self) -> Iterator[tuple[str, str, Path]]:
        """Yield ``(project_id, native_conversation_id, transcript_path)`` for
        every antigravity session recorded in any project's sessionslist."""
        yielded: set[Path] = set()
        for project_id, _composite_key, row in iter_sessionslist_rows(self.name):
            native = row.get("nativeSessionId")
            if not isinstance(native, str) or not native:
                continue
            path = transcript_path(native)
            if path.is_file() and path not in yielded:
                yielded.add(path)
                yield project_id, native, path

    # ── Per-transcript pipeline ─────────────────────────────────────────

    def _tail_one(self, project_id: str, native: str, path: Path) -> Iterator[Event]:
        for line in jsonl_tail.read_new_lines(path, self.offsets):
            import json
            try:
                step = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(step, dict):
                continue
            yield from self._convert(step, project_id, native)

    def _convert(self, step: dict, project_id: str, native: str) -> Iterator[Event]:
        ts = _t.created_at_iso(step) or ""
        if not ts:
            return

        if native not in self._sessions_seen:
            self._sessions_seen.add(native)
            yield SessionFirstSeen(
                ts=ts, native_session_id=native, runtime=self.name,
                project_id=project_id, cwd="",
            )

        stype = step.get("type")
        if stype == "USER_INPUT":
            yield MessageObserved(
                ts=ts, native_session_id=native, runtime=self.name,
                project_id=project_id, role="user",
            )
        elif stype == "PLANNER_RESPONSE":
            tool_calls = step.get("tool_calls") or []
            content = step.get("content")
            if isinstance(content, str) and content.strip() and not tool_calls:
                yield MessageObserved(
                    ts=ts, native_session_id=native, runtime=self.name,
                    project_id=project_id, role="assistant",
                )
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                name = call.get("name")
                if not isinstance(name, str) or not name:
                    continue
                yield ToolUseObserved(
                    ts=ts, native_session_id=native, runtime=self.name,
                    project_id=project_id, tool=name,
                )
                touched = self._file_touch(call, ts, native, project_id)
                if touched is not None:
                    yield touched

    def _file_touch(
        self, call: dict, ts: str, native: str, project_id: str
    ) -> Optional[FileTouched]:
        name = call.get("name", "")
        if name not in _WRITE_TOOLS:
            return None
        args = call.get("args") or {}
        if not isinstance(args, dict):
            return None
        target = next((args[k] for k in _TARGET_ARGS if isinstance(args.get(k), str)), None)
        if not target:
            return None
        project_root = (xo_projects_root() / project_id).resolve()
        try:
            rel = Path(target).resolve().relative_to(project_root)
        except (ValueError, OSError):
            return None
        return FileTouched(
            ts=ts, native_session_id=native, runtime=self.name,
            project_id=project_id, relative_path=str(rel).replace("\\", "/"),
            created=_WRITE_TOOLS[name],
        )
