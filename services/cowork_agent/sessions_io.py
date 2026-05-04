"""
Session-file I/O: scan the `~/.openclaw/agents/*/sessions/` directories and
`~/claude-cowork/*/.sessions/` directories.

Concerns:
- listing sessions across agents and sorting by updated time
- finding the JSONL file or session key for a given session id
- persisting a user-selected `directory` into the matching `sessions.json` entry
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.settings import AGENTS_DIR, CLAUDE_COWORK_DIR, OPENCLAW_DIR
from services.cowork_agent.helpers import derive_title, iso_now, ms_to_iso, parse_jsonl


def load_all_sessions() -> list[dict]:
    """Scan all agents (OpenClaw and Claude Code) and build SessionResponse objects."""
    sessions = []

    # OpenClaw sessions
    if AGENTS_DIR.exists():
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

    # Claude Code sessions — current layout: ~/claude-cowork/{agent_id}/sessions/sessions.json
    seen_ids: set[str] = {s["id"] for s in sessions}
    if CLAUDE_COWORK_DIR.exists():
        for agent_dir in sorted(CLAUDE_COWORK_DIR.iterdir()):
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
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
                    "agent": agent_dir.name,
                    "directory": meta.get("directory") or str(agent_dir),
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

    # Claude Code sessions — old backward-compat layout: ~/claude-cowork/{agent_id}/.sessions/*.json
    if CLAUDE_COWORK_DIR.exists():
        cc_scan: list[tuple[Path, str, Path]] = []
        for agent_dir in sorted(CLAUDE_COWORK_DIR.iterdir()):
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            cc_scan.append((agent_dir / ".sessions", agent_dir.name, agent_dir))
        cc_scan.append((CLAUDE_COWORK_DIR / ".sessions", "default", CLAUDE_COWORK_DIR))

        for sessions_dir, agent_name, agent_dir in cc_scan:
            if not sessions_dir.exists():
                continue

            for session_file in sorted(sessions_dir.iterdir()):
                if session_file.suffix != ".json":
                    continue
                try:
                    rec = json.loads(session_file.read_text())
                except Exception:
                    continue

                session_id = rec.get("session_id", "")
                if not session_id or session_id in seen_ids:
                    continue
                seen_ids.add(session_id)

                sessions.append({
                    "id": session_id,
                    "project_id": None,
                    "parent_id": None,
                    "slug": None,
                    "agent": agent_name,
                    "directory": str(agent_dir),
                    "title": rec.get("title") or "Untitled Session",
                    "version": 1,
                    "summary_additions": 0,
                    "summary_deletions": 0,
                    "summary_files": 0,
                    "summary_diffs": [],
                    "is_pinned": False,
                    "permission": {},
                    "time_created": rec.get("created_at") or iso_now(),
                    "time_updated": rec.get("updated_at") or iso_now(),
                    "time_compacting": None,
                    "time_archived": None,
                })

    sessions.sort(key=lambda s: s["time_updated"], reverse=True)
    return sessions


def find_session_file(session_id: str) -> Path | None:
    """Find the JSONL messages file for a session (OpenClaw or Claude Code)."""
    # OpenClaw agents
    if AGENTS_DIR.exists():
        for agent_dir in AGENTS_DIR.iterdir():
            if not agent_dir.is_dir():
                continue
            path = agent_dir / "sessions" / f"{session_id}.jsonl"
            if path.exists():
                return path

    # Claude Code current layout: ~/claude-cowork/{agent_id}/sessions/{session_id}.jsonl
    if CLAUDE_COWORK_DIR.exists():
        for agent_dir in CLAUDE_COWORK_DIR.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            path = agent_dir / "sessions" / f"{session_id}.jsonl"
            if path.exists():
                return path

    # Claude Code old format: look for saved .messages.jsonl files
    from services.cowork_agent.claude_sessions import find_session_messages_path
    return find_session_messages_path(session_id)


def find_session_key(session_id: str) -> str | None:
    """Look up the session key for a given session ID (OpenClaw or Claude Code)."""
    # OpenClaw agents
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

    # Claude Code current layout: ~/claude-cowork/{agent_id}/sessions/sessions.json
    if CLAUDE_COWORK_DIR.exists():
        for agent_dir in CLAUDE_COWORK_DIR.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
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

    return None


def find_session_backend(session_id: str) -> str | None:
    """Return the adapter name that owns session_id, or None.

    Iterates every registered adapter's sessions_root(). Each root's direct
    subdirectories are expected to contain a sessions/{session_id}.jsonl file.
    When a new adapter is added, only its sessions_root() classmethod needs to
    be implemented — this function requires no changes.
    """
    from services.cowork_agent.adapter_registry import get_sessions_roots

    for adapter_name, root in get_sessions_roots().items():
        if not root.exists():
            continue
        for agent_dir in root.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            if (agent_dir / "sessions" / f"{session_id}.jsonl").exists():
                return adapter_name

    # Legacy claude_code format: ~/claude-cowork/{agent_id}/.sessions/*.json
    from services.cowork_agent.claude_sessions import load_session
    if load_session(session_id) is not None:
        return "claude_code"

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


def update_claude_session_directory(session_id: str, directory: str) -> bool:
    """
    Update the workspace directory for a Claude Code session.

    Checks the current index format (~/claude-cowork/{agent_id}/sessions/sessions.json)
    first, then falls back to the old .sessions/{session_id}.json format.
    Returns True if the record was found and updated.
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    # Current index format: ~/claude-cowork/{agent_id}/sessions/sessions.json
    if CLAUDE_COWORK_DIR.exists():
        for agent_dir in CLAUDE_COWORK_DIR.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
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
                history = meta.get("directoryHistory") or []
                history.append({"directory": directory, "selectedAt": now_ms})
                meta["directoryHistory"] = history[-200:]
                meta["directory"] = directory
                meta["updatedAt"] = now_ms
                changed = True
                break

            if changed:
                index_path.write_text(
                    json.dumps(index_data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return True

    # Fall back to old-format .sessions/{session_id}.json
    return _try_update_old_claude_session(session_id, directory)


def _try_update_old_claude_session(session_id: str, directory: str) -> bool:
    """Update directory in the old ~/claude-cowork/{agent_id}/.sessions/{session_id}.json format."""
    if not CLAUDE_COWORK_DIR.exists():
        return False
    # Root .sessions
    path = CLAUDE_COWORK_DIR / ".sessions" / f"{session_id}.json"
    if path.exists():
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            rec["directory"] = directory
            path.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
            return True
        except Exception:
            pass
    for agent_dir in CLAUDE_COWORK_DIR.iterdir():
        if not agent_dir.is_dir() or agent_dir.name.startswith("."):
            continue
        path = agent_dir / ".sessions" / f"{session_id}.json"
        if path.exists():
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
                rec["directory"] = directory
                path.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
                return True
            except Exception:
                pass
    return False
