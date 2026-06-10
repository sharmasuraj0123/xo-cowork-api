"""Commit-relay watcher loop. Per tick, per project: detect a remote-branch advance
(ls-remote), enumerate the new hashes locally (no fetch), and report them to swarm.
Fetching happens elsewhere — on broadcast receipt in the subscriber. No working-tree
changes here."""
from __future__ import annotations

import asyncio
import logging
import os

from services.cowork_agent.project_layout import list_projects, project_dir

from . import git_ops, state, swarm_client

log = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.getenv("RELAY_ENABLED", "").strip().lower() in ("1", "true", "yes")


def _branch() -> str:
    return (os.getenv("RELAY_WATCH_BRANCH", "main") or "main").strip()


def _interval() -> int:
    try:
        return int(os.getenv("RELAY_POLL_INTERVAL_SECONDS", "60"))
    except ValueError:
        return 60


async def run_tick(project_id: str, repo_dir, branch: str) -> str:
    """One detect→report cycle. Returns the action (for logging/tests):
    skip | baseline | noop | reported | report_failed."""
    remote = git_ops.remote_head(repo_dir, branch)
    if remote is None:
        return "skip"
    last = state.load_last_reported(repo_dir)
    if last is None:
        state.save_last_reported(repo_dir, remote)   # baseline; don't report history
        return "baseline"
    if remote == last:
        return "noop"
    hashes = git_ops.enumerate_hashes(repo_dir, last, remote)
    ok = await swarm_client.report_commits(project_id, hashes)
    if not ok:
        return "report_failed"                       # leave marker; retry next tick
    state.save_last_reported(repo_dir, remote)
    return "reported"


async def start_commit_relay_watcher() -> None:
    """Background entry point. Resilient loop until cancelled."""
    if not _enabled():
        log.info("commit_relay watcher: disabled (set RELAY_ENABLED=true)")
        return
    branch = _branch()
    interval = _interval()
    log.info("commit_relay watcher: started branch=%s interval=%ss", branch, interval)
    while True:
        try:
            for proj in list_projects():
                name = proj.get("name")
                if not name:
                    continue
                repo = project_dir(name)
                if not (repo / ".git").is_dir():
                    continue
                await run_tick(name, repo, branch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            log.warning("commit_relay watcher: tick error: %s", exc)
        await asyncio.sleep(interval)
