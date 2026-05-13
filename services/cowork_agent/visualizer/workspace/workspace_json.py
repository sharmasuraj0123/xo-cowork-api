"""``~/xo-projects/.xo/workspace.json`` — workspace identity + project
discovery list.

Materialised by the watcher on every tick (cheap — small JSON,
single iterdir of the workspace root). Shape matches
``services/cowork_agent/project_template/.xo/schema/workspace.schema.json``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from services.cowork_agent.project_layout import workspace_xo_dir, xo_projects_root
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.workspace_index import list_project_ids


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply() -> bool:
    """Refresh ``workspace.json``. Returns ``True``."""
    wxo = workspace_xo_dir()
    payload = {
        "schema": 1,
        "updated_at": _now_iso(),
        "projects_root": str(xo_projects_root()),
        "projects": list_project_ids(),
    }
    write_json_atomic(wxo / "workspace.json", payload)
    return True
