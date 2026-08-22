"""Read-only, privacy-aware catalog of machine-local Quirq state.

The catalog powers the local Quirq view, opened from the Setup tab's header
(deep link ``#/quirq``). It deliberately reports structure and
operational summaries rather than serving arbitrary files: credential values,
native session contents, cursor paths, and symlink targets never leave the
machine-local state service.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.cowork_agent.local_state import quirq_state_dir
from services.cowork_agent.project_layout import xo_projects_root
from services.cowork_agent.registry.agent_env import load_env_entries
from services.cowork_agent.runtime_config import (
    configured_settings,
    effective_settings,
    root_settings,
)


_MAX_FILES = 500
_MAX_JSON_BYTES = 2 * 1024 * 1024
_SENSITIVE_NAMES = frozenset({"secrets.env"})

_PROJECT_OUTPUT_CONTRACT = (
    {
        "path": "project.json",
        "producer": "Watcher identity sink + project scaffold",
        "purpose": "Stable project id, display name, description, and creation time",
        "used_by": "Projects, Graph",
    },
    {
        "path": "sessions/sessionslist.json",
        "producer": "Runtime source adapter",
        "purpose": "Metadata-only index that maps native sessions to this project",
        "used_by": "Projects APIs, Graph",
    },
    {
        "path": "sessions/sessions-augment.json",
        "producer": "Watcher session sink",
        "purpose": "Derived message, tool, task, model, timing, and usage summaries",
        "used_by": "Project APIs, Graph",
    },
    {
        "path": "todos.json",
        "producer": "Watcher todo sink + Todo API",
        "purpose": "Per-session work items and their lifecycle state",
        "used_by": "Projects",
    },
    {
        "path": "stats.json",
        "producer": "Watcher statistics sink",
        "purpose": "Rolling usage, runtime, model, tool, and daily aggregates",
        "used_by": "Project analytics APIs",
    },
    {
        "path": "timeline.jsonl",
        "producer": "Watcher timeline sink",
        "purpose": "Append-only normalized history of sessions, files, tools, and tasks",
        "used_by": "Projects",
    },
)

_WORKSPACE_OUTPUT_CONTRACT = (
    {
        "path": "workspace.json",
        "producer": "Watcher workspace rollup",
        "purpose": "Workspace identity and the discovered project list",
        "used_by": "Projects, Graph",
    },
    {
        "path": "sessions/sessionslist.json",
        "producer": "Watcher workspace rollup",
        "purpose": "Union of every project session index",
        "used_by": "Workspace APIs, Graph",
    },
    {
        "path": "sessions/sessions-augment.json",
        "producer": "Watcher workspace rollup",
        "purpose": "Union of watcher-derived session summaries",
        "used_by": "Workspace APIs, Graph",
    },
    {
        "path": "stats.json",
        "producer": "Watcher workspace rollup",
        "purpose": "Aggregated statistics across every project",
        "used_by": "Workspace analytics APIs",
    },
    {
        "path": "timeline.jsonl",
        "producer": "Watcher workspace rollup",
        "purpose": "Multiplexed project timelines tagged with project id",
        "used_by": "Workspace timeline APIs",
    },
)


def _iso_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, TypeError):
        return None


def _description(relative_path: str, *, is_dir: bool) -> str:
    if is_dir:
        if relative_path == "watcher":
            return "Watcher cursors, locks, and live presence"
        if relative_path == "watcher/activity":
            return "Ephemeral activity snapshots"
        if relative_path == "watcher/activity/projects":
            return "Per-project live presence"
        return "Directory"
    name = Path(relative_path).name
    if name == "state.json":
        return "Installation and onboarding state"
    if name == "runtime.env":
        return "Non-secret runtime settings"
    if name == "roots.env":
        return "Host roots queued for the installer"
    if name == "secrets.env":
        return "Write-only credentials; values are masked"
    if name == "offsets.json":
        return "Watcher read cursors; source paths are hidden"
    if relative_path == "watcher/activity/workspace.json":
        return "Workspace-wide live presence"
    if relative_path.startswith("watcher/activity/projects/"):
        return "Project live presence"
    return "Machine-local state file"


def _tree(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    file_count = 0
    directory_count = 0
    total_bytes = 0
    truncated = False
    if not root.is_dir():
        return items, {
            "files": 0,
            "directories": 0,
            "bytes": 0,
            "truncated": False,
        }

    try:
        paths = sorted(root.rglob("*"), key=lambda path: str(path).lower())
    except OSError:
        paths = []
    for path in paths:
        if len(items) >= _MAX_FILES:
            truncated = True
            break
        try:
            if path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            stat = path.stat()
            is_dir = path.is_dir()
        except (OSError, ValueError):
            continue
        size = 0 if is_dir else stat.st_size
        if is_dir:
            directory_count += 1
        else:
            file_count += 1
            total_bytes += size
        items.append(
            {
                "path": relative,
                "name": path.name,
                "depth": len(Path(relative).parts) - 1,
                "kind": "directory" if is_dir else "file",
                "size_bytes": size,
                "modified_at": _iso_time(stat.st_mtime),
                "sensitive": path.name in _SENSITIVE_NAMES,
                "description": _description(relative, is_dir=is_dir),
            }
        )
    return items, {
        "files": file_count,
        "directories": directory_count,
        "bytes": total_bytes,
        "truncated": truncated,
    }


def _activity(root: Path) -> dict[str, Any]:
    activity_root = root / "watcher" / "activity"
    workspace = _read_json(activity_root / "workspace.json")
    workspace_sessions = (
        workspace.get("open_sessions", [])
        if isinstance(workspace, dict)
        else []
    )
    projects: list[dict[str, Any]] = []
    projects_root = activity_root / "projects"
    try:
        project_files = sorted(projects_root.glob("*.json"))
    except OSError:
        project_files = []
    for path in project_files[:_MAX_FILES]:
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        sessions = data.get("open_sessions")
        sessions = sessions if isinstance(sessions, list) else []
        runtimes = sorted(
            {
                str(row.get("runtime"))
                for row in sessions
                if isinstance(row, dict) and row.get("runtime")
            }
        )
        projects.append(
            {
                "project_id": path.stem,
                "open_sessions": len(sessions),
                "runtimes": runtimes,
                "updated_at": data.get("updated_at"),
            }
        )
    return {
        "workspace_open_sessions": len(workspace_sessions),
        "workspace_updated_at": (
            workspace.get("updated_at") if isinstance(workspace, dict) else None
        ),
        "projects": projects,
    }


def _watcher(root: Path) -> dict[str, Any]:
    offsets = _read_json(root / "watcher" / "offsets.json")
    if isinstance(offsets, dict):
        tracked_files = len(offsets)
    elif isinstance(offsets, list):
        tracked_files = len(offsets)
    else:
        tracked_files = 0
    configured = configured_settings()
    applied = effective_settings()
    return {
        "enabled": applied["watcher_enabled"],
        "interval_seconds": applied["watcher_interval_seconds"],
        "source_mode": applied["watcher_source_mode"],
        "configured_enabled": configured["watcher_enabled"],
        "tracked_files": tracked_files,
        "offsets_present": (root / "watcher" / "offsets.json").is_file(),
    }


def _install_state(root: Path) -> dict[str, Any]:
    raw = _read_json(root / "state.json")
    if not isinstance(raw, dict):
        return {"present": False}
    return {
        "present": True,
        "onboarding_completed": bool(raw.get("onboarding_completed")),
        "onboarding_completed_at": raw.get("onboarding_completed_at"),
    }


def _contract_status(
    xo_dirs: list[Path],
    contract: tuple[dict[str, str], ...],
    *,
    path_prefix: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for definition in contract:
        present = 0
        total_bytes = 0
        latest_mtime = 0.0
        for xo_dir in xo_dirs:
            path = xo_dir / definition["path"]
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                stat = path.stat()
            except OSError:
                continue
            present += 1
            total_bytes += stat.st_size
            latest_mtime = max(latest_mtime, stat.st_mtime)
        rows.append(
            {
                **definition,
                "location": f"{path_prefix}/{definition['path']}",
                "present_count": present,
                "bytes": total_bytes,
                "updated_at": _iso_time(latest_mtime) if latest_mtime else None,
            }
        )
    return rows


def _project_outputs() -> dict[str, Any]:
    # Same root helper as every other tab — see project_layout.
    projects_root = xo_projects_root()
    host_root = (
        os.getenv("QUIRQ_HOST_PROJECTS_ROOT", "") or ""
    ).strip()
    project_dirs: list[tuple[str, Path]] = []
    if projects_root.is_dir():
        try:
            candidates = sorted(
                projects_root.iterdir(),
                key=lambda path: path.name.lower(),
            )
        except OSError:
            candidates = []
        for candidate in candidates:
            if candidate.name.startswith(".") or candidate.is_symlink():
                continue
            try:
                xo_dir = candidate / ".xo"
                if candidate.is_dir() and xo_dir.is_dir() and not xo_dir.is_symlink():
                    project_dirs.append((candidate.name, xo_dir))
            except OSError:
                continue
            if len(project_dirs) >= 200:
                break

    xo_dirs = [xo_dir for _, xo_dir in project_dirs]
    projects: list[dict[str, Any]] = []
    for project_id, xo_dir in project_dirs:
        files = []
        total_bytes = 0
        latest_mtime = 0.0
        for definition in _PROJECT_OUTPUT_CONTRACT:
            path = xo_dir / definition["path"]
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                stat = path.stat()
            except OSError:
                continue
            files.append(definition["path"])
            total_bytes += stat.st_size
            latest_mtime = max(latest_mtime, stat.st_mtime)
        legacy_activity = (xo_dir / "activity.json").is_file()
        projects.append(
            {
                "project_id": project_id,
                "container_path": str(xo_dir),
                "host_path": (
                    str(Path(host_root) / project_id / ".xo")
                    if host_root
                    else ""
                ),
                "watcher_files": files,
                "watcher_file_count": len(files),
                "bytes": total_bytes,
                "updated_at": _iso_time(latest_mtime) if latest_mtime else None,
                "legacy_activity_file": legacy_activity,
            }
        )

    workspace_xo = projects_root / ".xo"
    workspace_dirs = [workspace_xo] if workspace_xo.is_dir() else []
    legacy_count = sum(
        1 for row in projects if row["legacy_activity_file"]
    ) + int((workspace_xo / "activity.json").is_file())
    return {
        "root": {
            "container_path": str(projects_root),
            "host_path": host_root,
            "exists": projects_root.exists(),
            "readable": projects_root.exists() and os.access(projects_root, os.R_OK),
        },
        "project_count": len(projects),
        "projects": projects,
        "project_contract": _contract_status(
            xo_dirs,
            _PROJECT_OUTPUT_CONTRACT,
            path_prefix="<project>/.xo",
        ),
        "workspace_contract": _contract_status(
            workspace_dirs,
            _WORKSPACE_OUTPUT_CONTRACT,
            path_prefix="<XO root>/.xo",
        ),
        "legacy_activity_files": legacy_count,
        "legacy_activity_note": (
            ".xo/activity.json is legacy. Current presence is written only "
            "under .quirq/watcher/activity."
        ),
    }


def quirq_catalog() -> dict[str, Any]:
    root = quirq_state_dir()
    tree, totals = _tree(root)
    credentials = sorted(
        {
            str(entry.get("key") or "").strip()
            for entry in load_env_entries()
            if str(entry.get("key") or "").strip()
        }
    )
    roots = root_settings()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": {
            "container_path": str(root),
            "host_path": (
                os.getenv("QUIRQ_HOST_STATE_ROOT", "") or ""
            ).strip(),
            "exists": root.exists(),
            "readable": root.exists() and os.access(root, os.R_OK),
            "writable": root.exists() and os.access(root, os.W_OK),
        },
        "totals": totals,
        "tree": tree,
        "activity": _activity(root),
        "watcher": _watcher(root),
        "runtime": configured_settings(),
        "credentials": [
            {"key": key, "configured": True, "value": "••••••"}
            for key in credentials
        ],
        "install_state": _install_state(root),
        "root_change_required": roots["change_required"],
        "project_outputs": _project_outputs(),
    }
