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
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.staticfiles import StaticFiles

DEFAULT_SPACE_DIR = "~/Programming/XO/ClaudeWorkspace/xo-atlas"
SPACE_DIR = Path(os.getenv("SPACE_DIR", DEFAULT_SPACE_DIR)).expanduser()

router = APIRouter(prefix="/space/server", tags=["space"])


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


@router.get("/status")
async def space_server_status():
    """Lightweight status for the Space UI widget (also see /health)."""
    return {
        "status": "on",
        "pid": os.getpid(),
        "space_dir": str(SPACE_DIR),
        "space_dir_exists": SPACE_DIR.exists(),
    }


@router.post("/stop")
async def space_server_stop(request: Request):
    """Gracefully stop the server. Localhost only; restart via ./cowork-api.sh start."""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="stop is allowed from localhost only")

    async def _terminate_soon():
        await asyncio.sleep(0.4)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.get_running_loop().create_task(_terminate_soon())
    return {"status": "stopping", "restart": "./cowork-api.sh start"}


def mount_space(app):
    """Mount the Space folder at /space (index.html served at /space/)."""
    if SPACE_DIR.exists():
        app.mount("/space", StaticFiles(directory=str(SPACE_DIR), html=True), name="space")
    else:
        print(f"⚠️ Space folder not found at {SPACE_DIR}; /space not mounted (set SPACE_DIR to change)")
