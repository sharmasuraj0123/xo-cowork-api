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
from services.cowork_agent.visualizer.xo_overview import build_xo_overview
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


def mount_space(app):
    """Mount the Space folder at /space (index.html served at /space/)."""
    if SPACE_DIR.exists():
        app.mount("/space", StaticFiles(directory=str(SPACE_DIR), html=True), name="space")
    else:
        print(f"⚠️ Space folder not found at {SPACE_DIR}; /space not mounted (set SPACE_DIR to change)")
