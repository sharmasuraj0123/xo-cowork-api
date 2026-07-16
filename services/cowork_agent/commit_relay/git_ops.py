"""Async git plumbing for the commit relay. Every function is failure-tolerant:
git errors return None/False/empty — the relay must degrade, never crash."""
from __future__ import annotations

import asyncio

_SEP = "\x1f"  # unit separator: cannot appear in git subjects/authors


async def _run(repo_dir, *args) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo_dir), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def origin_url(repo_dir) -> str | None:
    code, out, _ = await _run(repo_dir, "config", "--get", "remote.origin.url")
    out = out.strip()
    return out if code == 0 and out else None


async def remote_head(repo_dir, branch: str) -> str | None:
    """SHA of origin's branch tip via ls-remote (metadata only, no download)."""
    code, out, _ = await _run(repo_dir, "ls-remote", "origin", f"refs/heads/{branch}")
    if code != 0:
        return None
    line = out.strip().splitlines()[0] if out.strip() else ""
    sha = line.split("\t")[0].strip() if line else ""
    return sha or None


async def enumerate_hashes(repo_dir, since_sha: str, head_sha: str) -> list[str]:
    """Hashes in since..head from LOCAL history (oldest first) — no fetch.

    Works when the workspace already has the objects (it pushed them). When the
    range can't be computed locally, fall back to reporting just [head_sha]; the
    subscribers' fetch still pulls the full range.
    """
    if since_sha:
        code, out, _ = await _run(repo_dir, "rev-list", "--reverse",
                                  f"{since_sha}..{head_sha}")
        if code == 0:
            hashes = [h.strip() for h in out.splitlines() if h.strip()]
            if hashes:
                return hashes
    return [head_sha]


async def fetch_origin(repo_dir) -> tuple[bool, str]:
    code, _, err = await _run(repo_dir, "fetch", "origin", "--quiet")
    return code == 0, err.strip()


async def commit_present(repo_dir, sha: str) -> bool:
    code, _, _ = await _run(repo_dir, "cat-file", "-e", f"{sha}^{{commit}}")
    return code == 0


async def recent_commits(repo_dir, branch: str, limit: int = 20) -> tuple[list[dict], str]:
    """Minimal feed: hash, subject, author, ISO date. Prefers the relay's view
    (origin/<branch>) so not-yet-merged commits show; falls back to HEAD."""
    fmt = f"--format=%H{_SEP}%s{_SEP}%an{_SEP}%cI"
    for source in (f"origin/{branch}", "HEAD"):
        code, out, _ = await _run(repo_dir, "log", source, f"-n{int(limit)}", fmt)
        if code != 0:
            continue
        commits = []
        for line in out.splitlines():
            parts = line.split(_SEP)
            if len(parts) == 4:
                commits.append({"hash": parts[0], "subject": parts[1],
                                "author": parts[2], "date": parts[3]})
        return commits, source
    return [], "none"


async def behind_count(repo_dir, branch: str) -> int | None:
    """Commits on origin/<branch> not yet in HEAD — the 'fetched, not applied' count."""
    code, out, _ = await _run(repo_dir, "rev-list", "--count", f"HEAD..origin/{branch}")
    if code != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None
