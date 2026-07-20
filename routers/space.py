"""Space: the local workspace knowledge graph.

Serves the Space folder (graph UI + its data/space.json) as static files under
/space, plus a tiny control API the UI uses for its server on/off widget.

The folder location comes from SPACE_DIR (env), defaulting to the xo-atlas
folder in the ClaudeWorkspace. Data never leaves this machine: the UI reads
data/space.json from this mount. See <SPACE_DIR>/README.md for the format.
"""

import asyncio
import os
import signal
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from services.cowork_agent.visualizer.session_telemetry import (
    build_session_telemetry,
)
from services.cowork_agent.visualizer.sessions_graph import build_sessions_graph
from services.cowork_agent.visualizer.environments_graph import build_environments_graph
from services.cowork_agent.visualizer.commit_timeline import (
    build_project_commit_timeline,
    build_environment_commit_timeline,
)
from services.cowork_agent.visualizer.session_diffs import build_session_diffs
from services.cowork_agent.visualizer.xo_overview import (
    build_sessions_overview,
    build_xo_overview,
)
from services.cowork_agent.visualizer.space_index import build_space_data

# Bundled UI (space_ui/ at the repo root); SPACE_DIR env var overrides, e.g.
# to point at a live xo-atlas checkout during UI development.
DEFAULT_SPACE_DIR = str(Path(__file__).resolve().parent.parent / "space_ui")
SPACE_DIR = Path(os.getenv("SPACE_DIR", DEFAULT_SPACE_DIR)).expanduser()

router = APIRouter(prefix="/space", tags=["space"])


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


@router.get("/server/status")
async def space_server_status():
    """Lightweight status for the Space UI widget (also see /health)."""
    return {
        "status": "on",
        "pid": os.getpid(),
        "space_dir": str(SPACE_DIR),
        "space_dir_exists": SPACE_DIR.exists(),
    }


@router.post("/server/stop")
async def space_server_stop(request: Request):
    """Gracefully stop the server. Localhost only; restart via ./cowork-api.sh start."""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="stop is allowed from localhost only")

    async def _terminate_soon():
        await asyncio.sleep(0.4)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.get_running_loop().create_task(_terminate_soon())
    return {"status": "stopping", "restart": "./cowork-api.sh start"}


SPACE_CACHE_TTL = float(os.getenv("SPACE_CACHE_TTL", "30"))

# (built_at_monotonic, payload) — module-level; refreshed when older than TTL.
_data_cache: tuple[float, dict] | None = None


@router.get("/data/space.json")
async def space_data():
    """The Space graph, generated live from ~/xo-projects.

    Registered before the static mount (see server.py include order), so it
    shadows <SPACE_DIR>/data/space.json. Falls back to that file when the
    builder fails; 503 when there is no fallback either."""
    global _data_cache
    now = time.monotonic()
    if _data_cache is not None and now - _data_cache[0] < SPACE_CACHE_TTL:
        return JSONResponse(_data_cache[1], headers={"Cache-Control": "no-store"})

    try:
        data = build_space_data()
    except Exception as exc:
        print(f"⚠️ space_index failed ({exc}); falling back to static space.json")
        static = SPACE_DIR / "data" / "space.json"
        if static.is_file():
            return FileResponse(static, media_type="application/json",
                                headers={"Cache-Control": "no-store"})
        raise HTTPException(
            status_code=503,
            detail={"code": "projects_root_unavailable",
                    "message": "Could not build the graph and no static fallback exists."},
        )

    _data_cache = (now, data)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


# Multi-runtime session telemetry — second Space dataset. Same TTL, separate
# cache slot. Individual read-only providers resolve their own storage paths.
_argus_cache: tuple[float, dict] | None = None
_sessions_cache_lock: asyncio.Lock | None = None
_sessions_cache_lock_loop: asyncio.AbstractEventLoop | None = None
_sessions_build_task: asyncio.Task[dict] | None = None


def _get_sessions_cache_lock() -> asyncio.Lock:
    """Return the lock owned by the current app/test event loop."""
    global _sessions_cache_lock, _sessions_cache_lock_loop
    loop = asyncio.get_running_loop()
    if _sessions_cache_lock is None or _sessions_cache_lock_loop is not loop:
        _sessions_cache_lock = asyncio.Lock()
        _sessions_cache_lock_loop = loop
    return _sessions_cache_lock


async def _build_and_cache_sessions() -> dict:
    """Build once for every concurrent request wave and cache only success."""
    global _argus_cache
    try:
        # SQLite and rollout parsing are synchronous; keep a cold history scan
        # off FastAPI's event loop.
        data = await asyncio.to_thread(build_session_telemetry)
    except Exception as exc:
        # Full diagnostics stay server-side; callers receive a stable message.
        print(f"⚠️ session telemetry failed ({exc})")
        raise
    _argus_cache = (time.monotonic(), data)
    return data


def _clear_sessions_build_task(task: asyncio.Task[dict]) -> None:
    global _sessions_build_task
    if _sessions_build_task is task:
        _sessions_build_task = None


def _schedule_sessions_build_cleanup(task: asyncio.Task[dict]) -> None:
    # Retrieve a background failure even if the initiating request was
    # cancelled. Awaiters still receive the same exception from the task.
    if not task.cancelled():
        task.exception()
    # Defer cleanup one loop turn so requests already in the concurrent wave
    # can observe and await the same completed success or failure.
    task.get_loop().call_soon(_clear_sessions_build_task, task)


@router.get("/data/sessions.json")
async def sessions_data():
    """All available session-telemetry stats for the Space Sessions tab.

    Providers fail independently: one readable source still yields a useful
    200 response with source status metadata. No static fallback: a truthful
    503 when every provider is unavailable beats stale pretty numbers."""
    global _argus_cache, _sessions_build_task
    now = time.monotonic()
    if _argus_cache is not None and now - _argus_cache[0] < SPACE_CACHE_TTL:
        return JSONResponse(_argus_cache[1], headers={"Cache-Control": "no-store"})

    # Atomically reuse or create the one build shared by this concurrent wave.
    async with _get_sessions_cache_lock():
        now = time.monotonic()
        if _argus_cache is not None and now - _argus_cache[0] < SPACE_CACHE_TTL:
            return JSONResponse(
                _argus_cache[1], headers={"Cache-Control": "no-store"}
            )
        task = _sessions_build_task
        if task is not None and task.get_loop() is not asyncio.get_running_loop():
            # Defensive for test/app loop replacement; production uses one loop.
            task = None
            _sessions_build_task = None
        if task is None:
            task = asyncio.create_task(_build_and_cache_sessions())
            _sessions_build_task = task
            task.add_done_callback(_schedule_sessions_build_cleanup)

    try:
        # A disconnected caller must not cancel the shared build for its peers.
        data = await asyncio.shield(task)
    except Exception:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "argus_db_unavailable",
                "message": "Session telemetry is temporarily unavailable.",
            },
        )
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


# Sessions rendered as a Space graph — third dataset, for the Graph tab's
# Output/Sessions toggle. Same TTL, separate cache slot.
_sessions_graph_cache: tuple[float, dict] | None = None


@router.get("/data/sessions_graph.json")
async def sessions_graph_data():
    """Session telemetry projected into the Space graph schema.

    Same shape as /space/data/space.json, so the graph renderer (and its
    Timeline and Six Degrees lenses) consume it unchanged. No static
    fallback: a truthful 503 beats a stale map."""
    global _sessions_graph_cache
    now = time.monotonic()
    if _sessions_graph_cache is not None and now - _sessions_graph_cache[0] < SPACE_CACHE_TTL:
        return JSONResponse(_sessions_graph_cache[1],
                            headers={"Cache-Control": "no-store"})

    try:
        # Telemetry providers read SQLite and rollout files synchronously;
        # keep the scan off the event loop.
        data = await asyncio.to_thread(build_sessions_graph)
    except Exception as exc:
        print(f"⚠️ sessions_graph failed ({exc})")
        raise HTTPException(
            status_code=503,
            detail={"code": "argus_db_unavailable",
                    "message": "Session telemetry is temporarily unavailable."},
        )

    _sessions_graph_cache = (now, data)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


# Environments — fourth dataset, for the space switcher's third entry. The
# workspace's projects clustered into 5 fixed business-purpose hubs (app/
# ops/wiki/marketing/customer) instead of one hub per project. Same TTL,
# separate cache slot.
_environments_graph_cache: tuple[float, dict] | None = None


@router.get("/data/environments_graph.json")
async def environments_graph_data():
    """The workspace's projects classified into 5 business-purpose clusters.

    Same shape as /space/data/space.json, so the graph renderer (and its
    Timeline and Six Degrees lenses) consume it unchanged. No static
    fallback: a truthful 503 beats a stale map."""
    global _environments_graph_cache
    now = time.monotonic()
    if _environments_graph_cache is not None and now - _environments_graph_cache[0] < SPACE_CACHE_TTL:
        return JSONResponse(_environments_graph_cache[1],
                            headers={"Cache-Control": "no-store"})

    try:
        # Filesystem walk + git log per project; keep it off the event loop.
        data = await asyncio.to_thread(build_environments_graph)
    except Exception as exc:
        print(f"⚠️ environments_graph failed ({exc})")
        raise HTTPException(
            status_code=503,
            detail={"code": "projects_root_unavailable",
                    "message": "Could not build the environments graph."},
        )

    _environments_graph_cache = (now, data)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


# Workspace .xo/ state for the Overview tab. Read-only (the watcher service
# owns .xo/); same TTL, separate cache slot. Builds are single-flight: a slow
# tree walk must not fan out one duplicate build per polling client.
_overview_cache: tuple[float, dict] | None = None
_overview_lock: asyncio.Lock | None = None
_overview_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_overview_lock() -> asyncio.Lock:
    global _overview_lock, _overview_lock_loop
    loop = asyncio.get_running_loop()
    if _overview_lock is None or _overview_lock_loop is not loop:
        _overview_lock = asyncio.Lock()
        _overview_lock_loop = loop
    return _overview_lock


@router.get("/data/overview.json")
async def overview_data():
    """The workspace's .xo/ state (manifest, stats, activity, timeline)."""
    global _overview_cache
    now = time.monotonic()
    if _overview_cache is not None and now - _overview_cache[0] < SPACE_CACHE_TTL:
        return JSONResponse(_overview_cache[1], headers={"Cache-Control": "no-store"})
    async with _get_overview_lock():
        now = time.monotonic()
        if _overview_cache is not None and now - _overview_cache[0] < SPACE_CACHE_TTL:
            return JSONResponse(_overview_cache[1], headers={"Cache-Control": "no-store"})
        try:
            data = await asyncio.to_thread(build_xo_overview)
        except Exception as exc:
            print(f"⚠️ xo_overview failed ({exc})")
            raise HTTPException(
                status_code=503,
                detail={"code": "xo_state_unavailable",
                        "message": "The workspace's .xo state is not readable."},
            )
        # Stamp after the build so a slow walk still gets a full TTL of reuse.
        _overview_cache = (time.monotonic(), data)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


# Sessions-space Overview: every runtime's on-disk session data (trees +
# store metadata), aggregated per adapter capability. Same lock+TTL shape.
_sess_overview_cache: tuple[float, dict] | None = None
_sess_overview_lock: asyncio.Lock | None = None
_sess_overview_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_sess_overview_lock() -> asyncio.Lock:
    global _sess_overview_lock, _sess_overview_lock_loop
    loop = asyncio.get_running_loop()
    if _sess_overview_lock is None or _sess_overview_lock_loop is not loop:
        _sess_overview_lock = asyncio.Lock()
        _sess_overview_lock_loop = loop
    return _sess_overview_lock


@router.get("/data/overview_sessions.json")
async def overview_sessions_data():
    """Runtime session-data stores for the Overview tab's Sessions space."""
    global _sess_overview_cache
    now = time.monotonic()
    if _sess_overview_cache is not None and now - _sess_overview_cache[0] < SPACE_CACHE_TTL:
        return JSONResponse(_sess_overview_cache[1], headers={"Cache-Control": "no-store"})
    async with _get_sess_overview_lock():
        now = time.monotonic()
        if _sess_overview_cache is not None and now - _sess_overview_cache[0] < SPACE_CACHE_TTL:
            return JSONResponse(_sess_overview_cache[1],
                                headers={"Cache-Control": "no-store"})
        try:
            data = await asyncio.to_thread(build_sessions_overview)
        except Exception as exc:
            print(f"⚠️ sessions overview failed ({exc})")
            raise HTTPException(
                status_code=503,
                detail={"code": "session_data_unavailable",
                        "message": "No runtime session data is readable."},
            )
        _sess_overview_cache = (time.monotonic(), data)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


# Commit timeline — one entry per commit, across every project and its
# in-repo worktrees. Powers the Projects-space Timeline's light-cone view.
_commits_cache: tuple[float, dict] | None = None
_commits_lock: asyncio.Lock | None = None
_commits_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_commits_lock() -> asyncio.Lock:
    global _commits_lock, _commits_lock_loop
    loop = asyncio.get_running_loop()
    if _commits_lock is None or _commits_lock_loop is not loop:
        _commits_lock = asyncio.Lock()
        _commits_lock_loop = loop
    return _commits_lock


@router.get("/data/commits.json")
async def commits_data():
    """Git commit history (projects + worktrees) as timeline events."""
    global _commits_cache
    now = time.monotonic()
    if _commits_cache is not None and now - _commits_cache[0] < SPACE_CACHE_TTL:
        return JSONResponse(_commits_cache[1], headers={"Cache-Control": "no-store"})
    async with _get_commits_lock():
        now = time.monotonic()
        if _commits_cache is not None and now - _commits_cache[0] < SPACE_CACHE_TTL:
            return JSONResponse(_commits_cache[1], headers={"Cache-Control": "no-store"})
        try:
            # Many `git log` subprocess calls; keep them off the event loop.
            data = await asyncio.to_thread(build_project_commit_timeline)
        except Exception as exc:
            print(f"⚠️ commit_timeline failed ({exc})")
            raise HTTPException(
                status_code=503,
                detail={"code": "projects_root_unavailable",
                        "message": "Could not build the commit timeline."},
            )
        _commits_cache = (time.monotonic(), data)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


# Environment commit timeline — commits.json's events tagged with which of
# the 5 Environments clusters each project belongs to. Powers the Growth
# Trunk view: one trunk per cluster, not per project.
_env_commits_cache: tuple[float, dict] | None = None
_env_commits_lock: asyncio.Lock | None = None
_env_commits_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_env_commits_lock() -> asyncio.Lock:
    global _env_commits_lock, _env_commits_lock_loop
    loop = asyncio.get_running_loop()
    if _env_commits_lock is None or _env_commits_lock_loop is not loop:
        _env_commits_lock = asyncio.Lock()
        _env_commits_lock_loop = loop
    return _env_commits_lock


@router.get("/data/environment_commits.json")
async def environment_commits_data():
    """Commit history tagged by Environments cluster (app/ops/wiki/docs/customer)."""
    global _env_commits_cache
    now = time.monotonic()
    if _env_commits_cache is not None and now - _env_commits_cache[0] < SPACE_CACHE_TTL:
        return JSONResponse(_env_commits_cache[1], headers={"Cache-Control": "no-store"})
    async with _get_env_commits_lock():
        now = time.monotonic()
        if _env_commits_cache is not None and now - _env_commits_cache[0] < SPACE_CACHE_TTL:
            return JSONResponse(_env_commits_cache[1], headers={"Cache-Control": "no-store"})
        try:
            # git log subprocess calls + a full per-project classification
            # walk; keep off the event loop.
            data = await asyncio.to_thread(build_environment_commit_timeline)
        except Exception as exc:
            print(f"⚠️ environment_commit_timeline failed ({exc})")
            raise HTTPException(
                status_code=503,
                detail={"code": "projects_root_unavailable",
                        "message": "Could not build the environment commit timeline."},
            )
        _env_commits_cache = (time.monotonic(), data)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


# Session diffs — the Sessions-space Timeline's equivalent of commits.json,
# derived from agent Edit/Write tool calls in each runtime's transcripts.
_session_diffs_cache: tuple[float, dict] | None = None
_session_diffs_lock: asyncio.Lock | None = None
_session_diffs_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_session_diffs_lock() -> asyncio.Lock:
    global _session_diffs_lock, _session_diffs_lock_loop
    loop = asyncio.get_running_loop()
    if _session_diffs_lock is None or _session_diffs_lock_loop is not loop:
        _session_diffs_lock = asyncio.Lock()
        _session_diffs_lock_loop = loop
    return _session_diffs_lock


@router.get("/data/session_diffs.json")
async def session_diffs_data():
    """Agent-derived code diffs (Edit/Write tool calls) as timeline events."""
    global _session_diffs_cache
    now = time.monotonic()
    if _session_diffs_cache is not None and now - _session_diffs_cache[0] < SPACE_CACHE_TTL:
        return JSONResponse(_session_diffs_cache[1], headers={"Cache-Control": "no-store"})
    async with _get_session_diffs_lock():
        now = time.monotonic()
        if _session_diffs_cache is not None and now - _session_diffs_cache[0] < SPACE_CACHE_TTL:
            return JSONResponse(_session_diffs_cache[1], headers={"Cache-Control": "no-store"})
        try:
            # Transcript scans are synchronous file I/O; keep off the event loop.
            data = await asyncio.to_thread(build_session_diffs)
        except Exception as exc:
            print(f"⚠️ session_diffs failed ({exc})")
            raise HTTPException(
                status_code=503,
                detail={"code": "commit_diffs_unavailable",
                        "message": "No runtime commit-diff data is readable."},
            )
        _session_diffs_cache = (time.monotonic(), data)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


def mount_space(app):
    """Mount the Space folder at /space (index.html served at /space/)."""
    if SPACE_DIR.exists():
        app.mount("/space", StaticFiles(directory=str(SPACE_DIR), html=True), name="space")
    else:
        print(f"⚠️ Space folder not found at {SPACE_DIR}; /space not mounted (set SPACE_DIR to change)")
