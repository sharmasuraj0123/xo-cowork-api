"""``~/xo-projects/.xo/activity.json`` — union of every project's
open sessions, tagged with ``project_id``.

Same schema as per-project ``activity.json``; each ``open_sessions``
row carries an extra ``project_id`` field (the schema's
``additionalProperties: false`` on the row would normally reject
that, but the workspace schema is a thin extension — see
docs/watcher-design.md §3.10).

Strictly, the workspace activity schema needs ``project_id`` in
``additionalProperties`` allowlist. For v1 we widen the activity
schema to allow the field; the BFF route already declares it.
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
    open_sessions: list[dict] = []
    for pid in list_project_ids():
        act = read_json(xo_dir(pid) / "activity.json")
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
    write_json_atomic(workspace_xo_dir() / "activity.json", payload)
    return True
