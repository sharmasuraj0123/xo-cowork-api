"""Machine-local project registry — ``~/.xo/registry.json``.

The runtime tier keys every project's telemetry by ``project.json:pid`` and
stores it under ``~/.xo/<pid>/`` (see docs/xo-runtime-tier-restructure.md). This
module maintains the inverse map so the system can resolve in every direction it
needs without re-deriving lossy keys:

    pid → local_path / folder_id / runtime_dir / sessions

The registry is **runtime tier**: machine-local, never synced. Each machine
builds its own from the discovery pass the watcher already runs every tick
(``workspace_index.list_project_ids()`` + each folder's ``project.json``). It is
*not* load-bearing for correctness on the hot path — callers resolve pid through
``project_layout.runtime_key`` (reading ``project.json`` directly). The registry
exists for reverse lookups (folder/path → pid), debugging, and a future orphan-GC
pass keyed on which pids still have a backing folder.

This module names no agent — it is pure infrastructure.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.workspace_index import list_project_ids

logger = logging.getLogger(__name__)

SCHEMA = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _registry_path():
    return project_layout.xo_runtime_root() / "registry.json"


def build_registry() -> dict:
    """Rebuild ``~/.xo/registry.json`` from the projects on disk.

    Returns the payload that was written. Cheap to call every tick — it is a
    directory listing plus one small JSON read per project. A pid that maps to
    more than one folder (the "same project restored twice" edge case, doc 02
    §2.1) keeps a **list** of anchors rather than clobbering to one.
    """
    projects: dict[str, dict] = {}

    for name in list_project_ids():
        meta = project_layout.load_project(name)
        if not isinstance(meta, dict):
            continue
        pid = meta.get("pid")
        if not pid or meta.get("_template", False):
            # No stable identity yet — the watcher mints it on a later tick.
            continue
        pid = str(pid)

        anchor = {
            "folder_id": name,
            "local_path": str(project_layout.project_dir(name)),
        }
        existing = projects.get(pid)
        if existing is None:
            projects[pid] = {
                "anchors": [anchor],
                "runtime_dir": str(project_layout.runtime_dir(pid)),
            }
        else:
            # Same pid in two folders — keep both anchors.
            if anchor not in existing["anchors"]:
                existing["anchors"].append(anchor)

    payload = {
        "schema": SCHEMA,
        "updated_at": _now_iso(),
        "projects": projects,
    }
    try:
        write_json_atomic(_registry_path(), payload)
    except Exception:
        logger.exception("registry write failed (non-fatal)")
    return payload
