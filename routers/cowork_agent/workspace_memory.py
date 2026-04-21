"""
Workspace-memory endpoints.

Currently all stubbed; the frontend calls these for per-workspace persistent
memory features (add / list / update / delete / refresh / export).
"""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/api/workspace-memory")
def workspace_memory(workspace_path: str = ""):
    return {"memory": None}


@router.get("/api/workspace-memory/list")
def workspace_memory_list():
    return []


@router.put("/api/workspace-memory")
async def workspace_memory_update(request: Request):
    return {"ok": True}


@router.delete("/api/workspace-memory")
def workspace_memory_delete(workspace_path: str = ""):
    return {"ok": True}


@router.post("/api/workspace-memory/refresh")
def workspace_memory_refresh(workspace_path: str = ""):
    return {"ok": True}


@router.post("/api/workspace-memory/export")
def workspace_memory_export(workspace_path: str = ""):
    return {"ok": True}
