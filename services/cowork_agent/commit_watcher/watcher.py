"""Commit watcher loop.

Started from server.py's lifespan. Each tick reads the project's local and
origin main SHAs, decides via decision.decide(), and reports this workspace's
own pushes to swarm. One workspace == one project (resolved from list_projects).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from services.cowork_agent.project_layout import list_projects, project_dir

from . import decision, git_refs, state, swarm_client

log = logging.getLogger(__name__)


def _poll_interval() -> int:
    try:
        return int(os.getenv("WATCH_POLL_INTERVAL_SECONDS", "60"))
    except ValueError:
        return 60


def _branch() -> str:
    return (os.getenv("WATCH_BRANCH", "main") or "main").strip()


def resolve_project_dir() -> Path | None:
    """The single xo-project's directory (one workspace == one project)."""
    projects = list_projects()
    if not projects:
        return None
    name = projects[0].get("name")
    if not name:
        return None
    if len(projects) > 1:
        log.warning("commit_watcher: %d projects found; watching %r", len(projects), name)
    return project_dir(name)


async def run_tick(project_id: str, pdir: Path, branch: str) -> str:
    """One observe-decide-act cycle. Returns the action taken (for logging/tests)."""
    local = git_refs.local_head(pdir, branch)
    origin = git_refs.remote_tracking_head(pdir, branch)
    if local is None or origin is None:
        return "skip"
    last = state.load_last_seen(pdir)
    action, sha = decision.decide(local, origin, last)
    if action in ("baseline", "fetch_update"):
        state.save_last_seen(pdir, sha)
    elif action == "report":
        ok = await swarm_client.report_change(project_id, sha)
        if ok:
            state.save_last_seen(pdir, sha)
    return action


async def start_commit_watcher() -> None:
    """Background entry point. Loops until cancelled; never dies on a tick error."""
    project_id = os.getenv("PROJECT_ID", "").strip()
    if not project_id:
        log.warning("commit_watcher: PROJECT_ID unset; watcher disabled")
        return
    interval = _poll_interval()
    branch = _branch()
    log.info("commit_watcher: started (project=%s branch=%s interval=%ss)",
             project_id, branch, interval)
    while True:
        try:
            pdir = resolve_project_dir()
            if pdir is not None:
                await run_tick(project_id, pdir, branch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("commit_watcher: tick error: %s", exc)
        await asyncio.sleep(interval)
