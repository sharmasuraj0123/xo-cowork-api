"""
Session-file I/O: scan `~/xo-projects/*/.xo/sessions/` and
`~/.openclaw/agents/*/sessions/` directories.

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
from services.cowork_agent.project_layout import xo_projects_root


def load_all_sessions() -> list[dict]:
    """Scan all agents and build SessionResponse objects.

    Only loads sessions from the canonical xo-projects layout
    (~/xo-projects/<id>/.xo/sessions/sessions.json). Legacy locations
    (~/claude-cowork/, ~/.openclaw/agents/) are no longer surfaced.
    """
    sessions = []
    seen_ids: set[str] = set()

    def _ingest_project_sessions_dir(sessions_dir: Path, agent_name: str, project_dir: Path) -> None:
        sessions_index = sessions_dir / "sessions.json"
        if not sessions_index.exists():
            return
        try:
            with open(sessions_index, encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception:
            return

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

            # For openclaw sessions the session key is "agent:<id>:web:<random>".
            # Use that embedded id so sessions group under the openclaw agent,
            # not the project folder they happen to be stored in.
            if meta.get("backend") == "openclaw":
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
                "directory": meta.get("directory") or str(project_dir),
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

    sessions.sort(key=lambda s: s["time_updated"], reverse=True)
    return sessions


def find_session_file(session_id: str) -> Path | None:
    """Find the JSONL messages file for a session."""
    # OpenClaw agents — ~/.openclaw/agents/<id>/sessions/<sid>.jsonl
    if AGENTS_DIR.exists():
        for agent_dir in AGENTS_DIR.iterdir():
            if not agent_dir.is_dir():
                continue
            path = agent_dir / "sessions" / f"{session_id}.jsonl"
            if path.exists():
                return path

    # Current xo-projects layout: ~/xo-projects/<id>/.xo/sessions/<sid>.jsonl
    projects_root = xo_projects_root()
    if projects_root.exists():
        for agent_dir in projects_root.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            path = agent_dir / ".xo" / "sessions" / f"{session_id}.jsonl"
            if path.exists():
                return path

    return None


def find_session_key(session_id: str) -> str | None:
    """Look up the session key for a given session ID."""
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

    # Current xo-projects layout: ~/xo-projects/<id>/.xo/sessions/sessions.json
    projects_root = xo_projects_root()
    if projects_root.exists():
        for agent_dir in projects_root.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            index_path = agent_dir / ".xo" / "sessions" / "sessions.json"
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

    Iterates every adapter's session_lookup_specs() — a list of
    ``(root, sessions_subpath)`` pairs. For each pair we walk the root's
    immediate subdirectories and check for
    ``<entry>/<subpath>/<session_id>.jsonl``.

    When the matching ``sessions.json`` entry carries a ``backend`` tag,
    that tag wins — this avoids mis-attributing tee'd transcripts (e.g.
    an openclaw mirror inside a project's ``.xo/sessions/`` would
    otherwise look like claude_code under that lookup spec).
    """
    from services.cowork_agent.adapter_registry import get_session_lookup_specs

    def _tagged_backend(idx_path: Path) -> str | None:
        if not idx_path.exists():
            return None
        try:
            idx = json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(idx, dict):
            return None
        for meta in idx.values():
            if isinstance(meta, dict) and meta.get("sessionId") == session_id:
                tag = meta.get("backend")
                if isinstance(tag, str) and tag.strip():
                    return tag.strip()
        return None

    for adapter_name, specs in get_session_lookup_specs().items():
        for root, subpath in specs:
            if not root.exists():
                continue
            for agent_dir in root.iterdir():
                if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                    continue
                if (agent_dir / subpath / f"{session_id}.jsonl").exists():
                    return _tagged_backend(agent_dir / subpath / "sessions.json") or adapter_name

    return None


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
            if _try_index(agent_dir / ".xo" / "sessions" / "sessions.json"):
                return True

    return False
