"""
Restore orchestration — single-project and all-projects.

Restore semantics:
- Without ``force=True``, refuse to overwrite an existing local project
  folder. The router turns the resulting ``ProjectExistsError`` into a
  409 with a clear suggestion.
- ``force=True`` deletes the existing folder and replaces it with the
  decrypted snapshot wholesale. No automatic pre-restore snapshot —
  the design treats "force" as a deliberate destructive action.

Bulk restore is independent-per-project: a 409 (or any other error) on
one project does NOT abort the rest. The result list carries one entry
per project with ``ok`` and an optional ``error`` field, so the caller
can surface partial failures cleanly.

Also reads the remote-side snapshot index for the `/projects` endpoint.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from services.cowork_agent.project_layout import project_dir

from . import crypto, github, manifest
from .config import SyncConfig


@dataclass
class RestoreResult:
    project_id: str
    restored_from: str | None
    target: str | None
    ok: bool = True
    error: str | None = None
    error_code: str | None = None  # "exists" | "not_found" | "verify_failed" | "decrypt_failed" | …

    def to_dict(self) -> dict:
        return asdict(self)


class ProjectExistsError(RuntimeError):
    """Local project folder already exists and ``force`` was not set."""


class SnapshotNotFoundError(RuntimeError):
    """The requested snapshot (or any snapshot for the project) is not on the remote."""


class ChecksumMismatchError(RuntimeError):
    """Concatenated parts' sha256 did not match the manifest. Abort before decrypt."""


_lock = asyncio.Lock()


# ── Public: listing ──────────────────────────────────────────────────────────


@dataclass
class SnapshotSummary:
    id: str
    created_at: str
    size_bytes: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProjectSummary:
    project_id: str
    snapshots: list[SnapshotSummary]

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "snapshots": [s.to_dict() for s in self.snapshots],
        }


async def list_remote_projects(
    *,
    auth: github.GitHubAuth,
    repo_url: str,
) -> list[ProjectSummary]:
    """Read the staging clone's project subdirs and list snapshots per project.

    Triggers a `git pull` to make sure we see snapshots pushed from
    elsewhere. Returns projects sorted alphabetically; snapshots within
    each project sorted newest-first.
    """
    async with _lock:
        staging = await github.ensure_clone(repo_url, auth=auth)
        out: list[ProjectSummary] = []
        for entry in sorted(staging.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            snapshots = _list_project_snapshots(entry)
            if not snapshots:
                continue
            out.append(ProjectSummary(project_id=entry.name, snapshots=snapshots))
        return out


def _list_project_snapshots(project_dir_in_repo: Path) -> list[SnapshotSummary]:
    """Inspect each timestamped subdir; return summaries newest-first.

    Folders without a valid manifest are silently skipped — they're
    either in-progress writes or corruption; either way they're not
    safe to advertise as restorable.
    """
    summaries: list[SnapshotSummary] = []
    for snap_dir in sorted(project_dir_in_repo.iterdir(), reverse=True):
        if not snap_dir.is_dir():
            continue
        try:
            mani = manifest.SnapshotManifest.read(snap_dir)
        except (FileNotFoundError, ValueError, KeyError):
            continue
        summaries.append(SnapshotSummary(
            id=mani.snapshot_id,
            created_at=mani.created_at,
            size_bytes=mani.size_bytes,
        ))
    return summaries


# ── Public: restore ──────────────────────────────────────────────────────────


async def restore_one(
    project_id: str,
    *,
    cfg: SyncConfig,
    auth: github.GitHubAuth,
    repo_url: str,
    snapshot_id: str | None = None,
    force: bool = False,
) -> RestoreResult:
    """Restore a single project. Acquires module lock for the duration."""
    async with _lock:
        staging = await github.ensure_clone(repo_url, auth=auth)
        return await _restore_one_locked(
            project_id, staging=staging, cfg=cfg,
            snapshot_id=snapshot_id, force=force,
        )


async def restore_all(
    *,
    cfg: SyncConfig,
    auth: github.GitHubAuth,
    repo_url: str,
    snapshot_id_map: dict[str, str] | None = None,
    force: bool = False,
) -> list[RestoreResult]:
    """Restore every project that has snapshots in the remote.

    ``snapshot_id_map`` lets the caller pin specific versions per project
    (e.g., for partial rollback); projects not in the map use the latest
    snapshot. ``force`` applies uniformly to all projects.
    """
    async with _lock:
        staging = await github.ensure_clone(repo_url, auth=auth)
        results: list[RestoreResult] = []
        snapshot_id_map = snapshot_id_map or {}
        for entry in sorted(staging.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            project_id = entry.name
            try:
                result = await _restore_one_locked(
                    project_id, staging=staging, cfg=cfg,
                    snapshot_id=snapshot_id_map.get(project_id),
                    force=force,
                )
                results.append(result)
            except Exception as exc:
                results.append(_error_result(project_id, exc))
        return results


# ── Internal: locked single-project path ─────────────────────────────────────


async def _restore_one_locked(
    project_id: str,
    *,
    staging: Path,
    cfg: SyncConfig,
    snapshot_id: str | None,
    force: bool,
) -> RestoreResult:
    """All restore logic runs here. Caller is responsible for the lock + pull."""
    assert cfg.passphrase is not None  # caller validated cfg.configured

    project_root = staging / project_id
    if not project_root.is_dir():
        raise SnapshotNotFoundError(f"No snapshots for project {project_id!r} in backup repo.")

    resolved_snapshot_id = snapshot_id or _newest_snapshot_id(project_root)
    if resolved_snapshot_id is None:
        raise SnapshotNotFoundError(
            f"Project {project_id!r} has no valid snapshots in backup repo."
        )

    snap_dir = project_root / resolved_snapshot_id
    if not snap_dir.is_dir():
        raise SnapshotNotFoundError(
            f"Snapshot {resolved_snapshot_id!r} not found for project {project_id!r}."
        )

    mani = manifest.SnapshotManifest.read(snap_dir)

    # Verify integrity BEFORE deciding to overwrite local state. A bad
    # snapshot must not block a working local copy.
    parts = [snap_dir / name for name in mani.parts]
    missing = [p for p in parts if not p.is_file()]
    if missing:
        raise SnapshotNotFoundError(
            f"Snapshot {resolved_snapshot_id} is missing parts: {[p.name for p in missing]}"
        )
    actual_sha = manifest.sha256_files_concat(parts)
    if actual_sha != mani.sha256:
        raise ChecksumMismatchError(
            f"Snapshot {resolved_snapshot_id} sha256 mismatch (manifest={mani.sha256[:12]} actual={actual_sha[:12]}). "
            "Possible corruption — refusing to decrypt."
        )

    target = project_dir(project_id)
    if target.exists():
        if not force:
            raise ProjectExistsError(
                f"Project folder already exists at {target}. "
                "Pass force=true to overwrite; existing local data will be lost."
            )
        # Will replace at the end — keep on disk until the new content is ready.

    # Decrypt + extract into a temp dir first, then atomic move into place.
    # Two reasons:
    # 1. If decrypt fails (wrong passphrase), the local project is untouched.
    # 2. If extract fails halfway, we don't leave a half-written project tree.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        plaintext_tar = tmp_root / "snapshot.tar.gz"
        try:
            await crypto.decrypt_from_chunks(parts, plaintext_tar, cfg.passphrase)
        except crypto.GpgFailedError as exc:
            raise RuntimeError(f"Snapshot decrypt failed (wrong passphrase?): {exc}")

        extracted = tmp_root / "extracted"
        # tarball.extract_tarball is sync; safe inside async because tarfile
        # releases the GIL on its underlying IO.
        from .tarball import extract_tarball  # local import to avoid cycle warnings
        extract_tarball(plaintext_tar, extracted)

        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), str(target))

    return RestoreResult(
        project_id=project_id,
        restored_from=resolved_snapshot_id,
        target=str(target),
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _newest_snapshot_id(project_root: Path) -> str | None:
    candidates = sorted(
        (d for d in project_root.iterdir() if d.is_dir()),
        key=lambda d: d.name,
        reverse=True,
    )
    for d in candidates:
        try:
            mani = manifest.SnapshotManifest.read(d)
            return mani.snapshot_id
        except (FileNotFoundError, ValueError, KeyError):
            continue
    return None


def _error_result(project_id: str, exc: BaseException) -> RestoreResult:
    code = {
        ProjectExistsError: "exists",
        SnapshotNotFoundError: "not_found",
        ChecksumMismatchError: "verify_failed",
    }.get(type(exc), "error")
    return RestoreResult(
        project_id=project_id,
        restored_from=None,
        target=None,
        ok=False,
        error=str(exc),
        error_code=code,
    )
