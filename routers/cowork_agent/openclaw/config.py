"""
OpenClaw-specific config endpoint.

Extracted from ``routers/cowork_agent/config.py`` during Phase 6c — the
shared config router stays polymorphic, while OpenClaw-only routes live
in this subpackage and mount via ``openclaw_routers``.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services.cowork_agent.agent_registry import get_agent
from services.cowork_agent.helpers import _mask_sensitive
from services.cowork_agent.adapters.openclaw.store import load_openclaw_config

router = APIRouter()

_OPENCLAW = get_agent("openclaw")


@router.get("/api/config/openclaw")
def get_openclaw_config():
    """Return the full ``openclaw.json`` with sensitive fields masked."""
    cfg = load_openclaw_config()
    if not cfg:
        return JSONResponse(
            status_code=404,
            content={"detail": f"{_OPENCLAW.config_file.name} not found"},
        )
    return _mask_sensitive(cfg)
