"""
OpenClaw session-listing + lookup logic for ``OpenclawAdapter.list_sessions``
and ``list_messages``.

Ported from ``services/cowork_agent/sessions_io.py`` during Phase 4. The
service-layer file keeps its own copy of this logic for now (so both code
paths exist in parallel); Phase 5 will collapse the duplicates once
shared routers call through the dispatcher.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .settings import AGENTS_DIR
from services.cowork_agent.helpers import (
    derive_title,
    iso_now,
    ms_to_iso,
    parse_jsonl,
)
from services.cowork_agent.project_layout import xo_projects_root


def _resolve_index_path(sessions_dir: Path) -> Path | None:
    """Return the first existing index file, preferring sessionslist.json."""
    for fname in ("sessionslist.json", "sessions.json"):
        p = sessions_dir / fname
        if p.exists():
            return p
    return None


def _session_record_from_index(
    key: str,
    meta: dict,
    *,
    fallback_agent: str,
    fallback_directory: str | None = None,
    title_records: list | None = None,
) -> dict | None:
    """Build the SessionResponse dict for one ``sessionslist.json`` entry."""
    session_id = meta.get("sessionId", "")
    if not session_id:
        return None

    updated_at = meta.get("updatedAt")
    time_updated = ms_to_iso(updated_at) if updated_at else iso_now()
    time_created = time_updated
    title = "Untitled Session"

    if title_records:
        try:
            first_ts = title_records[0].get("timestamp")
            if first_ts:
                time_created = first_ts
            title = derive_title(title_records)
        except Exception:
            pass

    parts = key.split(":")
    effective_agent = parts[1] if len(parts) >= 2 and parts[1] else fallback_agent

    return {
        "id": session_id,
        "project_id": None,
        "parent_id": None,
        "slug": None,
        "agent": effective_agent,
        "directory": meta.get("directory") or fallback_directory or "",
        "title": title,
        "version": 1,
        "summary_additions": 0,
        "summary_deletions": 0,
        "summary_files": 0,
        "summary_diffs": [],
        "is_pinned": False,
        "permission": {},
        "time_created": time_created,
        "time_updated": time_updated,
        "time_compacting": None,
        "time_archived": None,
    }


def list_openclaw_sessions() -> list[dict]:
    """Return SessionResponse dicts for every OpenClaw session on disk.

    Scans both source locations:
    - Project-tied: ``~/xo-projects/<id>/.xo/sessions/sessionslist.json`` rows
      whose ``backend == "openclaw"``.
    - Native: ``~/.openclaw/agents/<id>/sessions/sessions.json`` (no project).

    De-duplicates by ``sessionId`` so a tee'd session that is both project-tied
    and natively present surfaces only once (project-tied wins, scanned first).
    """
    sessions: list[dict] = []
    seen_ids: set[str] = set()

    projects_root = xo_projects_root()
    if projects_root.exists():
        for agent_dir in sorted(projects_root.iterdir()):
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            idx_path = _resolve_index_path(agent_dir / ".xo" / "sessions")
            if not idx_path:
                continue
            try:
                index = json.loads(idx_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for key, meta in index.items():
                if not isinstance(meta, dict):
                    continue
                if meta.get("backend") != "openclaw":
                    continue
                session_id = meta.get("sessionId", "")
                if not session_id or session_id in seen_ids:
                    continue

                # Walk the native JSONL for title + first-message timestamp.
                title_records = None
                parts = key.split(":")
                oc_agent = parts[1] if len(parts) >= 2 and parts[1] else agent_dir.name
                if AGENTS_DIR.exists():
                    oc_file = AGENTS_DIR / oc_agent / "sessions" / f"{session_id}.jsonl"
                    if oc_file.exists():
                        try:
                            title_records = parse_jsonl(oc_file)
                        except Exception:
                            title_records = None

                record = _session_record_from_index(
                    key,
                    meta,
                    fallback_agent=agent_dir.name,
                    fallback_directory=str(agent_dir),
                    title_records=title_records,
                )
                if record:
                    seen_ids.add(session_id)
                    sessions.append(record)

    if AGENTS_DIR.exists():
        for agent_dir in sorted(AGENTS_DIR.iterdir()):
            if not agent_dir.is_dir():
                continue
            sessions_index = agent_dir / "sessions" / "sessions.json"
            if not sessions_index.exists():
                continue
            try:
                index = json.loads(sessions_index.read_text(encoding="utf-8"))
            except Exception:
                continue
            for key, meta in index.items():
                if not isinstance(meta, dict):
                    continue
                session_id = meta.get("sessionId", "")
                if not session_id or session_id in seen_ids:
                    continue

                session_file = agent_dir / "sessions" / f"{session_id}.jsonl"
                title_records = None
                if session_file.exists():
                    try:
                        title_records = parse_jsonl(session_file)
                    except Exception:
                        title_records = None

                record = _session_record_from_index(
                    key,
                    meta,
                    fallback_agent=agent_dir.name,
                    title_records=title_records,
                )
                if record:
                    seen_ids.add(session_id)
                    sessions.append(record)

    sessions.sort(key=lambda s: s["time_updated"], reverse=True)
    return sessions


def find_openclaw_session_jsonl(session_id: str) -> Path | None:
    """Locate the native JSONL transcript for an OpenClaw session.

    Returns ``None`` if the session does not exist under any agent.
    """
    if not AGENTS_DIR.exists():
        return None
    for agent_dir in AGENTS_DIR.iterdir():
        if not agent_dir.is_dir():
            continue
        path = agent_dir / "sessions" / f"{session_id}.jsonl"
        if path.exists():
            return path
    return None


def find_openclaw_session_key(session_id: str) -> str | None:
    """Return the OpenClaw session key for a given session id, or None.

    Walks two locations:
    - ``~/.openclaw/agents/<a>/sessions/sessions.json`` (native sessions)
    - ``~/xo-projects/<id>/.xo/sessions/sessionslist.json`` (tee'd sessions,
      both ``claude_code`` and ``openclaw`` rows — only openclaw rows carry
      a session_key that the gateway understands).

    Used by the OpenClaw adapter + chat fast-path; the session_key is the
    header value OpenClaw's gateway requires for resume.
    """
    if AGENTS_DIR.exists():
        for agent_dir in AGENTS_DIR.iterdir():
            if not agent_dir.is_dir():
                continue
            index_path = agent_dir / "sessions" / "sessions.json"
            if not index_path.exists():
                continue
            try:
                with open(index_path, encoding="utf-8") as f:
                    index_data = json.load(f)
            except Exception:
                continue
            for key, meta in index_data.items():
                if isinstance(meta, dict) and meta.get("sessionId") == session_id:
                    return key

    projects_root = xo_projects_root()
    if projects_root.exists():
        for agent_dir in projects_root.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            idx_path = _resolve_index_path(agent_dir / ".xo" / "sessions")
            if not idx_path:
                continue
            try:
                with open(idx_path, encoding="utf-8") as f:
                    index_data = json.load(f)
            except Exception:
                continue
            for key, meta in index_data.items():
                if isinstance(meta, dict) and meta.get("sessionId") == session_id:
                    return key

    return None


def update_openclaw_session_directory(session_id: str, directory: str) -> bool:
    """Persist a selected workspace directory on the matching OpenClaw
    ``sessions.json`` entry. Returns True on a successful write, False if
    the session id wasn't found under any agent.

    Records the change in the entry's ``directoryHistory`` (capped at 200
    rows) plus the current ``directory`` and ``updatedAt`` fields.
    """
    if not AGENTS_DIR.exists():
        return False

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for agent_dir in AGENTS_DIR.iterdir():
        if not agent_dir.is_dir():
            continue
        index_path = agent_dir / "sessions" / "sessions.json"
        if not index_path.exists():
            continue
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception:
            continue

        changed = False
        for meta in index_data.values():
            if not isinstance(meta, dict) or meta.get("sessionId") != session_id:
                continue
            history = meta.get("directoryHistory")
            if not isinstance(history, list):
                history = []
            history.append({"directory": directory, "selectedAt": now_ms})
            meta["directoryHistory"] = history[-200:]
            meta["directory"] = directory
            meta["updatedAt"] = now_ms
            changed = True
            break

        if changed:
            index_path.write_text(
                json.dumps(index_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True

    return False
