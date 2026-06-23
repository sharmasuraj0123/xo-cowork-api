"""OpenClaw visualizer source — tails
``~/.openclaw/agents/<agent>/sessions/<sid>.jsonl``.

Loaded by :func:`services.cowork_agent.visualizer.source_loader.load_source_module`
when ``AGENT_NAME=openclaw``. The class name ``Source`` is the loader
contract.

Responsibilities (mirror of the claude_code source, minus the parts
that don't apply):

* Walk every workspace project, enumerate its OpenClaw jsonls (from
  the adapter-written ``sessionslist.json`` rows whose
  ``backend == "openclaw"``), tail each via :mod:`ingest.jsonl_tail`.
* Normalise through :func:`pii_filter.normalize_event_openclaw`.
* Cache the per-file session id from the ``type:"session"`` header
  row — OpenClaw doesn't put ``sessionId`` on every message.
* Emit a single :class:`events.SessionFirstSeen` per session id.

Not implemented (intentional):

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

from services.cowork_agent.visualizer.discovery import iter_sessionslist_rows
from services.cowork_agent.visualizer.ingest import jsonl_tail
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    MessageObserved,
    SessionFirstSeen,
    ToolUseObserved,
    UsageObserved,
    compute_latency_ms,
)

logger = logging.getLogger(__name__)


_OPENCLAW_AGENTS_DIR = Path.home() / ".openclaw" / "agents"


class Source:
    """Visualizer source for the OpenClaw backend.

    The class name ``Source`` is the loader contract — see
    ``services/cowork_agent/visualizer/source_loader.py``.
    """

    name = "openclaw"

    def __init__(self, offsets: Optional[jsonl_tail.OffsetStore] = None) -> None:
        self.offsets = offsets or jsonl_tail.OffsetStore()
        self._sessions_seen: set[str] = set()
        # Per-file session id (header row holds it once; lines that
        # follow don't carry it). Keyed by absolute jsonl path so a
        # second pass on a different file doesn't clobber the cache.
        self._sid_by_path: dict[str, str] = {}
        # native_session_id → ts of the last MessageObserved(role="user").
        # Used to attach latency_ms on the matching UsageObserved
        # Mirrors the claude_code source.
        self._last_user_ts: dict[str, str] = {}

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
        the adapter has recorded in any project's sessionslist.

        Row enumeration is shared (see
        :mod:`services.cowork_agent.visualizer.discovery`); the
        OpenClaw-specific part is the composite-key parse
        (``openclaw:<agent>:<surface>:<8hex>`` — see
        ``adapters/openclaw/transcript.py:128`` for the same parse)
        and the agent → jsonl path translation.
        """
        if not _OPENCLAW_AGENTS_DIR.is_dir():
            return
        for project_id, composite, row in iter_sessionslist_rows(self.name):
            native = row.get("nativeSessionId")
            if not isinstance(native, str) or not native:
                continue
            # Fall back to "main" if the parse fails — matches the
            # default in adapters/openclaw/transcript.py:128.
            agent = "main"
            if ":" in composite:
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

            for ev in self._normalize_event(raw, current_sid=sid):
                ev = dataclasses.replace(ev, project_id=project_id)
                nsid = ev.native_session_id
                if nsid and nsid not in self._sessions_seen:
                    self._sessions_seen.add(nsid)
                    yield SessionFirstSeen(
                        ts=ev.ts,
                        native_session_id=nsid,
                        runtime=self.name,
                        project_id=project_id,
                        cwd="",  # OpenClaw doesn't surface a cwd per line
                    )
                # Latency tracking: stash user-message ts, attach
                # latency_ms to the next UsageObserved for this
                # session, pop on use so each user message
                # contributes at most one sample.
                if isinstance(ev, MessageObserved) and ev.role == "user" and nsid:
                    self._last_user_ts[nsid] = ev.ts
                elif isinstance(ev, UsageObserved) and nsid:
                    user_ts = self._last_user_ts.pop(nsid, None)
                    if user_ts is not None:
                        latency = compute_latency_ms(user_ts, ev.ts)
                        if latency is not None:
                            ev = dataclasses.replace(ev, latency_ms=latency)
                yield ev

    # ── Normaliser ──────────────────────────────────────────────────────

    def _normalize_event(self, raw: dict, *, current_sid: str) -> Iterator[Event]:
        """Yield zero or more normalised events from one raw OpenClaw
        jsonl line.

        Unlike Claude, OpenClaw doesn't repeat the session id on every
        message — the caller caches it from the header row and passes
        it in via ``current_sid``. Header rows (``type:"session"``) are
        handled in :meth:`_tail_one`; this method is only called for
        message rows.
        """
        if not isinstance(raw, dict):
            return
        if raw.get("type") != "message":
            return

        ts = raw.get("timestamp")
        if not isinstance(ts, str) or not ts:
            return
        if not current_sid:
            return

        msg = raw.get("message")
        if not isinstance(msg, dict):
            return
        role = msg.get("role")
        if role not in ("user", "assistant"):
            return

        model: Optional[str] = None
        if role == "assistant":
            m = msg.get("model")
            if isinstance(m, str) and m:
                model = m

        yield MessageObserved(
            ts=ts, native_session_id=current_sid, runtime=self.name,
            role=role, model=model,
        )

        usage = msg.get("usage")
        if isinstance(usage, dict):
            # OpenClaw token field names → Claude-shaped fields.
            yield UsageObserved(
                ts=ts, native_session_id=current_sid, runtime=self.name,
                input_tokens=int(usage.get("input", 0) or 0),
                output_tokens=int(usage.get("output", 0) or 0),
                cache_read_input_tokens=int(usage.get("cacheRead", 0) or 0),
                cache_creation_input_tokens=int(usage.get("cacheWrite", 0) or 0),
                model=model,
            )

        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "toolCall":
                    continue
                name = block.get("name")
                if isinstance(name, str) and name:
                    yield ToolUseObserved(
                        ts=ts, native_session_id=current_sid, runtime=self.name,
                        tool=name,
                    )
