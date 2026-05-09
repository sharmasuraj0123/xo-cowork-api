"""
Tee OpenClaw exchanges into the project's ``.xo/sessions/`` directory.

OpenClaw's gateway is the source of truth for session state (used for
resume via the session-key header). This module writes a project-local
transcript copy alongside the gateway's files so the canonical
``xo-projects/<project>/.xo/sessions/`` layout has the same data the
harness reads for any other backend.

The target project is determined by the explicit ``xo_agent_id`` argument
(the subdirectory name under ``~/xo-projects/``). If not supplied it falls
back to the agent ID embedded in the session key. The ``.xo/sessions/``
directory is created on demand — no pre-existing project scaffold required.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.helpers import short_id
from services.cowork_agent.project_layout import xo_projects_root


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
    xo_agent_id: str | None = None,
) -> None:
    """Append (user, assistant) turn to ``<project>/.xo/sessions/{session_id}.jsonl``
    and bump the project's sessions.json entry.

    ``xo_agent_id`` is the subdirectory name under ``~/xo-projects/``. When
    not supplied the function returns without writing — agent-only chats
    (no project selected) are intentionally not mirrored under xo-projects;
    the openclaw native session files in ``~/.openclaw/agents/<id>/sessions/``
    are the source of truth in that case.
    """
    if not xo_agent_id or not session_id:
        return
    agent_id = xo_agent_id

    xo = xo_projects_root() / agent_id / ".xo"
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
