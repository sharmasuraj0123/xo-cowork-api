"""
Relay router — lets a workspace announce a project commit to the relayer.

After pushing a project's shared GitHub repo, the caller hits ``POST /api/relay/ping``
with the ``project_id`` (and optionally the ``commit``; if omitted we resolve the
repo's current HEAD). The minimal ping is broadcast to every other subscribed
workspace via the relayer. Receiving + fetching is handled by the background
subscriber in ``services.cowork_agent.relay``.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.cowork_agent import relay as relay_svc
from services.cowork_agent.project_layout import xo_projects_root

router = APIRouter(prefix="/api/relay", tags=["relay"])


class PingBody(BaseModel):
    project_id: str
    commit: str | None = None  # if omitted, resolve HEAD of the project repo


@router.post("/ping")
async def ping(body: PingBody) -> dict:
    project_path = xo_projects_root() / body.project_id
    if not (project_path / ".git").is_dir():
        raise HTTPException(status_code=404, detail=f"no git repo at {project_path}")

    commit = (body.commit or "").strip()
    if not commit:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(project_path), "rev-parse", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail=f"could not resolve HEAD for {body.project_id}: {err.decode().strip()}",
            )
        commit = out.decode().strip()

    published = await relay_svc.ping_commit(body.project_id, commit)
    return {
        "ok": True,
        "project_id": body.project_id,
        "commit": commit,
        "published": published,
    }
