"""
Session-file I/O: cross-backend coordinator.

Concerns:
- listing sessions across agents and sorting by updated time
- finding the message file for a given session id
- persisting a user-selected ``directory`` into the matching index entry
  (Claude Code only — OpenClaw's equivalent lives in
  ``adapters/openclaw/sessions_api.update_openclaw_session_directory``).

Backend-specific scan / lookup logic for OpenClaw lives in the adapter
(``adapters/openclaw/sessions_api``) and is imported lazily here so a
fork that deletes ``adapters/openclaw/`` boots cleanly. Hermes is
delegated through ``hermes_state_db``.

Security model
--------------
The project folder (.xo/sessions/) holds only metadata (sessionslist.json).
Chat messages are never written there. They live in the provider's own
storage:
  claude_code → ~/.claude/projects/{encoded_dir}/{nativeSessionId}.jsonl
  openclaw    → ~/.openclaw/agents/{agent}/sessions/{sessionId}.jsonl
  hermes      → ~/.hermes/state.db (or ~/.hermes/profiles/<name>/state.db) — SQLite,
                read-only via services.cowork_agent.hermes_state_db.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.helpers import (
    derive_title_native_claude,
    iso_now,
    ms_to_iso,
    parse_jsonl,
)
from services.cowork_agent.hermes_state_db import (
    find_hermes_profile,
    list_hermes_sessions,
)
from services.cowork_agent.project_layout import xo_projects_root


# ── OpenClaw delegation (graceful if the adapter package is removed) ──────────


def _try_list_openclaw_sessions() -> list[dict]:
    """Return OpenClaw sessions, or [] if the adapter isn't installed."""
    try:
        from services.cowork_agent.adapters.openclaw.sessions_api import (
            list_openclaw_sessions,
        )
    except ImportError:
        return []
    return list_openclaw_sessions()


def _try_find_openclaw_session_jsonl(session_id: str) -> Path | None:
    """Locate an OpenClaw session JSONL, or None if the adapter isn't installed."""
    try:
        from services.cowork_agent.adapters.openclaw.sessions_api import (
            find_openclaw_session_jsonl,
        )
    except ImportError:
        return None
    return find_openclaw_session_jsonl(session_id)


# ── Native Claude project-dir helpers ────────────────────────────────────────


def _encode_dir_for_claude(directory: str) -> str:
    """Convert an absolute path to the folder name Claude Code uses.

    Claude Code names its project folders by replacing every '/' with '-',
    so '/home/coder/xo-projects/blackhole' → '-home-coder-xo-projects-blackhole'.
    """
    return directory.replace("/", "-")


def _find_native_claude_file(native_session_id: str, directory: str) -> Path | None:
    """Return the path to a Claude Code native JSONL, or None if absent."""
    if not native_session_id or not directory:
        return None
    encoded = _encode_dir_for_claude(directory)
    path = Path.home() / ".claude" / "projects" / encoded / f"{native_session_id}.jsonl"
    return path if path.exists() else None


# ── Index filename resolution (new name + legacy fallback) ────────────────────


def _resolve_index_path(sessions_dir: Path) -> Path | None:
    """Return the first existing index file, preferring sessionslist.json."""
    for fname in ("sessionslist.json", "sessions.json"):
        p = sessions_dir / fname
        if p.exists():
            return p
    return None


# ── Session listing ───────────────────────────────────────────────────────────


def load_all_sessions() -> list[dict]:
    """Scan agents and build SessionResponse objects, filtered by active backend.

    Sources (only the ones matching ``AGENT_NAME`` are touched):
    - **openclaw**: ``adapters/openclaw/sessions_api.list_openclaw_sessions()``
      — handles both project-tied (``~/xo-projects/<id>/.xo/sessions/``) and
      native (``~/.openclaw/agents/<id>/sessions/``) scans with internal dedup.
    - **claude_code**: inline ``~/xo-projects/<id>/.xo/sessions/`` walk for
      rows with ``backend == "claude_code"``.
    - **hermes**: ``~/.hermes/state.db`` + per-profile dbs via
      ``hermes_state_db.list_hermes_sessions()``.

    De-duplicated via ``sessionId``.
    """
    import os
    active_backend = os.getenv("AGENT_NAME", "openclaw")

    sessions: list[dict] = []
    seen_ids: set[str] = set()

    # ── OpenClaw (delegated to adapter; empty if adapter not installed) ──
    if active_backend == "openclaw":
        for s in _try_list_openclaw_sessions():
            sid = s.get("id", "")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                sessions.append(s)

    # ── Claude Code (inline scan of xo-projects) ─────────────────────────
    if active_backend in ("openclaw", "claude_code"):
        projects_root = xo_projects_root()
        if projects_root.exists():
            for agent_dir in sorted(projects_root.iterdir()):
                if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                    continue
                _ingest_claude_project_sessions(
                    agent_dir / ".xo" / "sessions",
                    agent_dir.name,
                    agent_dir,
                    sessions,
                    seen_ids,
                )

    # ── Hermes (SQLite-backed scan) ──────────────────────────────────────
    if active_backend == "hermes":
        for hermes_session in list_hermes_sessions():
            sid = hermes_session["id"]
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            sessions.append(hermes_session)

    sessions.sort(key=lambda s: s["time_updated"], reverse=True)
    return sessions


def _ingest_claude_project_sessions(
    sessions_dir: Path,
    agent_name: str,
    project_dir: Path,
    sessions: list[dict],
    seen_ids: set[str],
) -> None:
    """Inline scan of xo-projects/<id>/.xo/sessions/, claude_code rows only.

    OpenClaw entries that may also appear in this index are intentionally
    skipped — they're served by the OpenClaw adapter via
    ``_try_list_openclaw_sessions``, which already covers both the project-tied
    and native locations with its own dedup.
    """
    idx_path = _resolve_index_path(sessions_dir)
    if not idx_path:
        return
    try:
        with open(idx_path, encoding="utf-8") as f:
            index_data = json.load(f)
    except Exception:
        return

    for _key, meta in index_data.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("backend") != "claude_code":
            continue

        session_id = meta.get("sessionId", "")
        if not session_id or session_id in seen_ids:
            continue
        seen_ids.add(session_id)

        updated_at = meta.get("updatedAt")
        time_updated = ms_to_iso(updated_at) if updated_at else iso_now()
        time_created = time_updated
        title = "Untitled Session"

        native_id = meta.get("nativeSessionId", "")
        directory = meta.get("directory", "")

        native_path = _find_native_claude_file(native_id, directory)
        if native_path:
            try:
                records = parse_jsonl(native_path)
                if records:
                    ts = records[0].get("timestamp")
                    if ts:
                        time_created = ts
                title = derive_title_native_claude(records)
            except Exception:
                pass

        sessions.append({
            "id": session_id,
            "project_id": None,
            "parent_id": None,
            "slug": None,
            "agent": agent_name,
            "directory": directory or str(project_dir),
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
        })


# ── Message file lookup ───────────────────────────────────────────────────────


def find_session_file(session_id: str) -> Path | None:
    """Find the JSONL messages file for a session.

    Walks xo-projects for claude_code rows (resolves to ``~/.claude/projects/``)
    and delegates to the OpenClaw adapter for any openclaw session — both
    project-tied and native are handled by ``find_openclaw_session_jsonl``.
    """
    projects_root = xo_projects_root()
    if projects_root.exists():
        for agent_dir in projects_root.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            idx_path = _resolve_index_path(agent_dir / ".xo" / "sessions")
            if not idx_path:
                continue
            try:
                index = json.loads(idx_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for meta in index.values():
                if not isinstance(meta, dict) or meta.get("sessionId") != session_id:
                    continue
                if meta.get("backend") == "claude_code":
                    path = _find_native_claude_file(
                        meta.get("nativeSessionId", ""),
                        meta.get("directory", ""),
                    )
                    if path:
                        return path

    # OpenClaw (project-tied with no resolved claude path above + native).
    return _try_find_openclaw_session_jsonl(session_id)


def find_session_backend(session_id: str) -> str | None:
    """Return the adapter name that owns session_id, or None."""
    # xo-projects: read backend tag directly from sessionslist.json.
    projects_root = xo_projects_root()
    if projects_root.exists():
        for agent_dir in projects_root.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            idx_path = _resolve_index_path(agent_dir / ".xo" / "sessions")
            if not idx_path:
                continue
            try:
                index = json.loads(idx_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for meta in index.values():
                if isinstance(meta, dict) and meta.get("sessionId") == session_id:
                    tag = meta.get("backend", "")
                    if tag:
                        return tag

    # OpenClaw native sessions (via adapter).
    if _try_find_openclaw_session_jsonl(session_id) is not None:
        return "openclaw"

    # Hermes sessions (SQLite-backed, scanned across every profile).
    if find_hermes_profile(session_id) is not None:
        return "hermes"

    return None


def update_claude_session_directory(session_id: str, directory: str) -> bool:
    """Update the workspace directory for a Claude Code session (xo-projects only)."""
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
