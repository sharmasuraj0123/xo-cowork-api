"""Antigravity (agy) visualizer source — tails the agy transcripts of
xo-project sessions and emits normalised activity events.

Loaded by ``services.cowork_agent.visualizer.source_loader.load_source_module``
when ``AGENT_NAME=antigravity``. The class name ``Source`` is the loader
contract; ``name`` must equal the adapter directory name.

Discovery has two paths (mirroring codex, whose native store is likewise keyed by
session id rather than by cwd):

1. **Adapter rows** — every ``antigravity`` row in a project's
   ``sessionslist.json`` names a ``nativeSessionId`` (agy conversation uuid) →
   its transcript ``brain/<uuid>/.system_generated/logs/transcript_full.jsonl``.
2. **Auto-discovery** — any other conversation in agy's ``brain/`` store whose
   launch cwd resolves under the workspace. Keeps a project visible when its row
   is missing or has not learned a ``nativeSessionId`` yet, and catches direct
   ``cd ~/xo-projects/foo && agy`` runs.

Each new step maps to an :class:`Event`:

    USER_INPUT                          → MessageObserved(role="user")
    PLANNER_RESPONSE (content and/or tools) → MessageObserved(role="assistant")
    PLANNER_RESPONSE.tool_calls[]       → ToolUseObserved(tool=<name>)  (+ FileTouched for writes)

A single :class:`SessionFirstSeen` is emitted per conversation.

:class:`UsageObserved` does not come from the transcript at all — agy keeps token
counts in the per-conversation SQLite store (``tokens.py``), so after a tick that
moved the transcript we read the new ``gen_metadata`` rows and emit one event per
model call. Three properties of those numbers, all deliberate:

* they are **client-side tokenizer ESTIMATES**, not the provider's billed usage
  (same caveat ``usage.py`` carries, surfaced there as ``estimated_tokens``);
* agy has **no cache tier**, so ``cache_read``/``cache_creation`` are always 0;
* a call's ``input`` is the whole re-sent context, so Σinput is a billed-input
  **upper bound**, not a per-turn delta like codex's ``last_token_usage``. That
  is on purpose: it is the same number ``/api/usage`` reports for the same
  conversation, so the two planes agree instead of disagreeing by construction.

Presence: agy DOES write ``<agy-home>/presence/<conversation-uuid>.lock``, but it
is a 0-byte flock sentinel — none of the fields ``sinks/activity.py:78-104``
requires (no pid, cwd, startedAt/updatedAt), held only for the few seconds of a
single prompt and never removed on exit. So ``poll_presence`` returns an empty
snapshot — a valid "no live sessions" answer, and a truthful one.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator, Optional

from services.cowork_agent.adapters.antigravity import transcript as _t
from services.cowork_agent.adapters.antigravity.paths import (
    BRAIN_DIR,
    LAST_CONVERSATIONS,
    conversation_db,
    transcript_path,
)
from services.cowork_agent.adapters.antigravity.tokens import conversation_tokens
from services.cowork_agent.visualizer.discovery import iter_sessionslist_rows
from services.cowork_agent.visualizer.ingest import jsonl_tail
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    FileTouched,
    MessageObserved,
    SessionFirstSeen,
    ToolUseObserved,
    UsageObserved,
)
from services.cowork_agent.visualizer.project_index import project_id_for_cwd
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
        # conversation uuid → the launch cwd agy recorded for it. Sticky on
        # purpose: ``cache/last_conversations.json`` keeps only the NEWEST
        # conversation per directory, so a conversation we have already mapped
        # would otherwise drop out of discovery — and stop being tailed — the
        # moment a newer run starts in the same project.
        self._cwd_by_conversation: dict[str, str] = {}
        # cwd → project id (None = outside the workspace). The brain scan asks
        # the same question about the same handful of directories on every 1 s
        # tick and ``project_id_for_cwd`` resolves the path each call, so memoise
        # both hits and misses; the answer is purely path-derived and stable for
        # the life of the process.
        self._project_by_cwd: dict[str, Optional[str]] = {}
        # conversation uuid → the model of its newest gen_metadata row. agy names
        # the model per CALL, not per session, so this is refreshed whenever the
        # token store is read and attached to assistant messages (mirror codex's
        # ``_model_by_native``, fed there by turn_context).
        self._model_by_conversation: dict[str, str] = {}

    # ── Public protocol ─────────────────────────────────────────────────

    def poll_events(self) -> Iterator[Event]:
        for project_id, native, cwd, path in self._discover():
            yield from self._tail_one(project_id, native, cwd, path)
        try:
            self.offsets.flush()
        except Exception as exc:
            logger.warning("antigravity source: offset flush failed: %s", exc)

    def poll_presence(self) -> list[dict]:
        # agy writes ``<agy-home>/presence/<conversation-uuid>.lock``, but it is a
        # 0-byte flock sentinel: no pid, no cwd, no started/updated timestamps —
        # none of the fields the activity sink requires — held only for the few
        # seconds of one prompt and left behind on exit. Empty list is the honest
        # "no sessions open" snapshot; synthesizing rows out of mtime would
        # report finished prompts as open sessions.
        return []

    # ── Discovery ───────────────────────────────────────────────────────

    def _discover(self) -> Iterator[tuple[str, str, str, Path]]:
        """Yield ``(project_id, native_conversation_id, cwd, transcript_path)``.

        Path 1 — adapter rows: every ``backend=="antigravity"`` row in a
        project's sessionslist names a ``nativeSessionId`` (the conversation
        uuid, patched in by adapter.py once the run resolves it) → its
        transcript, with the row's ``directory`` as the cwd. Path 2 —
        auto-discovery: any other conversation in agy's ``brain/`` store whose
        launch cwd resolves under the workspace. Path 2 is what keeps a project
        alive when the row is missing or never learned a ``nativeSessionId``
        (mirror ``codex/visualizer_source.py:125-164``).
        """
        yielded: set[Path] = set()

        # 1. Adapter-row path (Plane-B chat sessions).
        for project_id, _composite_key, row in iter_sessionslist_rows(self.name):
            native = row.get("nativeSessionId")
            if not isinstance(native, str) or not native:
                continue  # preliminary row; the conversation uuid isn't known yet
            path = transcript_path(native)
            if path.is_file() and path not in yielded:
                yielded.add(path)
                directory = row.get("directory")
                cwd = directory if isinstance(directory, str) else ""
                yield project_id, native, cwd, path

        # 2. Auto-discovery for conversations with no usable row. Built lazily:
        # the index is read at most once per tick, and only when the brain store
        # holds a transcript path 1 didn't already claim.
        cwd_index: Optional[dict[str, str]] = None
        for native, path in self._iter_brain_transcripts():
            if path in yielded:
                continue
            cwd = self._cwd_by_conversation.get(native)
            if cwd is None:
                if cwd_index is None:
                    cwd_index = self._load_cwd_index()
                cwd = cwd_index.get(native)
                if not cwd:
                    continue  # agy never recorded a launch cwd — can't attribute it
                self._cwd_by_conversation[native] = cwd
            project_id = self._project_for_cwd(cwd)
            if not project_id:
                continue  # outside the workspace — ignore this conversation
            yielded.add(path)
            yield project_id, native, cwd, path

    def _iter_brain_transcripts(self) -> Iterator[tuple[str, Path]]:
        """Yield ``(conversation_id, transcript_path)`` for every conversation in
        agy's ``brain/`` store that has a transcript on disk, ordered by uuid so
        a tick's events land in a deterministic order. Empty when the store is
        absent / unreadable (never raises)."""
        try:
            entries = sorted(BRAIN_DIR.iterdir())
        except OSError:
            return
        for entry in entries:
            # No is_dir() probe — the transcript stat below already rules out
            # stray files, and it is the only syscall we want per conversation.
            path = transcript_path(entry.name)
            if path.is_file():
                yield entry.name, path

    def _load_cwd_index(self) -> dict[str, str]:
        """Invert ``cache/last_conversations.json``
        (``{<abs-launch-cwd>: <conversation-uuid>}``) into
        ``{<conversation-uuid>: <abs-launch-cwd>}``.

        An agy transcript carries no cwd of its own (unlike a codex rollout's
        ``session_meta`` line), so the mapping back from an orphaned conversation
        to a project has to come from one of agy's side indexes. This one is a
        single small JSON, cheap enough to re-read on a 1 s tick (agy rewrites it
        on every run); the other — ``conversation_summaries.db``, queried by
        ``transcript.conversation_id_from_summaries`` — is a SQLite table scan and
        is deliberately NOT used here. Its extra coverage is conversations that
        are no longer the newest in their directory, which we keep anyway via
        ``_cwd_by_conversation``.

        Missing / malformed ⇒ empty index, never raises.
        """
        try:
            data = json.loads(LAST_CONVERSATIONS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            cid: cwd
            for cwd, cid in data.items()
            if isinstance(cwd, str) and cwd and isinstance(cid, str) and cid
        }

    def _project_for_cwd(self, cwd: str) -> Optional[str]:
        """Memoised :func:`project_id_for_cwd` (see the cache note in ``__init__``)."""
        if cwd not in self._project_by_cwd:
            self._project_by_cwd[cwd] = project_id_for_cwd(cwd)
        return self._project_by_cwd[cwd]

    # ── Per-transcript pipeline ─────────────────────────────────────────

    def _tail_one(
        self, project_id: str, native: str, cwd: str, path: Path
    ) -> Iterator[Event]:
        lines = list(jsonl_tail.read_new_lines(path, self.offsets))
        if not lines:
            return  # idle conversation — never touch agy's live token store
        # Read the token rows BEFORE converting the steps, so this tick's
        # assistant messages already carry the model; the usage events
        # themselves are emitted after the steps (they need the last step's ts).
        pending = self._pending_calls(native)
        last_ts = ""
        for line in lines:
            try:
                step = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(step, dict):
                continue
            for event in self._convert(step, project_id, native, cwd):
                last_ts = event.ts
                yield event
        # No convertible step this tick ⇒ no timestamp to stamp usage with. The
        # cursor is untouched, so the rows are emitted on the next tick instead.
        if pending and last_ts:
            yield from self._usage_events(pending, project_id, native, ts=last_ts)

    def _pending_calls(self, native: str) -> list[tuple]:
        """``gen_metadata`` rows for this conversation we have not emitted yet.

        Tokens are not in the transcript — they live in agy's per-conversation
        SQLite store (``tokens.py``), so the live feed has to read it. Two rules
        keep that safe: only on a tick where the transcript actually moved (the
        caller's gate), and ``allow_checkpoint=False``, because the checkpointing
        fallback WRITES (``PRAGMA wal_checkpoint(TRUNCATE)``) into a store agy
        may still be using.

        Also refreshes the per-conversation model (agy names it per call), which
        the assistant :class:`MessageObserved` carries.
        """
        db = conversation_db(native)
        if not db.is_file():
            return []
        calls = conversation_tokens(native, allow_checkpoint=False).get("calls") or []
        if not calls:
            return []
        model = next((c[1] for c in reversed(calls) if c[1]), None)
        if model:
            self._model_by_conversation[native] = model
        try:
            inode = db.stat().st_ino
        except OSError:
            return []
        saved = self.offsets.get(db)
        # Inode mismatch ⇒ the store was replaced under the same uuid, so its
        # idx sequence restarted; re-read from 0 (jsonl_tail's rotation rule).
        cursor = saved[0] if saved and saved[1] == inode else 0
        return [c for c in calls if isinstance(c[0], int) and c[0] >= cursor]

    def _usage_events(
        self, rows: list[tuple], project_id: str, native: str, *, ts: str
    ) -> Iterator[Event]:
        """One :class:`UsageObserved` per new ``gen_metadata`` row, stamped with
        ``ts`` (the last converted step of this tick — agy's token rows carry no
        timestamp of their own).

        The cursor (next unseen ``idx``) is persisted in the shared offset store
        under the conversation's ``.db`` path — a key ``jsonl_tail`` never uses,
        since the transcript it tails is a different file. Persisting is not
        optional: ``stats.json`` is CUMULATIVE on disk, so an in-memory-only
        high-water mark would re-emit every call after a restart and double the
        totals.
        """
        for idx, model, in_tokens, out_tokens in rows:
            yield UsageObserved(
                ts=ts, native_session_id=native, runtime=self.name,
                project_id=project_id,
                input_tokens=int(in_tokens or 0),
                output_tokens=int(out_tokens or 0),
                # agy reports no cached-token tier at all (tokens.py:12-18).
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                model=model or None,
            )
        db = conversation_db(native)
        try:
            inode = db.stat().st_ino
        except OSError:
            inode = 0
        self.offsets.set(db, offset=max(r[0] for r in rows) + 1, inode=inode)

    def _convert(
        self, step: dict, project_id: str, native: str, cwd: str
    ) -> Iterator[Event]:
        ts = _t.created_at_iso(step) or ""
        if not ts:
            return

        if native not in self._sessions_seen:
            self._sessions_seen.add(native)
            yield SessionFirstSeen(
                ts=ts, native_session_id=native, runtime=self.name,
                project_id=project_id, cwd=cwd or "",
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
            # A turn that both speaks and calls a tool is still an assistant
            # message — same rule this adapter's own ``transcript.iter_turns``
            # applies ("content and/or tool_calls"), and the one codex and
            # claude_code apply unconditionally. Counter only: no text is
            # surfaced, here or anywhere else in this module.
            if (isinstance(content, str) and content.strip()) or tool_calls:
                yield MessageObserved(
                    ts=ts, native_session_id=native, runtime=self.name,
                    project_id=project_id, role="assistant",
                    model=self._model_by_conversation.get(native),
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
