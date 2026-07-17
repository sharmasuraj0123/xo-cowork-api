"""Publish-side watcher. Per member repo: detect a remote-branch advance
(ls-remote), enumerate new hashes locally, report {repo, workspace_id, commits}
to swarm. Member-gated via the poller's membership (solo repos never report).
No fetch, no working-tree changes here."""
from __future__ import annotations

import asyncio
import logging
import os

from . import git_ops, poller, state, status, swarm_client

log = logging.getLogger(__name__)


def _log(msg: str) -> None:
    """print(flush=True) so relay activity shows in the service log file —
    module-level log.info is invisible under default logging config."""
    print(msg, flush=True)


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
        _log(f"   relay: baseline {repo} @ {remote[:10]} (pushes before this are not relayed)")
        return "baseline"
    if remote == last:
        return "noop"
    hashes = await git_ops.enumerate_hashes(repo_dir, last, remote)
    ok = await swarm_client.report_commits(repo, workspace_id, hashes)
    if not ok:
        _log(f"⚠️ relay: report failed for {repo} ({len(hashes)} commit(s)) — retrying next tick")
        return "report_failed"                        # marker stays; retry next tick
    state.save_last_reported(repo_dir, remote)
    _log(f"📤 relay: reported {len(hashes)} commit(s) for {repo} @ {remote[:10]}")
    return "reported"


async def start_commit_relay_watcher() -> None:
    """Background entry point. Follows the poller's cadence envs; only member
    repos (known from the last poll) are watched. Resilient until cancelled."""
    log.info("commit_relay watcher: started (branch=%s)", _branch())
    while True:
        try:
            ws = (os.getenv("PROJECT_ID", "") or "").strip() or None
            enabled = os.getenv("RELAY_ENABLED", "true").strip().lower() not in ("0", "false", "no")
            watched = 0
            if ws and enabled:
                repos = await poller.local_repo_map()
                member = status.member_repos()
                for repo, d in repos.items():
                    if repo in member:
                        watched += 1
                        await run_tick_repo(ws, repo, d, _branch())
            interval = poller._active() if watched else poller._dormant()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            log.warning("commit_relay watcher: tick error: %s", exc)
            interval = poller._dormant()
        await asyncio.sleep(poller._jitter(interval))
