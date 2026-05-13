"""OpenClaw source — tails ``~/.openclaw/agents/<agent>/sessions/<sid>.jsonl``.

Responsibilities (mirror of :class:`ClaudeCodeSource`, minus the parts
that don't apply):

* Walk every workspace project, enumerate its OpenClaw jsonls (from
  the adapter-written ``sessionslist.json`` rows whose
  ``backend == "openclaw"``), tail each via :mod:`ingest.jsonl_tail`.
* Normalise through :func:`pii_filter.normalize_event_openclaw`.
* Cache the per-file session id from the ``type:"session"`` header
  row — OpenClaw doesn't put ``sessionId`` on every message.
* Emit a single :class:`events.SessionFirstSeen` per session id.

Not implemented (intentional, see
``docs/openclaw-watcher-implementation-plan.md`` §4):

* ``poll_presence`` returns ``[]`` — OpenClaw has no per-session pid
  file the way Claude does.
* ``FileTouched`` — OpenClaw's edit inputs aren't carried in the
  jsonl in the shape the PII filter wants. Defer.
* Task pairing — OpenClaw has no ``TaskCreate`` tool. Todos for
  OpenClaw flow through the ``POST /api/xo-projects/{id}/todos``
  HTTP endpoint, unaffected by the watcher.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Iterator, Optional

from services.cowork_agent.visualizer.ingest import jsonl_tail, pii_filter
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    SessionFirstSeen,
)
from services.cowork_agent.visualizer.workspace_index import list_project_ids
from services.cowork_agent.project_layout import xo_dir

logger = logging.getLogger(__name__)


_OPENCLAW_AGENTS_DIR = Path.home() / ".openclaw" / "agents"


class OpenClawSource:
    """Concrete :class:`Source` implementation for OpenClaw."""

    name = "openclaw"

    def __init__(self, offsets: Optional[jsonl_tail.OffsetStore] = None) -> None:
        # Share the OffsetStore with ClaudeCodeSource so a single
        # ``offsets.json`` covers both runtimes. The watcher
        # constructs one OffsetStore and hands it to both sources.
        self.offsets = offsets or jsonl_tail.OffsetStore()
        self._sessions_seen: set[str] = set()
        # Per-file session id (header row holds it once; lines that
        # follow don't carry it). Keyed by absolute jsonl path so a
        # second pass on a different file doesn't clobber the cache.
        self._sid_by_path: dict[str, str] = {}

    # ── Public protocol ─────────────────────────────────────────────────

    def poll_events(self) -> Iterator[Event]:
        for project_id, jsonl_path in self._discover_jsonls():
            yield from self._tail_one(project_id, jsonl_path)
        try:
            self.offsets.flush()
        except Exception as exc:
            logger.warning("OpenClaw source: offset flush failed: %s", exc)

    def poll_presence(self) -> list[dict]:
        # MVP: no per-session pid file. Honest signal that we don't
        # know who's active under OpenClaw. Revisit if/when we wire
        # the gateway's open-sessions list in.
        return []

    # ── Discovery ───────────────────────────────────────────────────────

    def _discover_jsonls(self) -> Iterator[tuple[str, Path]]:
        """Yield ``(project_id, jsonl_path)`` for every OpenClaw jsonl
        the adapter has recorded in any project's ``sessionslist.json``.

        Composite key shape: ``openclaw:<agent>:<surface>:<8hex>`` —
        see ``adapters/openclaw/transcript.py:128`` for the same parse.
        """
        if not _OPENCLAW_AGENTS_DIR.is_dir():
            return
        for project_id in list_project_ids():
            sl_path = xo_dir(project_id) / "sessions" / "sessionslist.json"
            if not sl_path.is_file():
                continue
            try:
                sl = json.loads(sl_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(sl, dict):
                continue
            for composite, row in sl.items():
                if not isinstance(row, dict):
                    continue
                if row.get("backend") != "openclaw":
                    continue
                native = row.get("nativeSessionId")
                if not isinstance(native, str) or not native:
                    continue
                # Fall back to "main" if the parse fails — matches the
                # default in adapters/openclaw/transcript.py:128.
                agent = "main"
                if isinstance(composite, str) and ":" in composite:
                    parts = composite.split(":")
                    if len(parts) >= 2 and parts[1]:
                        agent = parts[1]
                jsonl = _OPENCLAW_AGENTS_DIR / agent / "sessions" / f"{native}.jsonl"
                if jsonl.is_file():
                    yield project_id, jsonl

    # ── Per-jsonl pipeline ──────────────────────────────────────────────

    def _tail_one(self, project_id: str, jsonl_path: Path) -> Iterator[Event]:
        for line in jsonl_tail.read_new_lines(jsonl_path, self.offsets):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("OpenClaw source: dropped malformed line in %s", jsonl_path)
                continue
            if not isinstance(raw, dict):
                continue

            # Header row — record the session id and move on. The
            # next line's events carry the ts we want
            # SessionFirstSeen anchored to.
            if raw.get("type") == "session":
                sid = raw.get("id")
                if isinstance(sid, str) and sid:
                    self._sid_by_path[str(jsonl_path)] = sid
                continue

            sid = self._sid_by_path.get(str(jsonl_path))
            if not sid:
                # Tail started mid-file (server restart, offset>0) and
                # we never saw the header. Fall back to the filename
                # stem — OpenClaw's jsonl name == sessionId by
                # convention (see transcript.py).
                sid = jsonl_path.stem
                self._sid_by_path[str(jsonl_path)] = sid

            for ev in pii_filter.normalize_event_openclaw(raw, current_sid=sid):
                ev = dataclasses.replace(ev, project_id=project_id)
                if ev.native_session_id and ev.native_session_id not in self._sessions_seen:
                    self._sessions_seen.add(ev.native_session_id)
                    yield SessionFirstSeen(
                        ts=ev.ts,
                        native_session_id=ev.native_session_id,
                        runtime=self.name,
                        project_id=project_id,
                        cwd="",  # OpenClaw doesn't surface a cwd per line
                    )
                yield ev
