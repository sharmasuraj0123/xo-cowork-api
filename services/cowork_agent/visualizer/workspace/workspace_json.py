"""``~/xo-projects/.xo/workspace.json`` — workspace identity + project
discovery list.

Materialised by the watcher on every tick (cheap — small JSON,
single iterdir of the workspace root). Shape matches
``services/cowork_agent/project_template/.xo/schema/workspace.schema.json``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from services.cowork_agent.project_layout import (
    workspace_xo_dir,
    xo_dir,
    xo_projects_root,
)
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.workspace_index import list_project_ids


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _classification_rollup(projects: list[str]) -> dict | None:
    """Aggregate every project's persisted classification block (written by
    sinks/classification.py) into the workspace-level roll-up map. A manual
    top-level ``category`` in a project.json wins over its computed value."""
    # Lazy import: single source of truth for the category vocabulary
    # without pulling the tree walker into every watcher boot path.
    from services.cowork_agent.visualizer.environments_graph import (
        ENV_CATEGORIES,
        _TAG_ALIASES,
    )
    by_project: dict[str, dict] = {}
    clusters: dict[str, int] = {}
    for pid in projects:
        doc = read_json(xo_dir(pid) / "project.json") or {}
        block = doc.get("classification")
        if not isinstance(block, dict) or not block.get("category"):
            continue
        manual = str(doc.get("category") or "").strip().lower()
        manual = _TAG_ALIASES.get(manual, manual)
        # Alias the persisted category too: a block written before a taxonomy
        # change carries an old name (app/docs/wiki/customer).
        computed = _TAG_ALIASES.get(str(block["category"]), str(block["category"]))
        cat = manual if manual in ENV_CATEGORIES else computed
        by_project[pid] = {"category": cat, "ptype": block.get("ptype")}
        clusters[cat] = clusters.get(cat, 0) + 1
    if not by_project:
        return None
    return {"computed_at": _now_iso(), "by_project": by_project,
            "clusters": clusters}


def apply() -> bool:
    """Refresh ``workspace.json``. Returns ``True``."""
    wxo = workspace_xo_dir()
    projects = list_project_ids()
    payload = {
        "schema": 1,
        "updated_at": _now_iso(),
        "projects_root": str(xo_projects_root()),
        "projects": projects,
    }
    rollup = _classification_rollup(projects)
    if rollup is not None:
        payload["classification"] = rollup
    write_json_atomic(wxo / "workspace.json", payload)
    return True
