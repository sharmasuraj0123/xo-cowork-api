"""Write a row to ``<project>/.xo/sessions/sessionslist.json`` after
each hermes streaming exchange.

Mirrors what ``adapters/claude_code/adapter.py:write_preliminary_entry``
and ``adapters/openclaw/transcript.py:tee_exchange`` do for their
backends. Without this, hermes sessions are invisible to the
per-project xo-coworker dashboard — the watcher never sees them
because hermes writes its real session state to SQLite, not JSONL.

V1 limitation: ``usage`` is written as zeros. Hermes records token
counts in ``~/.hermes/state.db`` / ``~/.hermes/profiles/<name>/state.db``
on a 3–10 s delay (see :func:`hermes_state_db.register_inflight_exchange`).
A future enhancement can backfill the usage block from state.db once
hermes commits; each subsequent turn already overwrites the row with
a fresh ``updatedAt``, so the totals will catch up naturally once a
reader exists. The row's per-turn refresh is what matters for v1; the
dashboard's "totalMessages" / "totalTokens" widgets degrade gracefully
to zero until the backfill lands.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.cowork_agent.project_layout import xo_projects_root

logger = logging.getLogger(__name__)


def _write_index_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _composite_key(agent_id: str, our_session_id: str) -> str:
    """``hermes:<agent_id>:<surface>:<8hex>`` — mirrors the openclaw
    shape parsed by the visualizer (sessions_io / source).

    The 8-hex suffix is derived from ``our_session_id`` so the same
    xo-cowork session always maps to the same composite key (and so
    repeated writes overwrite the same row instead of accumulating
    duplicates).
    """
    suffix = our_session_id.replace("-", "")[:8] if our_session_id else "00000000"
    return f"hermes:{agent_id}:web:{suffix}"


def write_session_row(
    *,
    agent_id: Optional[str],
    our_session_id: Optional[str],
    native_session_id: Optional[str],
) -> None:
    """Upsert one row in ``<project>/.xo/sessions/sessionslist.json``.

    No-op when ``agent_id`` is missing (agent-only chat with no
    project selected) or ``native_session_id`` is missing (hermes
    didn't surface one — usually means the request errored before any
    session was created). All on-disk errors are swallowed with a
    log: the dashboard write must never fail a chat.
    """
    if not agent_id or not native_session_id:
        return
    try:
        project_dir = xo_projects_root() / agent_id
        sessions_dir = project_dir / ".xo" / "sessions"
        index_path = sessions_dir / "sessionslist.json"

        try:
            index = (
                json.loads(index_path.read_text(encoding="utf-8"))
                if index_path.exists()
                else {}
            )
        except (OSError, json.JSONDecodeError):
            index = {}
        if not isinstance(index, dict):
            index = {}

        composite = _composite_key(agent_id, our_session_id or native_session_id)
        existing = index.get(composite) if isinstance(index.get(composite), dict) else {}
        usage = (existing.get("usage") if isinstance(existing, dict) else None) or {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

        index[composite] = {
            "sessionId": our_session_id or native_session_id,
            "nativeSessionId": native_session_id,
            "directory": str(project_dir),
            "backend": "hermes",
            "updatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
            "usage": usage,
        }
        _write_index_atomic(index_path, index)
    except Exception as exc:  # noqa: BLE001 — never fail a chat for a dashboard write
        logger.warning("hermes sessionslist write failed: %s", exc)
