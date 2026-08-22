"""Machine-local union of every project's open sessions.

Stored at ``~/.quirq/watcher/activity/workspace.json``. Same schema
as each per-project presence snapshot; every ``open_sessions`` row
carries the activity schema's optional ``project_id`` field so the BFF
can group live sessions by project.
"""

from __future__ import annotations

from datetime import datetime, timezone

from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.state import (
    project_activity_path,
    workspace_activity_path,
)
from services.cowork_agent.visualizer.workspace_index import list_project_ids


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply() -> bool:
    open_sessions: list[dict] = []
    for pid in list_project_ids():
        act = read_json(project_activity_path(pid))
        if not isinstance(act, dict):
            continue
        for s in act.get("open_sessions") or []:
            if isinstance(s, dict):
                tagged = dict(s)
                tagged["project_id"] = pid
                open_sessions.append(tagged)

    payload = {
        "schema": 1,
        "updated_at": _now_iso(),
        "open_sessions": open_sessions,
    }
    write_json_atomic(workspace_activity_path(), payload)
    return True
