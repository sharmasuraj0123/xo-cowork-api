"""
xo-projects backup/restore router.

All endpoints under ``/api/xo-projects-sync``. The router is thin: it
maps HTTP requests to ``services.cowork_agent.xo_projects_sync``
functions, translates domain errors into the right HTTP status codes,
and never holds long-running logic itself.

Auth + config preconditions:
- Every endpoint except ``/setup`` requires ``SyncConfig.configured``
  (repo name + passphrase in env) AND a resolvable GitHub token.
  Failing either returns a structured 400/401 with the exact next step
  the user (or the agent on their behalf) needs to take.
- ``/setup`` is the bootstrap: it writes config into ``.env``, ensures
  the GitHub repo exists (gh CLI first, REST fallback), and refuses to
  succeed unless both halves landed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.cowork_agent.xo_projects_sync import backup as backup_mod
from services.cowork_agent.xo_projects_sync import config as cfg_mod
from services.cowork_agent.xo_projects_sync import crypto, github
from services.cowork_agent.xo_projects_sync import restore as restore_mod


log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/xo-projects-sync", tags=["xo-projects-sync"])


# ── Request bodies ───────────────────────────────────────────────────────────


class SetupBody(BaseModel):
    repo_name: str = Field(..., min_length=1, max_length=100,
                            description="GitHub repo name (no owner prefix). Created as private if missing.")
    passphrase: str = Field(..., min_length=1,
                             description="Symmetric encryption passphrase. Required to restore on any machine.")


class BackupBody(BaseModel):
    note: str | None = Field(None, max_length=500,
                              description="Optional short note appended to the commit message.")


class RestoreBody(BaseModel):
    snapshot_id: str | None = Field(None,
                                     description="YYYYMMDD-HHMMSS snapshot id; defaults to latest.")
    force: bool = Field(False,
                         description="Overwrite the local project folder if it exists. Destructive.")


class RestoreAllBody(BaseModel):
    snapshot_id_map: dict[str, str] | None = Field(None,
                                                    description="Per-project snapshot pins; missing projects use latest.")
    force: bool = False


# ── Auth/config helpers ──────────────────────────────────────────────────────


async def _require_config_and_auth() -> tuple[cfg_mod.SyncConfig, github.GitHubAuth, str, str]:
    """Common precondition for every operation post-setup.

    Returns (config, auth, owner, repo_url). Raises HTTPException with
    actionable detail when either side is missing.
    """
    cfg = cfg_mod.load_config()
    if not cfg.configured:
        raise HTTPException(400, detail={
            "error": "not_configured",
            "detail": "Backup is not configured.",
            "suggestion": "POST /api/xo-projects-sync/setup with {repo_name, passphrase} first.",
        })

    try:
        auth = await github.resolve_auth()
    except github.AuthMissingError as exc:
        raise HTTPException(401, detail={
            "error": "github_auth_missing",
            "detail": str(exc),
            "suggestion": (
                "Either complete the GitHub connector flow in xo-cowork UI, "
                f"or add {cfg_mod.ENV_GITHUB_PAT}=<your_token> to xo-cowork-api/.env."
            ),
        })

    try:
        owner = await github.discover_owner(auth)
    except github.GitHubAPIError as exc:
        raise HTTPException(exc.status, detail={
            "error": "github_identity_failed",
            "detail": exc.message,
        })

    assert cfg.repo_name is not None
    repo_url = f"https://github.com/{owner}/{cfg.repo_name}.git"
    return cfg, auth, owner, repo_url


# ── /setup ───────────────────────────────────────────────────────────────────


@router.post("/setup")
async def setup(body: SetupBody) -> JSONResponse:
    """Bootstrap config + ensure the GitHub repo exists.

    Steps:
      1. Verify GPG is installed (we'd fail at first backup otherwise).
      2. Resolve a GitHub token; fail 401 with a clear message if missing.
      3. Discover the owner.
      4. Persist repo name + passphrase into xo-cowork-api/.env and
         os.environ.
      5. Create the repo as private if it doesn't already exist (gh CLI
         first, REST fallback). Never asks the user to do it manually.
    """
    try:
        crypto.check_gpg_available()
    except crypto.GpgUnavailableError as exc:
        raise HTTPException(500, detail={"error": "gpg_missing", "detail": str(exc)})

    try:
        auth = await github.resolve_auth()
    except github.AuthMissingError as exc:
        raise HTTPException(401, detail={
            "error": "github_auth_missing",
            "detail": str(exc),
            "suggestion": (
                "Either complete the GitHub connector flow in xo-cowork UI, "
                f"or add {cfg_mod.ENV_GITHUB_PAT}=<your_token> to xo-cowork-api/.env."
            ),
        })

    try:
        owner = await github.discover_owner(auth)
    except github.GitHubAPIError as exc:
        raise HTTPException(exc.status, detail={
            "error": "github_identity_failed",
            "detail": exc.message,
        })

    # Persist BEFORE attempting repo creation. If create fails, the env
    # state still reflects the user's intent and they can retry without
    # re-entering the passphrase.
    repo_name = body.repo_name.strip()
    cfg_mod.upsert_env({
        cfg_mod.ENV_REPO_NAME: repo_name,
        cfg_mod.ENV_PASSPHRASE: body.passphrase,
    })

    try:
        already = await github.repo_exists(owner, repo_name, auth=auth)
        if already:
            repo_created = False
            clone_url = f"https://github.com/{owner}/{repo_name}.git"
        else:
            clone_url = await github.create_repo(owner, repo_name, auth=auth)
            repo_created = True
    except github.GitHubAPIError as exc:
        raise HTTPException(502, detail={
            "error": "repo_create_failed",
            "detail": exc.message,
            "suggestion": (
                "Token may lack `repo` scope. Regenerate the PAT with `repo` "
                "scope and re-run setup, or complete `gh auth login` via the UI."
            ),
        })

    return JSONResponse({
        "configured": True,
        "repo_owner": owner,
        "repo_name": repo_name,
        "repo_url": clone_url,
        "repo_created": repo_created,
        "token_source": auth.source,
    })


# ── /status ──────────────────────────────────────────────────────────────────


@router.get("/status")
async def status() -> JSONResponse:
    """Lightweight: report current config + token source. No GitHub network calls."""
    cfg = cfg_mod.load_config()
    token_source: str | None
    try:
        auth = await github.resolve_auth()
        token_source = auth.source
    except github.AuthMissingError:
        token_source = None
    try:
        crypto.check_gpg_available()
        gpg_ok = True
    except crypto.GpgUnavailableError:
        gpg_ok = False
    return JSONResponse({
        "configured": cfg.configured,
        "repo_name": cfg.repo_name,
        "token_source": token_source,
        "gpg_available": gpg_ok,
    })


# ── /projects ────────────────────────────────────────────────────────────────


@router.get("/projects")
async def list_projects_in_repo() -> JSONResponse:
    cfg, auth, _, repo_url = await _require_config_and_auth()
    try:
        projects = await restore_mod.list_remote_projects(auth=auth, repo_url=repo_url)
    except RuntimeError as exc:
        raise HTTPException(502, detail={"error": "git_failed", "detail": str(exc)})
    return JSONResponse([p.to_dict() for p in projects])


# ── /projects/{id} (backup one) ──────────────────────────────────────────────


@router.post("/projects/{project_id}")
async def backup_project(project_id: str, body: BackupBody | None = None) -> JSONResponse:
    cfg, auth, _, repo_url = await _require_config_and_auth()
    note = body.note if body else None
    try:
        result = await backup_mod.backup_one(
            project_id, cfg=cfg, auth=auth, repo_url=repo_url, note=note,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, detail={"error": "project_not_found", "detail": str(exc)})
    except RuntimeError as exc:
        raise HTTPException(502, detail={"error": "backup_failed", "detail": str(exc)})

    if not result.ok:
        raise HTTPException(500, detail={"error": "backup_failed", "detail": result.error})
    return JSONResponse(result.to_dict())


# ── /all (backup all) ────────────────────────────────────────────────────────


@router.post("/all")
async def backup_all_projects(body: BackupBody | None = None) -> JSONResponse:
    cfg, auth, _, repo_url = await _require_config_and_auth()
    note = body.note if body else None
    results = await backup_mod.backup_all(
        cfg=cfg, auth=auth, repo_url=repo_url, note=note,
    )
    return JSONResponse([r.to_dict() for r in results])


# ── /projects/{id}/restore ───────────────────────────────────────────────────


@router.post("/projects/{project_id}/restore")
async def restore_project(project_id: str, body: RestoreBody | None = None) -> JSONResponse:
    cfg, auth, _, repo_url = await _require_config_and_auth()
    snapshot_id = body.snapshot_id if body else None
    force = body.force if body else False
    try:
        result = await restore_mod.restore_one(
            project_id, cfg=cfg, auth=auth, repo_url=repo_url,
            snapshot_id=snapshot_id, force=force,
        )
    except restore_mod.ProjectExistsError as exc:
        raise HTTPException(409, detail={
            "error": "project_exists",
            "detail": str(exc),
            "suggestion": "Pass force=true in the body to overwrite; existing local data will be lost.",
        })
    except restore_mod.SnapshotNotFoundError as exc:
        raise HTTPException(404, detail={"error": "snapshot_not_found", "detail": str(exc)})
    except restore_mod.ChecksumMismatchError as exc:
        raise HTTPException(502, detail={"error": "verify_failed", "detail": str(exc)})
    except RuntimeError as exc:
        raise HTTPException(500, detail={"error": "restore_failed", "detail": str(exc)})
    return JSONResponse(result.to_dict())


# ── /all/restore ─────────────────────────────────────────────────────────────


@router.post("/all/restore")
async def restore_all_projects(body: RestoreAllBody | None = None) -> JSONResponse:
    cfg, auth, _, repo_url = await _require_config_and_auth()
    snapshot_id_map = body.snapshot_id_map if body else None
    force = body.force if body else False
    results = await restore_mod.restore_all(
        cfg=cfg, auth=auth, repo_url=repo_url,
        snapshot_id_map=snapshot_id_map, force=force,
    )
    return JSONResponse([r.to_dict() for r in results])
