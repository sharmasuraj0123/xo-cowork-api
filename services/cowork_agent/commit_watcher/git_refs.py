"""Read local git refs via `git rev-parse`. Returns a SHA string or None."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _rev_parse(project_dir: Path, ref: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:  # git missing, dir gone, timeout
        log.debug("commit_watcher git rev-parse failed for %s: %s", ref, exc)
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def local_head(project_dir: Path, branch: str = "main") -> str | None:
    return _rev_parse(project_dir, f"refs/heads/{branch}")


def remote_tracking_head(project_dir: Path, branch: str = "main") -> str | None:
    return _rev_parse(project_dir, f"refs/remotes/origin/{branch}")
