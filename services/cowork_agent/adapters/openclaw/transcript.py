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

from services.cowork_agent.project_layout import xo_projects_root
from services.cowork_agent.adapters.openclaw.paths import AGENTS_DIR


def _sum_usage(session_id: str, agent_name: str) -> dict | None:
    """Sum token usage across all assistant messages in the native OpenClaw JSONL."""
    path = AGENTS_DIR / agent_name / "sessions" / f"{session_id}.jsonl"
    if not path.exists():
        return None
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cost": 0.0}
    found = False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "message":
                continue
            msg = record.get("message", {})
            if msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if not usage:
                continue
            found = True
            totals["input_tokens"] += int(usage.get("input", 0) or 0)
            totals["output_tokens"] += int(usage.get("output", 0) or 0)
            totals["cache_read_input_tokens"] += int(usage.get("cacheRead", 0) or 0)
            totals["cache_creation_input_tokens"] += int(usage.get("cacheWrite", 0) or 0)
            cost_raw = usage.get("cost", 0)
            cost_val = float(cost_raw.get("total") or 0) if isinstance(cost_raw, dict) else float(cost_raw or 0)
            totals["cost"] += cost_val
    except Exception:
        pass
    if not found:
        return None
    totals["cost"] = round(totals["cost"], 6)
    return totals


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
    """Record session metadata in ``<project>/.xo/sessions/sessionslist.json``.

    Messages are NOT stored here — they live in the OpenClaw native session
    files under ``~/.openclaw/agents/<id>/sessions/``. This keeps the project
    folder free of chat content so it can be shared safely.

    ``xo_agent_id`` is the subdirectory name under ``~/xo-projects/``. When
    not supplied the function returns without writing — agent-only chats
    (no project selected) are not mirrored; the openclaw native files are the
    source of truth in that case.
    """
    if not xo_agent_id or not session_id:
        return
    agent_id = xo_agent_id

    xo = xo_projects_root() / agent_id / ".xo"
    sessions_dir = xo / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    index_path = sessions_dir / "sessionslist.json"
    # Fall back to legacy sessions.json if it exists and new file doesn't yet.
    if not index_path.exists():
        legacy = sessions_dir / "sessions.json"
        if legacy.exists():
            index_path = legacy
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

    # Read cumulative token usage directly from the native OpenClaw JSONL.
    # Summing the whole file on each turn avoids double-counting — we replace
    # rather than accumulate, so it stays accurate even if tee_exchange is
    # called multiple times for the same session.
    agent_name = session_key.split(":")[1] if ":" in session_key else "main"
    usage_totals = _sum_usage(session_id, agent_name)
    if usage_totals:
        entry["usage"] = usage_totals

    index[session_key] = entry
    # Always write to sessionslist.json going forward.
    _write_index_atomic(sessions_dir / "sessionslist.json", index)
