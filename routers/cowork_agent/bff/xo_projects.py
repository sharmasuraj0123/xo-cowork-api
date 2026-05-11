"""GET /api/xo-projects — BFF list of user projects.

Sits on top of services/cowork_agent/project_layout helpers. Strips the
``path`` field so the frontend never sees absolute filesystem
locations; merges scaffolded and unscaffolded directories into one
sorted list (newest first) and marks each entry with ``unscaffolded``
so the UI can prompt to complete setup.

See docs/bff-endpoints-design.md §9.1 for the full contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from routers.cowork_agent.bff.filters import (
    PROJECT_SYSTEM_LEAVES,
    is_hidden_name,
    is_root_only_hidden,
)
from services.cowork_agent import scopes
from services.cowork_agent.project_layout import (
    list_project_tree,
    list_projects,
    list_unscaffolded_dirs,
    project_dir_exists,
)

router = APIRouter()


class Project(BaseModel):
    id: str
    display_name: str
    description: Optional[str] = None
    created_at: Optional[str] = None
    unscaffolded: bool


class ListProjectsResponse(BaseModel):
    items: list[Project]
    total: int


def _to_iso_utc(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _shape_scaffolded(entry: dict) -> Project:
    name = str(entry.get("name") or "")
    display = entry.get("display_name") or name
    return Project(
        id=name,
        display_name=str(display),
        description=(entry.get("description") or None),
        created_at=(entry.get("created_at") or None),
        unscaffolded=False,
    )


def _shape_unscaffolded(entry: dict) -> Project:
    name = str(entry.get("name") or "")
    return Project(
        id=name,
        display_name=name,
        description=None,
        created_at=_to_iso_utc(entry.get("mtime")),
        unscaffolded=True,
    )


def _sort_newest_first(items: list[Project]) -> list[Project]:
    """Newest first by created_at; nulls last; alphabetical tiebreak."""
    with_ts = sorted(
        [p for p in items if p.created_at],
        key=lambda p: p.id,
    )
    with_ts.sort(key=lambda p: p.created_at or "", reverse=True)
    without_ts = sorted(
        [p for p in items if not p.created_at],
        key=lambda p: p.id,
    )
    return with_ts + without_ts


@router.get("/api/xo-projects", response_model=ListProjectsResponse)
def list_xo_projects() -> ListProjectsResponse:
    try:
        scopes.resolve_scope("xo-projects")  # validates scope exists
        scaffolded = list_projects()
        unscaffolded = list_unscaffolded_dirs()
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "scope_unavailable",
                "message": "Project directory is not readable.",
            },
        ) from exc

    items: list[Project] = []
    for entry in scaffolded:
        if (entry.get("name") or "") in PROJECT_SYSTEM_LEAVES:
            continue
        items.append(_shape_scaffolded(entry))
    for entry in unscaffolded:
        if (entry.get("name") or "") in PROJECT_SYSTEM_LEAVES:
            continue
        items.append(_shape_unscaffolded(entry))

    items = _sort_newest_first(items)
    return ListProjectsResponse(items=items, total=len(items))


# ── /api/xo-projects/{id}/tree ────────────────────────────────────────────────


class TreeEntry(BaseModel):
    name: str
    relative_path: str


class ProjectTreeResponse(BaseModel):
    project_id: str
    relative_path: str
    parent_relative_path: Optional[str] = None
    dirs: list[TreeEntry]
    files: list[TreeEntry]


def _filter_tree_entries(entries: list[dict], at_root: bool) -> list[TreeEntry]:
    out: list[TreeEntry] = []
    for e in entries:
        name = e.get("name") or ""
        if is_hidden_name(name):
            continue
        if at_root and is_root_only_hidden(name):
            continue
        out.append(TreeEntry(name=name, relative_path=e.get("relative_path") or ""))
    return out


@router.get("/api/xo-projects/{project_id}/tree", response_model=ProjectTreeResponse)
def project_tree(project_id: str, relative_path: str = "") -> ProjectTreeResponse:
    if not project_dir_exists(project_id):
        raise HTTPException(
            status_code=404,
            detail={"code": "project_not_found", "message": "Project not found."},
        )

    try:
        raw = list_project_tree(project_id, relative_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_relative_path",
                "message": "relative_path is malformed or escapes the project root.",
            },
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "scope_unavailable",
                "message": "Project directory is not readable.",
            },
        ) from exc

    if raw is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "directory_not_found", "message": "Directory not found in project."},
        )

    at_root = (raw["relative_path"] or "") == ""
    return ProjectTreeResponse(
        project_id=raw["project_id"],
        relative_path=raw["relative_path"],
        parent_relative_path=raw["parent_relative_path"],
        dirs=_filter_tree_entries(raw["dirs"], at_root),
        files=_filter_tree_entries(raw["files"], at_root),
    )
