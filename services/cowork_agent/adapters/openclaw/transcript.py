"""
Tee OpenClaw exchanges into the project's ``.xo/sessions/`` directory.

OpenClaw's gateway is the source of truth for session state (used for
resume via the session-key header). This module writes a project-local
transcript copy alongside the gateway's files so the canonical
``xo-projects/<project>/.xo/sessions/`` layout has the same data the
harness reads for any other backend.

Skips silently if the registered workspace for the openclaw agent_id
isn't laid out as an xo-projects project (no ``.xo/`` directory). That
keeps legacy ``~/.openclaw/workspace/<id>/`` agents working unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.helpers import short_id
from services.cowork_agent.openclaw_store import (
    list_agent_entries,
    load_openclaw_config,
    resolve_agent_workspace_dir,
)


def _agent_id_from_session_key(session_key: str | None) -> str | None:
    """``agent:<agent_id>:web:<random>`` → ``<agent_id>`` (or None)."""
    parts = (session_key or "").split(":")
    return parts[1] if len(parts) >= 2 and parts[1] else None


def _project_xo_dir_for_agent(agent_id: str) -> Path | None:
    """Return ``<workspace>/.xo`` for the openclaw agent's registered workspace,
    or ``None`` if the workspace doesn't have an ``.xo/`` (i.e. legacy layout)."""
    cfg = load_openclaw_config()
    if not list_agent_entries(cfg):
        return None
    workspace = resolve_agent_workspace_dir(cfg, agent_id)
    xo = workspace / ".xo"
    return xo if xo.is_dir() else None


def _write_index_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def tee_exchange(
    session_key: str,
    session_id: str,
    question: str,
    response_text: str,
    model_id: str = "",
) -> None:
    """Append (user, assistant) turn to ``<project>/.xo/sessions/{session_id}.jsonl``
    and bump the project's sessions.json entry. Resume still goes through the
    gateway; this is read-only mirror data for the harness.
    """
    agent_id = _agent_id_from_session_key(session_key)
    if not agent_id or not session_id:
        return
    xo = _project_xo_dir_for_agent(agent_id)
    if xo is None:
        return

    sessions_dir = xo / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    transcript = sessions_dir / f"{session_id}.jsonl"
    with transcript.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "message",
            "id": short_id(),
            "timestamp": now_iso,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": question}],
            },
        }) + "\n")
        f.write(json.dumps({
            "type": "message",
            "id": short_id(),
            "timestamp": now_iso,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": response_text}],
                "model": model_id,
                "stopReason": "stop",
            },
        }) + "\n")

    index_path = sessions_dir / "sessions.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    except Exception:
        index = {}
    if not isinstance(index, dict):
        index = {}

    entry = dict(index.get(session_key) or {})
    entry.update({
        "sessionId": session_id,
        "nativeSessionId": session_id,
        "directory": str(xo.parent),
        "backend": "openclaw",
        "updatedAt": now_ms,
    })
    index[session_key] = entry
    _write_index_atomic(index_path, index)
