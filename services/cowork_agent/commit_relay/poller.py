"""Pull-based relay poller (replaces the SSE subscriber).

Per tick: enumerate local clones -> one POST /commits/poll -> git fetch repos
with events (cursor advances only after the commits are verifiably present).
Cadence: ACTIVE (~50s) when a shared repo is cloned here, DORMANT (~10min)
otherwise, PARKED (no network at all) when PROJECT_ID is missing or
RELAY_ENABLED=false. `has_more`/partial fetches trigger a short drain tick."""
from __future__ import annotations

import asyncio
import logging
import os
import random
from pathlib import Path

from services.cowork_agent.project_layout import git_repo_dirs

from . import git_ops, state, status, swarm_client
from .repo_identity import normalize_repo

log = logging.getLogger(__name__)

DRAIN_INTERVAL = 5.0


def _enabled() -> bool:
    return os.getenv("RELAY_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def _workspace_id() -> str | None:
    return (os.getenv("PROJECT_ID", "") or "").strip() or None


def _interval(name: str, default: int) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return float(default)


def _active() -> float:
    return _interval("RELAY_POLL_INTERVAL_ACTIVE_SECONDS", 50)


def _dormant() -> float:
    return _interval("RELAY_POLL_INTERVAL_DORMANT_SECONDS", 600)


def _jitter(seconds: float) -> float:
    try:
        ratio = float(os.getenv("RELAY_POLL_JITTER_RATIO", "0.2"))
    except ValueError:
        ratio = 0.2
    return max(1.0, seconds * (1.0 + random.uniform(-ratio, ratio)))


def decide_cadence(num_shared_cloned: int) -> str:
    return "active" if num_shared_cloned > 0 else "dormant"


async def local_repo_map() -> dict[str, Path]:
    """normalized origin -> clone dir. Two clones of one repo in a workspace is
    ambiguous: warn and skip that repo entirely."""
    out: dict[str, Path] = {}
    dupes: set[str] = set()
    for d in git_repo_dirs():
        repo = normalize_repo(await git_ops.origin_url(d))
        if repo is None:
            continue
        if repo in out:
            dupes.add(repo)
            continue
        out[repo] = d
    for repo in dupes:
        out.pop(repo, None)
        status.record_repo_error(repo, None,
                                 "two clones of this repo in one workspace — skipped")
        log.warning("commit_relay: %s cloned twice in this workspace; skipping", repo)
    return out


def _looks_like_auth_failure(err: str) -> bool:
    e = (err or "").lower()
    return any(s in e for s in ("authentication", "denied", "credential",
                                "could not read username", "403"))


async def run_tick() -> float:
    """One poll cycle. Returns seconds to sleep before the next tick."""
    if not _enabled() or not _workspace_id():
        status.set_parked(_enabled(), _workspace_id() is not None)
        return _jitter(_dormant())
    ws = _workspace_id()
    repos = await local_repo_map()
    cursors = {repo: state.load_cursor(d) for repo, d in repos.items()}
    resp = await swarm_client.poll(ws, cursors)
    if resp is None:
        status.record_poll(ok=False)
        had_shared = bool(status.member_repos() & set(repos))
        return _jitter(_active() if had_shared else _dormant())

    membership: set[str] = set()
    drain = False
    for entry in resp.get("repos") or []:
        repo = entry.get("repo")
        if not repo:
            continue
        membership.add(repo)
        d = repos.get(repo)
        if entry.get("available") or d is None:
            status.record_available(repo)
            continue
        events = entry.get("events") or []
        if not events:
            status.record_synced(repo, d.name)
        else:
            ok, err = await git_ops.fetch_origin(d)
            if not ok:
                status.record_repo_error(repo, d.name, err or "git fetch failed",
                                         pending_github=_looks_like_auth_failure(err))
            else:
                present = []
                for e in events:
                    if await git_ops.commit_present(d, e.get("commit", "")):
                        present.append(e)
                if present:
                    state.save_cursor(d, max(int(e.get("seq", 0)) for e in present))
                    status.record_fetch(repo, d.name, len(present))
                if len(present) < len(events):
                    drain = True     # rest retries next (short) tick
        if entry.get("has_more"):
            drain = True

    n_shared_cloned = len(membership & set(repos))
    cadence = decide_cadence(n_shared_cloned)
    status.record_poll(ok=True, cadence=cadence, membership=membership,
                       local={r: repos[r].name for r in repos})
    if drain:
        return DRAIN_INTERVAL
    return _jitter(_active() if cadence == "active" else _dormant())


async def run_relay_poller() -> None:
    """Background entry point. Resilient until cancelled."""
    log.info("commit_relay poller: started")
    while True:
        try:
            delay = await run_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            log.warning("commit_relay poller: tick error: %s", exc)
            delay = _jitter(_dormant())
        await asyncio.sleep(delay)
