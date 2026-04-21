"""
Session-file I/O: scan the `~/.openclaw/agents/*/sessions/` directories and
materialize xo-cowork-shaped session records.

Concerns:
- listing sessions across agents and sorting by updated time
- finding the JSONL file or session key for a given session id
- persisting a user-selected `directory` into the matching `sessions.json` entry
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.settings import AGENTS_DIR, OPENCLAW_DIR
from services.cowork_agent.helpers import derive_title, iso_now, ms_to_iso, parse_jsonl


def load_all_sessions() -> list[dict]:
    """Scan all agents and build SessionResponse objects."""
    sessions = []

    if not AGENTS_DIR.exists():
        return sessions

    for agent_dir in sorted(AGENTS_DIR.iterdir()):
        if not agent_dir.is_dir():
            continue

        agent_name = agent_dir.name
        sessions_dir = agent_dir / "sessions"
        sessions_index = sessions_dir / "sessions.json"

        if not sessions_index.exists():
            continue

        with open(sessions_index) as f:
            index_data = json.load(f)

        for key, meta in index_data.items():
            session_id = meta.get("sessionId", "")
            session_file = sessions_dir / f"{session_id}.jsonl"

            updated_at = meta.get("updatedAt")
            time_updated = ms_to_iso(updated_at) if updated_at else iso_now()

            time_created = time_updated
            title = "Untitled Session"
            if session_file.exists():
                records = parse_jsonl(session_file)
                if records:
                    ts = records[0].get("timestamp")
                    if ts:
                        time_created = ts
                title = derive_title(records)

            sessions.append({
                "id": session_id,
                "project_id": None,
                "parent_id": None,
                "slug": None,
                "agent": agent_name,
                "directory": meta.get("directory") or str(OPENCLAW_DIR / "workspace"),
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


def find_session_file(session_id: str) -> Path | None:
    """Find the JSONL file for a given session ID across all agents."""
    if not AGENTS_DIR.exists():
        return None
    for agent_dir in AGENTS_DIR.iterdir():
        if not agent_dir.is_dir():
            continue
        path = agent_dir / "sessions" / f"{session_id}.jsonl"
        if path.exists():
            return path
    return None


def find_session_key(session_id: str) -> str | None:
    """Look up the OpenClaw session key for a given session ID."""
    if not AGENTS_DIR.exists():
        return None
    for agent_dir in AGENTS_DIR.iterdir():
        if not agent_dir.is_dir():
            continue
        index_path = agent_dir / "sessions" / "sessions.json"
        if not index_path.exists():
            continue
        with open(index_path) as f:
            index_data = json.load(f)
        for key, meta in index_data.items():
            if meta.get("sessionId") == session_id:
                return key
    return None


def update_session_directory(session_id: str, directory: str) -> bool:
    """Persist selected workspace directory on the matching sessions.json entry."""
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
