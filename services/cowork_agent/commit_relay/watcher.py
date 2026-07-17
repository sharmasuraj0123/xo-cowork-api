"""Publish-side check for the commit relay: detect a remote-branch advance
(ls-remote), enumerate the new hashes, report {repo, workspace_id, commits} to
swarm. No loop lives here — `poller.run_tick` calls `run_tick_repo` for each
member repo inside the same tick that just refreshed membership, so the check
never runs on stale or missing knowledge. No fetch, no working-tree changes."""
from __future__ import annotations

import os

from . import git_ops, log_line, state, swarm_client


def _branch() -> str:
    return (os.getenv("RELAY_WATCH_BRANCH", "main") or "main").strip()


async def run_tick_repo(workspace_id: str, repo: str, repo_dir, branch: str) -> str:
    """One detect→report cycle for one repo. Returns the action for logging/tests:
    skip | baseline | noop | reported | report_failed."""
    remote = await git_ops.remote_head(repo_dir, branch)
    if remote is None:
        return "skip"
    last = state.load_last_reported(repo_dir)
    if last is None:
        state.save_last_reported(repo_dir, remote)   # baseline; never report history
        log_line(f"   relay: baseline {repo} @ {remote[:10]} (pushes before this are not relayed)")
        return "baseline"
    if remote == last:
        return "noop"
    if not await git_ops.commit_present(repo_dir, remote):
        # The branch moved but we don't have the objects yet (someone else's
        # push, seen only through ls-remote metadata). Never announce commits
        # you haven't seen: fetch first, then name every commit in the range —
        # otherwise the enumerate fallback reports just the tip and the middle
        # commits of a multi-commit push never reach the ledger.
        ok, err = await git_ops.fetch_origin(repo_dir)
        if not ok or not await git_ops.commit_present(repo_dir, remote):
            log_line(f"⚠️ relay: {repo} advanced but fetch failed before reporting — retrying next tick ({err or 'git fetch failed'})")
            return "skip"
    hashes = await git_ops.enumerate_hashes(repo_dir, last, remote)
    ok = await swarm_client.report_commits(repo, workspace_id, hashes)
    if not ok:
        log_line(f"⚠️ relay: report failed for {repo} ({len(hashes)} commit(s)) — retrying next tick")
        return "report_failed"                        # marker stays; retry next tick
    state.save_last_reported(repo_dir, remote)
    log_line(f"📤 relay: reported {len(hashes)} commit(s) for {repo} @ {remote[:10]}")
    return "reported"
