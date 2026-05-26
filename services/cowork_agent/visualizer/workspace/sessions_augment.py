"""``~/xo-projects/.xo/sessions/sessions-augment.json`` — union of
every project's per-project augment file.

Same schema; same key shape (composite session id or native session
id, depending on whether an adapter row exists at the project tier).
"""

from __future__ import annotations

from datetime import datetime, timezone

from services.cowork_agent.project_layout import workspace_xo_dir, xo_dir
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.workspace_index import list_project_ids


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply() -> bool:
    sessions: dict[str, dict] = {}
    for pid in list_project_ids():
        aug = read_json(xo_dir(pid) / "sessions" / "sessions-augment.json")
        if not isinstance(aug, dict):
            continue
        for key, row in (aug.get("sessions") or {}).items():
            if isinstance(row, dict):
                sessions[key] = row

    payload = {
        "schema": 2,
        "updated_at": _now_iso(),
        "sessions": sessions,
    }
    write_json_atomic(workspace_xo_dir() / "sessions" / "sessions-augment.json", payload)
    return True
