"""Runtime setup routes for the local Quirq installation."""

from __future__ import annotations

import asyncio
import os
import signal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.cowork_agent.runtime_config import (
    runtime_status,
    save_root_settings,
    save_settings,
)

router = APIRouter()


class RuntimeConfigRequest(BaseModel):
    agent_name: str
    watcher_enabled: bool
    watcher_interval_seconds: float
    watcher_source_mode: str


class RootConfigRequest(BaseModel):
    xo_projects_root: str
    quirq_state_root: str


@router.get("/api/runtime-config")
def get_runtime_config() -> dict:
    return runtime_status()


@router.put("/api/runtime-config")
def put_runtime_config(body: RuntimeConfigRequest) -> dict:
    try:
        saved = save_settings(body.model_dump())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "saved": saved,
        "status": runtime_status(),
    }


@router.put("/api/runtime-config/roots")
def put_runtime_roots(body: RootConfigRequest) -> dict:
    try:
        saved = save_root_settings(body.model_dump())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "saved": saved,
        "status": runtime_status(),
    }


async def _terminate_for_managed_restart() -> None:
    await asyncio.sleep(0.4)
    os.kill(os.getpid(), signal.SIGTERM)


@router.post("/api/runtime-config/restart")
async def restart_runtime() -> dict:
    status = runtime_status()
    if not status["restart_supported"] or not status["managed_container"]:
        raise HTTPException(
            status_code=409,
            detail=(
                "Automatic restart is available only in the installer-managed "
                "local Docker container."
            ),
        )
    asyncio.create_task(_terminate_for_managed_restart())
    return {"ok": True, "restarting": True}
