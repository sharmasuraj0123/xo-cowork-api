"""Git reads for the commit-relay watcher: remote head (ls-remote) and a local,
fetch-free enumeration of the new commits. Never raises to the caller."""
from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)


def _run(repo_dir, args, timeout=30):
    try:
        return subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as exc:
        log.debug("commit_relay git failed in %s: %s", repo_dir, exc)
        return None


def remote_head(repo_dir, branch: str) -> str | None:
    """Remote SHA of refs/heads/<branch> via ls-remote (metadata only, no download)."""
    r = _run(repo_dir, ["ls-remote", "origin", f"refs/heads/{branch}"])
    if r is None or r.returncode != 0:
        return None
    line = r.stdout.strip()
    return line.split()[0] if line else None


def enumerate_hashes(repo_dir, since_sha: str, head_sha: str) -> list[str]:
    """Hashes in since..head from LOCAL history — no fetch.

    Works when the workspace already has the objects (it pushed them). When the range
    can't be computed locally (head absent — e.g. a GitHub PR merge — or non-linear
    history), fall back to reporting just [head_sha]; the subscriber's fetch still
    pulls the full range.
    """
    r = _run(repo_dir, ["log", f"{since_sha}..{head_sha}", "--format=%H"]) if since_sha else None
    if r is not None and r.returncode == 0:
        hashes = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        if hashes:
            return hashes
    return [head_sha]
