"""
OpenClaw gateway status endpoint.

Extracted from ``routers/cowork_agent/misc.py`` during Phase 6c — the
shared misc router stays generic, while the OpenClaw gateway probe
lives in this subpackage and mounts via ``openclaw_routers``.
"""

from urllib.parse import urlparse

import httpx
from fastapi import APIRouter

from services.cowork_agent.adapters.openclaw.settings import OPENCLAW_API_URL

router = APIRouter()


@router.get("/api/channels/openclaw/status")
def openclaw_status():
    """Check if the OpenClaw gateway is reachable."""
    parsed = urlparse(OPENCLAW_API_URL)
    port = parsed.port or 18789
    try:
        resp = httpx.get(OPENCLAW_API_URL, timeout=3.0)
        running = resp.status_code in (200, 405)
    except Exception:
        running = False
    return {
        "installed": True,
        "running": running,
        "port": port if running else None,
        "ws_url": None,
    }
