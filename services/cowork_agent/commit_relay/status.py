"""In-memory relay status for the UI (via GET /api/relay/status). Volatile by
design: restarts empty and repopulates on the first poll tick. Single-process
writer (the poller/watcher tasks); readers get plain-dict copies."""
from __future__ import annotations

import copy
from collections import deque
from datetime import datetime, timezone

_state: dict = {
    "enabled": True,
    "workspace_configured": False,
    "cadence": "parked",            # parked | dormant | active
    "last_poll_at": None,
    "last_poll_ok": None,
    "repos": {},                    # repo -> {project, shared, available,
                                    #          last_fetch_at, fetched, pending_github, last_error}
    "recent": deque(maxlen=50),     # [{at, repo, kind, detail}]
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo(repo: str) -> dict:
    return _state["repos"].setdefault(repo, {
        "project": None, "shared": False, "available": False,
        "last_fetch_at": None, "fetched": 0,
        "pending_github": False, "last_error": None,
    })


def _event(repo: str, kind: str, detail: str = "") -> None:
    _state["recent"].append({"at": _now(), "repo": repo, "kind": kind, "detail": detail})


def set_parked(enabled: bool, workspace_configured: bool) -> None:
    _state["enabled"] = enabled
    _state["workspace_configured"] = workspace_configured
    _state["cadence"] = "parked"


def record_poll(ok: bool, cadence: str = None, membership: set = None,
                local: dict = None) -> None:
    _state["last_poll_at"] = _now()
    _state["last_poll_ok"] = ok
    _state["enabled"] = True
    _state["workspace_configured"] = True
    if cadence:
        _state["cadence"] = cadence
    if membership is None:
        return
    for repo, project in (local or {}).items():
        r = _repo(repo)
        r["project"] = project
        r["shared"] = repo in membership
        if repo not in membership:
            r["available"] = False
    for repo in membership:
        r = _repo(repo)
        r["shared"] = True
        if repo in (local or {}):
            r["available"] = False


def record_fetch(repo: str, project: str, n: int) -> None:
    r = _repo(repo)
    r.update(project=project, last_fetch_at=_now(), fetched=r["fetched"] + n,
             pending_github=False, last_error=None)
    _event(repo, "fetch", f"{n} commit(s)")


def record_synced(repo: str, project: str) -> None:
    r = _repo(repo)
    r.update(project=project, pending_github=False, last_error=None)


def record_available(repo: str) -> None:
    _repo(repo)["available"] = True


def record_repo_error(repo: str, project, err: str, pending_github: bool = False) -> None:
    r = _repo(repo)
    if project:
        r["project"] = project
    r["last_error"] = err
    r["pending_github"] = pending_github
    _event(repo, "error", err)


def member_repos() -> set[str]:
    return {repo for repo, r in _state["repos"].items() if r.get("shared")}


def snapshot() -> dict:
    snap = copy.deepcopy({k: v for k, v in _state.items() if k != "recent"})
    snap["recent"] = list(_state["recent"])
    return snap
