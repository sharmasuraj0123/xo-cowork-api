"""BFF routes for the commit relay — the Space UI's only relay surface.

Three job types: a window into the poller (status), local git reads (commit
feed), and proxies to swarm carrying the workspace's token + PROJECT_ID (share/
revoke/members). The browser never talks to swarm and never sees the token.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.cowork_agent.commit_relay import git_ops, status, swarm_client
from services.cowork_agent.commit_relay.repo_identity import normalize_repo
from services.cowork_agent.project_layout import project_dir, project_dir_exists

router = APIRouter()


class ShareBody(BaseModel):
    workspace_id: str


def _own_workspace_id() -> str | None:
    return (os.getenv("PROJECT_ID", "") or "").strip() or None


def _watch_branch() -> str:
    return (os.getenv("RELAY_WATCH_BRANCH", "main") or "main").strip()


async def _repo_for(project_id: str) -> str:
    """Resolve a project name to its normalized repo identity, or raise."""
    if not project_dir_exists(project_id):
        raise HTTPException(status_code=404, detail={
            "code": "project_not_found", "message": "Project not found."})
    repo = normalize_repo(await git_ops.origin_url(project_dir(project_id)))
    if repo is None:
        raise HTTPException(status_code=404, detail={
            "code": "no_git_origin",
            "message": "This project has no git origin — nothing to share or sync."})
    return repo


def _require_workspace_id() -> str:
    ws = _own_workspace_id()
    if not ws:
        raise HTTPException(status_code=409, detail={
            "code": "workspace_unconfigured",
            "message": "This workspace has no PROJECT_ID configured; sharing is disabled."})
    return ws


@router.get("/api/relay/status")
def relay_status() -> dict:
    snap = status.snapshot()
    snap["own_workspace_id"] = _own_workspace_id()
    snap["watch_branch"] = _watch_branch()
    return snap


@router.get("/api/xo-projects/{project_id}/commits")
async def project_commits(project_id: str, limit: int = 20) -> dict:
    if not project_dir_exists(project_id):
        raise HTTPException(status_code=404, detail={
            "code": "project_not_found", "message": "Project not found."})
    d = project_dir(project_id)
    limit = max(1, min(int(limit), 50))
    commits, source = await git_ops.recent_commits(d, _watch_branch(), limit)
    behind = await git_ops.behind_count(d, _watch_branch())
    return {"project_id": project_id, "branch": _watch_branch(),
            "source": source, "behind": behind, "commits": commits}


@router.get("/api/xo-projects/{project_id}/members")
async def project_members(project_id: str) -> dict:
    repo = await _repo_for(project_id)
    ok, code, payload = await swarm_client.members(repo)
    if not ok:
        raise HTTPException(status_code=code if 400 <= code < 500 else 502,
                            detail={"code": "swarm_error", "message": str(payload)})
    return {"project_id": project_id, "repo": repo,
            "own_workspace_id": _own_workspace_id(),
            "members": payload.get("members", [])}


@router.post("/api/xo-projects/{project_id}/share")
async def share_project(project_id: str, body: ShareBody) -> dict:
    ws = _require_workspace_id()
    repo = await _repo_for(project_id)
    target = (body.workspace_id or "").strip()
    if not target:
        raise HTTPException(status_code=422, detail={
            "code": "missing_workspace_id", "message": "Enter the recipient's workspace id."})
    ok, code, detail = await swarm_client.share(repo, ws, target)
    if not ok:
        raise HTTPException(status_code=code if 400 <= code < 500 else 502,
                            detail={"code": "share_failed", "message": detail})
    return {"ok": True, "repo": repo}


@router.post("/api/xo-projects/{project_id}/revoke")
async def revoke_project(project_id: str, body: ShareBody) -> dict:
    _require_workspace_id()
    repo = await _repo_for(project_id)
    target = (body.workspace_id or "").strip()
    if not target:
        raise HTTPException(status_code=422, detail={
            "code": "missing_workspace_id", "message": "Enter the workspace id to revoke."})
    ok, code, detail = await swarm_client.revoke(repo, target)
    if not ok:
        raise HTTPException(status_code=code if 400 <= code < 500 else 502,
                            detail={"code": "revoke_failed", "message": detail})
    return {"ok": True, "repo": repo}
