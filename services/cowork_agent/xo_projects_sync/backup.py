"""
Backup orchestration — single-project and all-projects.

Glue layer over `tarball`, `crypto`, `github`, `manifest`, `config`.
Endpoints in `routers/cowork_agent/xo_projects_sync.py` call the
``backup_one`` / ``backup_all`` entry points; everything else here is
internal.

Concurrency: every public entry point acquires the module-level lock.
The staging clone is shared mutable state; two simultaneous backups
would race on commit/push, lose changes, or push conflicting commits.
v1 serializes; if higher throughput is ever required, splitting per
project_id is the natural next step.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from services.cowork_agent.project_layout import list_projects, project_dir, xo_projects_root

from . import crypto, github, manifest, tarball
from .config import MAX_VERSIONS_PER_PROJECT, SyncConfig


@dataclass
class BackupResult:
    project_id: str
    snapshot_id: str
    size_bytes: int
    sha256: str
    parts: int
    ok: bool = True
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_lock = asyncio.Lock()


# ── Public entry points ──────────────────────────────────────────────────────


async def backup_one(
    project_id: str,
    *,
    cfg: SyncConfig,
    auth: github.GitHubAuth,
    repo_url: str,
    note: str | None = None,
) -> BackupResult:
    """Back up a single xo-project. Holds the module lock for the duration."""
    async with _lock:
        return await _backup_one_locked(project_id, cfg=cfg, auth=auth, repo_url=repo_url, note=note)


async def backup_all(
    *,
    cfg: SyncConfig,
    auth: github.GitHubAuth,
    repo_url: str,
    note: str | None = None,
) -> list[BackupResult]:
    """Back up every xo-project on disk. One commit per project, single push at end.

    On a project-level error, records the error in that project's result and
    continues with the rest — caller can decide how to surface partial failures.
    """
    async with _lock:
        projects = [p["name"] for p in list_projects()]
        if not projects:
            return []
        results: list[BackupResult] = []
        staging = await github.ensure_clone(repo_url, auth=auth)
        for pid in projects:
            try:
                result = await _build_and_stage(pid, staging=staging, cfg=cfg, note=note)
                await _commit_project(pid, message=_commit_message(pid, result.snapshot_id, note),
                                      auth=auth, staging=staging)
                results.append(result)
            except Exception as exc:
                results.append(BackupResult(
                    project_id=pid, snapshot_id="", size_bytes=0, sha256="",
                    parts=0, ok=False, error=f"{type(exc).__name__}: {exc}",
                ))
        # Single push covers all per-project commits we just made.
        await github._git(["push", "origin", "HEAD"], cwd=staging, auth=auth)  # noqa: SLF001
        return results


# ── Internal: locked single-project path ─────────────────────────────────────


async def _backup_one_locked(
    project_id: str,
    *,
    cfg: SyncConfig,
    auth: github.GitHubAuth,
    repo_url: str,
    note: str | None,
) -> BackupResult:
    staging = await github.ensure_clone(repo_url, auth=auth)
    result = await _build_and_stage(project_id, staging=staging, cfg=cfg, note=note)
    pushed = await github.commit_and_push(
        _commit_message(project_id, result.snapshot_id, note),
        auth=auth,
        path=staging,
    )
    if not pushed:
        # Shouldn't happen — we just wrote new chunk files — but surface it
        # so silent no-ops don't masquerade as successes.
        result.ok = False
        result.error = "Nothing to commit; staging directory unchanged after backup write"
    return result


async def _build_and_stage(
    project_id: str,
    *,
    staging: Path,
    cfg: SyncConfig,
    note: str | None,
) -> BackupResult:
    """Build the encrypted snapshot for one project, write it into the staging clone.

    Does NOT commit/push — the caller decides how to batch commits.
    """
    src = project_dir(project_id)
    if not src.is_dir():
        raise FileNotFoundError(
            f"Project {project_id!r} not found at {src}. "
            f"Check xo-projects-root or scaffold it first."
        )
    assert cfg.passphrase is not None  # caller validated cfg.configured

    snapshot_id = manifest.utc_timestamp_id()
    project_dir_in_repo = staging / project_id
    snapshot_dir = project_dir_in_repo / snapshot_id
    snapshot_dir.mkdir(parents=True, exist_ok=False)

    # tar → gpg → split. Use a temp file outside the repo so the
    # plaintext tarball never lands inside the staging clone.
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / "snapshot.tar.gz"
        size_plain = await tarball.build_tarball(src, tar_path)
        parts = await crypto.encrypt_to_chunks(tar_path, snapshot_dir, cfg.passphrase)

    sha = manifest.sha256_files_concat(parts)
    encrypted_size = sum(p.stat().st_size for p in parts)
    mani = manifest.SnapshotManifest(
        project_id=project_id,
        snapshot_id=snapshot_id,
        created_at=manifest.utc_iso_now(),
        size_bytes=encrypted_size,
        sha256=sha,
        parts=[p.name for p in parts],
        gitignore_respected=True,
        note=note or None,
    )
    mani.write(snapshot_dir)

    _prune_old_snapshots(project_dir_in_repo)

    return BackupResult(
        project_id=project_id,
        snapshot_id=snapshot_id,
        size_bytes=encrypted_size,
        sha256=sha,
        parts=len(parts),
    )


async def _commit_project(project_id: str, *, message: str, auth: github.GitHubAuth, staging: Path) -> None:
    """Stage and commit only this project's subdir (no push)."""
    await github._git(["add", project_id], cwd=staging, auth=None)  # noqa: SLF001
    # Skip commit if nothing changed (shouldn't happen but defensive).
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(staging), "diff", "--cached", "--quiet",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode == 0:
        return
    await github._git(["commit", "-m", message], cwd=staging, auth=None)  # noqa: SLF001


def _commit_message(project_id: str, snapshot_id: str, note: str | None) -> str:
    base = f"Backup: {project_id} @ {snapshot_id}"
    if note:
        # Trim to a sensible single-line commit subject.
        clean = note.strip().replace("\n", " ")[:120]
        if clean:
            return f"{base} — {clean}"
    return base


def _prune_old_snapshots(project_dir_in_repo: Path) -> None:
    """Delete oldest timestamped subdirs so at most MAX_VERSIONS_PER_PROJECT remain."""
    if not project_dir_in_repo.is_dir():
        return
    timestamps = sorted(
        (d for d in project_dir_in_repo.iterdir() if d.is_dir() and _looks_like_timestamp(d.name)),
        key=lambda d: d.name,
    )
    excess = len(timestamps) - MAX_VERSIONS_PER_PROJECT
    if excess <= 0:
        return
    for old in timestamps[:excess]:
        shutil.rmtree(old, ignore_errors=True)


def _looks_like_timestamp(name: str) -> bool:
    # YYYYMMDD-HHMMSS — 15 chars total, digit-dash-digit pattern.
    return (
        len(name) == 15
        and name[:8].isdigit()
        and name[8] == "-"
        and name[9:].isdigit()
    )
