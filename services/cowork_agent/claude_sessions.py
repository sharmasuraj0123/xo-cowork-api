"""
Session storage for Claude Code agents.

Sessions live under ~/claude-cowork/{agent_id}/.sessions/{session_id}.json.
Each file contains:
  {session_id, native_session_id, agent_id, title, created_at, updated_at}

native_session_id is Claude CLI's --resume identifier.
session_id is the UUID this API exposes to the frontend.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.settings import CLAUDE_COWORK_DIR


def _sessions_dir(agent_id: str) -> Path:
    return CLAUDE_COWORK_DIR / agent_id / ".sessions"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_session(
    session_id: str,
    native_session_id: str | None,
    agent_id: str,
    title: str,
) -> None:
    d = _sessions_dir(agent_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.json"
    now = _iso_now()
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            existing = {}
        if native_session_id is not None:
            existing["native_session_id"] = native_session_id
        existing["title"] = title
        existing["updated_at"] = now
        path.write_text(json.dumps(existing, indent=2))
    else:
        path.write_text(json.dumps({
            "session_id": session_id,
            "native_session_id": native_session_id,
            "agent_id": agent_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
        }, indent=2))


def load_session(session_id: str) -> dict | None:
    """Find a session record by session_id across all claude-cowork agents."""
    if not CLAUDE_COWORK_DIR.exists():
        return None
    # Check root .sessions dir first (agent_id="" case)
    root_path = _sessions_dir("") / f"{session_id}.json"
    if root_path.exists():
        try:
            return json.loads(root_path.read_text())
        except Exception:
            return None
    for agent_dir in CLAUDE_COWORK_DIR.iterdir():
        if not agent_dir.is_dir() or agent_dir.name.startswith("."):
            continue
        path = _sessions_dir(agent_dir.name) / f"{session_id}.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                return None
    return None


def _messages_path(agent_id: str, session_id: str) -> Path:
    return _sessions_dir(agent_id) / f"{session_id}.messages.jsonl"


def save_session_messages(
    session_id: str,
    agent_id: str,
    question: str,
    response: str,
) -> None:
    """Save user/assistant pair as an OpenClaw-compatible messages JSONL file."""
    import uuid as _uuid
    path = _messages_path(agent_id, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = _iso_now()
    lines = [
        json.dumps({
            "type": "message",
            "id": _uuid.uuid4().hex,
            "timestamp": now,
            "message": {"role": "user", "content": [{"type": "text", "text": question}]},
        }),
        json.dumps({
            "type": "message",
            "id": _uuid.uuid4().hex,
            "timestamp": now,
            "message": {"role": "assistant", "content": [{"type": "text", "text": response}]},
        }),
    ]
    path.write_text("\n".join(lines) + "\n")


def find_session_messages_path(session_id: str) -> Path | None:
    """Find the messages JSONL for a Claude Code session across all agent dirs."""
    if not CLAUDE_COWORK_DIR.exists():
        return None
    root = _messages_path("", session_id)
    if root.exists():
        return root
    for agent_dir in CLAUDE_COWORK_DIR.iterdir():
        if not agent_dir.is_dir() or agent_dir.name.startswith("."):
            continue
        p = _messages_path(agent_dir.name, session_id)
        if p.exists():
            return p
    return None


def list_agent_sessions(agent_id: str) -> list[dict]:
    d = _sessions_dir(agent_id)
    if not d.exists():
        return []
    sessions = []
    for f in d.iterdir():
        if f.suffix != ".json":
            continue
        try:
            sessions.append(json.loads(f.read_text()))
        except Exception:
            pass
    sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return sessions
