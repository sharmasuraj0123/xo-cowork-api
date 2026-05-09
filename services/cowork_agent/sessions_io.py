"""
Session-file I/O: scan `~/xo-projects/*/.xo/sessions/` and
`~/.openclaw/agents/*/sessions/` directories.

Concerns:
- listing sessions across agents and sorting by updated time
- finding the message file for a given session id
- persisting a user-selected `directory` into the matching sessionslist.json entry

Security model
--------------
The project folder (.xo/sessions/) holds only metadata (sessionslist.json).
Chat messages are never written there. They live in the provider's own
storage:
  claude_code → ~/.claude/projects/{encoded_dir}/{nativeSessionId}.jsonl
  openclaw    → ~/.openclaw/agents/{agent}/sessions/{sessionId}.jsonl
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.settings import AGENTS_DIR, OPENCLAW_DIR
from services.cowork_agent.helpers import (
    derive_title,
    derive_title_native_claude,
    iso_now,
    ms_to_iso,
    parse_jsonl,
)
from services.cowork_agent.project_layout import xo_projects_root


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
    """Scan all agents and build SessionResponse objects.

    Two scan roots:
    - ``~/xo-projects/<id>/.xo/sessions/`` — project-tied sessions (claude_code
      and openclaw chats with a project workspace selected).
    - ``~/.openclaw/agents/<id>/sessions/`` — openclaw native sessions for
      agent-only chats (no project picked).

    De-duplicated via ``sessionId`` so a session that is both project-tee'd
    and natively present surfaces only once (project-tied wins).
    """
    sessions = []
    seen_ids: set[str] = set()

    def _ingest_project_sessions_dir(sessions_dir: Path, agent_name: str, project_dir: Path) -> None:
        idx_path = _resolve_index_path(sessions_dir)
        if not idx_path:
            return
        try:
            with open(idx_path, encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception:
            return

        for key, meta in index_data.items():
            session_id = meta.get("sessionId", "")
            if not session_id or session_id in seen_ids:
                continue
            seen_ids.add(session_id)

            updated_at = meta.get("updatedAt")
            time_updated = ms_to_iso(updated_at) if updated_at else iso_now()
            time_created = time_updated
            title = "Untitled Session"

            backend = meta.get("backend", "")
            native_id = meta.get("nativeSessionId", "")
            directory = meta.get("directory", "")

            if backend == "claude_code":
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
            elif backend == "openclaw":
                # Messages live in ~/.openclaw/agents/*/sessions/ — find by sessionId.
                parts = key.split(":")
                oc_agent = parts[1] if len(parts) >= 2 and parts[1] else agent_name
                if AGENTS_DIR.exists():
                    oc_file = AGENTS_DIR / oc_agent / "sessions" / f"{session_id}.jsonl"
                    if oc_file.exists():
                        try:
                            records = parse_jsonl(oc_file)
                            if records:
                                ts = records[0].get("timestamp")
                                if ts:
                                    time_created = ts
                            title = derive_title(records)
                        except Exception:
                            pass

            if backend == "openclaw":
                parts = key.split(":")
                effective_agent = parts[1] if len(parts) >= 2 and parts[1] else agent_name
            else:
                effective_agent = agent_name

            sessions.append({
                "id": session_id,
                "project_id": None,
                "parent_id": None,
                "slug": None,
                "agent": effective_agent,
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

    projects_root = xo_projects_root()
    if projects_root.exists():
        for agent_dir in sorted(projects_root.iterdir()):
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            _ingest_project_sessions_dir(agent_dir / ".xo" / "sessions", agent_dir.name, agent_dir)

    # OpenClaw native sessions (no project picked at chat time).
    if AGENTS_DIR.exists():
        for agent_dir in sorted(AGENTS_DIR.iterdir()):
            if not agent_dir.is_dir():
                continue
            sessions_dir = agent_dir / "sessions"
            sessions_index = sessions_dir / "sessions.json"
            if not sessions_index.exists():
                continue
            try:
                with open(sessions_index, encoding="utf-8") as f:
                    index_data = json.load(f)
            except Exception:
                continue
            for key, meta in index_data.items():
                if not isinstance(meta, dict):
                    continue
                session_id = meta.get("sessionId", "")
                if not session_id or session_id in seen_ids:
                    continue
                seen_ids.add(session_id)

                session_file = sessions_dir / f"{session_id}.jsonl"
                updated_at = meta.get("updatedAt")
                time_updated = ms_to_iso(updated_at) if updated_at else iso_now()
                time_created = time_updated
                title = "Untitled Session"
                if session_file.exists():
                    try:
                        records = parse_jsonl(session_file)
                        if records:
                            ts = records[0].get("timestamp")
                            if ts:
                                time_created = ts
                        title = derive_title(records)
                    except Exception:
                        pass

                parts = key.split(":")
                effective_agent = parts[1] if len(parts) >= 2 and parts[1] else agent_dir.name

                sessions.append({
                    "id": session_id,
                    "project_id": None,
                    "parent_id": None,
                    "slug": None,
                    "agent": effective_agent,
                    "directory": meta.get("directory") or "",
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

    sessions.sort(key=lambda s: s["time_updated"], reverse=True)
    return sessions


# ── Message file lookup ───────────────────────────────────────────────────────


def find_session_file(session_id: str) -> Path | None:
    """Find the JSONL messages file for a session.

    For claude_code sessions: looks up nativeSessionId + directory from
    sessionslist.json and returns the file from ~/.claude/projects/.
    For openclaw sessions: returns the file from ~/.openclaw/agents/.
    """
    # xo-projects: check sessionslist.json for metadata to find native file.
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
                backend = meta.get("backend", "")
                if backend == "claude_code":
                    path = _find_native_claude_file(
                        meta.get("nativeSessionId", ""),
                        meta.get("directory", ""),
                    )
                    if path:
                        return path
                elif backend == "openclaw":
                    # Messages are in the OpenClaw native directory.
                    if AGENTS_DIR.exists():
                        for oc_dir in AGENTS_DIR.iterdir():
                            if not oc_dir.is_dir():
                                continue
                            p = oc_dir / "sessions" / f"{session_id}.jsonl"
                            if p.exists():
                                return p

    # OpenClaw native sessions (no project selected).
    if AGENTS_DIR.exists():
        for agent_dir in AGENTS_DIR.iterdir():
            if not agent_dir.is_dir():
                continue
            path = agent_dir / "sessions" / f"{session_id}.jsonl"
            if path.exists():
                return path

    return None


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

    # OpenClaw native sessions.
    if AGENTS_DIR.exists():
        for agent_dir in AGENTS_DIR.iterdir():
            if not agent_dir.is_dir():
                continue
            if (agent_dir / "sessions" / f"{session_id}.jsonl").exists():
                return "openclaw"

    return None


def find_session_key(session_id: str) -> str | None:
    """Look up the session key for a given session ID."""
    # OpenClaw agents native
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

    # xo-projects (claude_code and tee'd openclaw sessions)
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


# ── Directory update ──────────────────────────────────────────────────────────


def update_session_directory(session_id: str, directory: str) -> bool:
    """Persist selected workspace directory on the matching sessions.json entry (OpenClaw)."""
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
            index_path.write_text(json.dumps(index_data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True

    return False


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
