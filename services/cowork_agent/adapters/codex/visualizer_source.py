"""Codex (OpenAI Codex CLI) visualizer source — tails the date-nested
rollout files ``~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<uuid>.jsonl``
and emits normalised activity events.

Loaded by ``services.cowork_agent.visualizer.source_loader.load_source_module``
when ``AGENT_NAME=codex``. The class name ``Source`` is the loader contract;
``name`` must equal the adapter directory name.

Unlike claude_code (one Anthropic jsonl shape) codex stores every line as
``{"timestamp": ISO8601, "type": <TOP>, "payload": {...}}``; this module carries
its OWN in-class normaliser (mirror of openclaw's) rather than reusing the
Claude-shaped ``pii_filter``. Mapping (PII boundary enforced — names/paths/counts
only, never prompts / message text / reasoning / tool inputs):

    session_meta (first sight)              → SessionFirstSeen(cwd=session_meta.cwd)
    event_msg/user_message                  → MessageObserved(role="user")
    event_msg/agent_message                 → MessageObserved(role="assistant", model=<turn_context.model>)
    event_msg/token_count.info.last_token_usage → UsageObserved(...)  (per-turn delta; one per event)
    response_item/custom_tool_call|function_call → ToolUseObserved(tool=name)  (name only)
    event_msg/patch_apply_end.changes[path] → FileTouched(rel, created=type=="add")  (abs→project-rel)

Not emitted (deliberate): response_item/message (deduped against event_msg/*),
response_item/reasoning (encrypted), *_output (tool outputs), world_state.

Presence: codex has NO per-process presence file (no ~/.codex/sessions/<pid>.json
analog to claude's ~/.claude/sessions/<pid>.json), so ``poll_presence`` returns []
— a valid "no live sessions" snapshot (same as openclaw / antigravity).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
from pathlib import Path
from typing import Iterator, Optional

from services.cowork_agent.adapters.codex import paths as _paths
from services.cowork_agent.visualizer.discovery import iter_sessionslist_rows
from services.cowork_agent.visualizer.ingest import jsonl_tail
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    FileTouched,
    MessageObserved,
    SessionFirstSeen,
    ToolUseObserved,
    UsageObserved,
    compute_latency_ms,
)
from services.cowork_agent.visualizer.project_index import project_id_for_cwd
from services.cowork_agent.project_layout import xo_projects_root

logger = logging.getLogger(__name__)

_BACKEND = "codex"

# rollout-<ISO8601-with-dashes>-<uuid>.jsonl  → trailing UUIDv7 (the ISO prefix
# also contains dashes, so anchor on the canonical 8-4-4-4-12 shape at the end).
_ROLLOUT_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)


def _uuid_from_rollout(path: Path) -> Optional[str]:
    m = _ROLLOUT_UUID_RE.search(path.name)
    return m.group(1) if m else None


class Source:
    """Visualizer source for the codex backend.

    The class name ``Source`` is the loader contract — see
    ``services/cowork_agent/visualizer/source_loader.py``.
    """

    name = _BACKEND

    def __init__(self, offsets: Optional[jsonl_tail.OffsetStore] = None) -> None:
        self.offsets = offsets or jsonl_tail.OffsetStore()
        # Emit exactly one SessionFirstSeen per native session id.
        self._sessions_seen: set[str] = set()
        # native_session_id → latest turn_context.model (codex sets model
        # per turn; assistant MessageObserved/UsageObserved attach it).
        self._model_by_native: dict[str, str] = {}
        # native_session_id → ts of the last event_msg/user_message. Attach
        # latency_ms to the next UsageObserved for that session; pop on use so
        # a user message contributes at most one latency sample (mirror
        # claude_code visualizer_source.py:317-324).
        self._last_user_ts: dict[str, str] = {}
        # Absolute rollout path → (native_session_id, cwd) parsed from its
        # session_meta line, or None if unreadable. Cached so the auto-
        # discovery walk reads line 1 at most once per file per process.
        self._meta_cache: dict[str, Optional[tuple[str, str]]] = {}

    # ── Public protocol ─────────────────────────────────────────────────

    def poll_events(self) -> Iterator[Event]:
        """One tick. Yields normalised events from every codex rollout that
        maps to a workspace project. Flushes the offset store at the end
        (the watcher does NOT flush for us — framework §5.2)."""
        for project_id, native, cwd, path in self._discover():
            yield from self._tail_one(project_id, native, cwd, path)
        try:
            self.offsets.flush()
        except Exception as exc:
            logger.warning("codex source: offset flush failed: %s", exc)

    def poll_presence(self) -> list[dict]:
        # Codex has no per-process presence file (no ~/.codex/sessions/<pid>.json
        # analog to claude's ~/.claude/sessions/<pid>.json). Empty list is a
        # valid "no sessions open" snapshot — same as openclaw / antigravity.
        #
        # Design fork (do NOT ship for parity): presence could be synthesized
        # from rollout mtime (a rollout touched within the last N seconds ⇒ a
        # "busy" row), but that needs the epoch-ms keys the activity sink reads
        # (session_id / runtime / started_at_ms / updated_at_ms / project_id),
        # the watcher's model_by_session cache populated for that session, and a
        # debounce to avoid flapping. Revisit only if a live "who's coding now"
        # tile is required.
        return []

    # ── Discovery (two paths, mirror claude_code:172-239) ────────────────

    def _discover(self) -> Iterator[tuple[str, str, str, Path]]:
        """Yield ``(project_id, native_session_id, cwd, rollout_path)``.

        Path 1 — adapter rows: every ``backend=="codex"`` row in a project's
        sessionslist names a ``nativeSessionId`` (the thread UUID, patched in
        by adapter.py on ``thread.started``); resolve its rollout by glob-by-
        uuid. Path 2 — auto-discovery: any other rollout whose session_meta
        ``cwd`` resolves under the workspace (catches ``cd ~/xo-projects/foo
        && codex`` runs with no sessionslist row).
        """
        yielded: set[Path] = set()

        # 1. Adapter-row path (Plane-B chat sessions).
        for project_id, _composite_key, row in iter_sessionslist_rows(self.name):
            native = row.get("nativeSessionId")
            if not isinstance(native, str) or not native:
                continue  # empty-native preliminary row; UUID not learned yet
            path = _paths.find_rollout(native)
            if path is None or not path.is_file() or path in yielded:
                continue
            yielded.add(path)
            directory = row.get("directory")
            cwd = directory if isinstance(directory, str) else ""
            yield project_id, native, cwd, path

        # 2. Auto-discovery for direct ``codex`` runs inside an xo-project.
        for path in _paths.iter_rollouts():
            if path in yielded:
                continue
            meta = self._session_meta_for(path)
            if meta is None:
                continue
            native, cwd = meta
            if not native:
                continue
            project_id = project_id_for_cwd(cwd)
            if not project_id:
                continue  # outside the workspace — ignore this rollout
            yielded.add(path)
            yield project_id, native, cwd, path

    def _session_meta_for(self, path: Path) -> Optional[tuple[str, str]]:
        """Return ``(native_session_id, cwd)`` from a rollout's session_meta
        (line 1), cached per path. ``None`` if unreadable / not a session_meta.
        Falls back to the filename UUID when the payload lacks session_id."""
        key = str(path)
        if key in self._meta_cache:
            return self._meta_cache[key]
        result: Optional[tuple[str, str]] = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fp:
                first = fp.readline()
        except OSError:
            self._meta_cache[key] = None
            return None
        try:
            raw = json.loads(first)
        except (json.JSONDecodeError, ValueError):
            raw = None
        if isinstance(raw, dict) and raw.get("type") == "session_meta":
            payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
            sid = payload.get("session_id")
            cwd = payload.get("cwd")
            native = sid if isinstance(sid, str) and sid else (_uuid_from_rollout(path) or "")
            result = (native, cwd if isinstance(cwd, str) else "")
        self._meta_cache[key] = result
        return result

    # ── Per-rollout pipeline ─────────────────────────────────────────────

    def _tail_one(
        self, project_id: str, native: str, cwd: str, path: Path
    ) -> Iterator[Event]:
        for line in jsonl_tail.read_new_lines(path, self.offsets):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("codex source: dropped malformed line in %s", path)
                continue
            if not isinstance(raw, dict):
                continue
            yield from self._normalize_rollout_line(
                raw, project_id=project_id, native=native, cwd=cwd
            )

    # ── In-class normaliser (own PII boundary — mirror openclaw:180-244) ──

    def _normalize_rollout_line(
        self, raw: dict, *, project_id: str, native: str, cwd: str
    ) -> Iterator[Event]:
        top = raw.get("type")
        ts = raw.get("timestamp")
        if not isinstance(ts, str) or not ts:
            return
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        # SessionFirstSeen — once per native session, anchored to the first
        # line we see for it (session_meta on a fresh tail; any line on a
        # mid-file restart, where cwd came from discovery).
        if native and native not in self._sessions_seen:
            self._sessions_seen.add(native)
            first_cwd = cwd
            if top == "session_meta":
                c = payload.get("cwd")
                if isinstance(c, str) and c:
                    first_cwd = c
            yield SessionFirstSeen(
                ts=ts, native_session_id=native, runtime=self.name,
                project_id=project_id, cwd=first_cwd or "",
            )

        if top == "turn_context":
            m = payload.get("model")
            if isinstance(m, str) and m and native:
                self._model_by_native[native] = m
            return

        if top == "event_msg":
            yield from self._from_event_msg(
                payload, ts=ts, native=native, project_id=project_id
            )
            return

        if top == "response_item":
            yield from self._from_response_item(
                payload, ts=ts, native=native, project_id=project_id
            )
            return

        # session_meta / world_state / anything else: no further event.
        return

    def _from_event_msg(
        self, payload: dict, *, ts: str, native: str, project_id: str
    ) -> Iterator[Event]:
        etype = payload.get("type")

        if etype == "user_message":
            # Counter only — NEVER surface payload.message (the raw prompt).
            self._last_user_ts[native] = ts
            yield MessageObserved(
                ts=ts, native_session_id=native, runtime=self.name,
                project_id=project_id, role="user",
            )
            return

        if etype == "agent_message":
            # Counter only — NEVER surface payload.message (visible text).
            yield MessageObserved(
                ts=ts, native_session_id=native, runtime=self.name,
                project_id=project_id, role="assistant",
                model=self._model_by_native.get(native),
            )
            return

        if etype == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                return
            last = info.get("last_token_usage")
            if not isinstance(last, dict):
                return
            ev = self._usage_from_last(
                last, ts=ts, native=native, project_id=project_id
            )
            user_ts = self._last_user_ts.pop(native, None)
            if user_ts is not None:
                latency = compute_latency_ms(user_ts, ts)
                if latency is not None:
                    ev = dataclasses.replace(ev, latency_ms=latency)
            yield ev
            return

        if etype == "patch_apply_end":
            if payload.get("success") is False:
                return  # patch failed — no file was touched
            changes = payload.get("changes")
            if not isinstance(changes, dict):
                return
            for abs_path, meta in changes.items():
                if not isinstance(abs_path, str) or not abs_path:
                    continue
                created = isinstance(meta, dict) and meta.get("type") == "add"
                ft = self._file_touch(
                    abs_path, ts=ts, native=native,
                    project_id=project_id, created=created,
                )
                if ft is not None:
                    yield ft
            return

        # task_started / task_complete / etc.: nothing to emit.
        # TODO(codex): MCP tool calls in the rollout are UNVERIFIED — none ran in
        # the 7 captured sessions; they likely surface as response_item
        # function_call (already covered). Add their payload.type here only if a
        # future build emits them under event_msg instead.
        return

    def _from_response_item(
        self, payload: dict, *, ts: str, native: str, project_id: str
    ) -> Iterator[Event]:
        rtype = payload.get("type")
        # message   → NOT emitted (deduped against event_msg/user|agent_message;
        #             also carries role=developer / injected frames). See B6.4.
        # reasoning → dropped (encrypted_content — never surface).
        # *_output  → dropped (custom_tool_call_output / function_call_output = PII).
        # TODO(codex): response_item/local_shell_call and other item types are
        # UNVERIFIED (not observed). If a future build emits shell/web-search
        # items as response_item, add their payload.type to this tool-name branch.
        if rtype in ("custom_tool_call", "function_call"):
            name = payload.get("name")
            if isinstance(name, str) and name:
                yield ToolUseObserved(
                    ts=ts, native_session_id=native, runtime=self.name,
                    project_id=project_id, tool=name,
                )
        return

    # ── Helpers ──────────────────────────────────────────────────────────

    def _usage_from_last(
        self, last: dict, *, ts: str, native: str, project_id: str
    ) -> UsageObserved:
        """Map codex last_token_usage → the disjoint UsageObserved fields.

        Codex: cached_input_tokens ⊆ input_tokens, reasoning ⊆ output_tokens.
        UsageObserved fields are summed by the stats sink, so subtract the
        cached tokens out of ``input`` to keep them disjoint (§1.4):
        input + output + cache_read + cache_creation == total_tokens.
        """
        inp = int(last.get("input_tokens", 0) or 0)
        cr = int(last.get("cached_input_tokens", 0) or 0)
        cw = int(last.get("cache_write_input_tokens", 0) or 0)
        out = int(last.get("output_tokens", 0) or 0)
        return UsageObserved(
            ts=ts, native_session_id=native, runtime=self.name,
            project_id=project_id,
            input_tokens=max(inp - cr, 0),
            output_tokens=out,                       # reasoning already inside
            cache_read_input_tokens=cr,
            cache_creation_input_tokens=cw,
            model=self._model_by_native.get(native),
        )

    def _file_touch(
        self, abs_path: str, *, ts: str, native: str,
        project_id: str, created: bool,
    ) -> Optional[FileTouched]:
        """Re-anchor an absolute changed-file path to project-relative.
        Drops the event if it escapes the project root (mirror antigravity
        visualizer_source.py:150-171)."""
        project_root = (xo_projects_root() / project_id).resolve()
        try:
            rel = Path(abs_path).resolve().relative_to(project_root)
        except (ValueError, OSError):
            return None  # outside project root — drop (never leak the path)
        return FileTouched(
            ts=ts, native_session_id=native, runtime=self.name,
            project_id=project_id,
            relative_path=str(rel).replace("\\", "/"),
            created=created,
        )
