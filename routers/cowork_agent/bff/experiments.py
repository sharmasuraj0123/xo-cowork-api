"""Intent-level API for the Space Experiment tab."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from services.cowork_agent.experiments.runtime import (
    ExperimentCapacityExceeded,
    ExperimentError,
    ExperimentNotFound,
    ExperimentNotReady,
    ExperimentTurnBusy,
    ExperimentUnavailable,
    MAX_MESSAGE_CHARS,
    experiment_manager,
)
from services.cowork_agent.project_layout import xo_projects_root

router = APIRouter()


class CreateExperimentRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=255)


class CreateExperimentTurnRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ExperimentMessageResponse(BaseModel):
    id: str
    role: str
    text: str
    status: str
    created_at: str
    updated_at: str


class ExperimentResponse(BaseModel):
    id: str
    project_id: str
    provider: str
    model: str
    status: str
    stage: str
    failed_stage: str | None
    output: str
    error: str | None
    agent_session_id: str | None
    sandbox_id: str | None
    space_url: str | None
    workspace_directory: str | None
    turn_status: str
    turn_error: str | None
    messages: list[ExperimentMessageResponse]
    expires_at: str | None
    created_at: str
    updated_at: str
    can_stop: bool
    can_message: bool


class CreateExperimentResponse(BaseModel):
    experiment: ExperimentResponse
    reused: bool


class ListExperimentsResponse(BaseModel):
    items: list[ExperimentResponse]


@router.get("/api/experiments/options")
async def experiment_options() -> dict[str, Any]:
    return await experiment_manager.options()


@router.get("/api/experiments", response_model=ListExperimentsResponse)
async def list_experiments() -> ListExperimentsResponse:
    return ListExperimentsResponse(items=await experiment_manager.list())


@router.post(
    "/api/experiments",
    response_model=CreateExperimentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_experiment(body: CreateExperimentRequest) -> CreateExperimentResponse:
    source_dir = _resolve_project_source(body.project_id)
    try:
        snapshot, reused = await experiment_manager.start(body.project_id, source_dir)
    except ExperimentCapacityExceeded as error:
        raise HTTPException(
            status_code=429,
            detail={"code": "experiment_capacity_reached", "message": str(error)},
        ) from error
    except ExperimentUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "experiment_provider_unavailable", "message": str(error)},
        ) from error
    return CreateExperimentResponse(experiment=ExperimentResponse(**snapshot), reused=reused)


@router.get("/api/experiments/{experiment_id}", response_model=ExperimentResponse)
async def get_experiment(experiment_id: str) -> ExperimentResponse:
    try:
        return ExperimentResponse(**(await experiment_manager.get(experiment_id)))
    except ExperimentNotFound as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "experiment_not_found", "message": str(error)},
        ) from error


@router.post("/api/experiments/{experiment_id}/stop", response_model=ExperimentResponse)
async def stop_experiment(experiment_id: str) -> ExperimentResponse:
    try:
        return ExperimentResponse(**(await experiment_manager.stop(experiment_id)))
    except ExperimentNotFound as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "experiment_not_found", "message": str(error)},
        ) from error


@router.post(
    "/api/experiments/{experiment_id}/turns",
    response_model=ExperimentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_experiment_turn(
    experiment_id: str,
    body: CreateExperimentTurnRequest,
) -> ExperimentResponse:
    try:
        return ExperimentResponse(
            **(await experiment_manager.start_turn(experiment_id, body.text))
        )
    except ExperimentNotFound as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "experiment_not_found", "message": str(error)},
        ) from error
    except (ExperimentNotReady, ExperimentTurnBusy) as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "experiment_not_ready", "message": str(error)},
        ) from error
    except ExperimentUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "experiment_provider_unavailable", "message": str(error)},
        ) from error
    except ExperimentError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_experiment_turn", "message": str(error)},
        ) from error


def _resolve_project_source(project_id: str) -> Path:
    if (
        project_id in {".", ".."}
        or project_id.startswith(".")
        or "/" in project_id
        or "\\" in project_id
        or "\x00" in project_id
        or any(ord(character) < 32 or ord(character) == 127 for character in project_id)
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_project_id", "message": "Project id is malformed."},
        )
    root = xo_projects_root().resolve()
    candidate = root / project_id
    source = candidate.resolve()
    if candidate.is_symlink() or source.parent != root or not source.is_dir():
        raise HTTPException(
            status_code=404,
            detail={"code": "project_not_found", "message": "Project not found."},
        )
    return source
