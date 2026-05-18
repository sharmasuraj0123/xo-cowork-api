"""OpenClaw-specific routes mounted onto the cowork-agent surface.

Currently contains:
- ``usage_dashboard``: ``/openclaw/usage/*`` analytics (moved from the
  top-level ``routers/openclaw_usage.py`` during Phase 1).
- ``config``: ``/api/config/openclaw`` (extracted from ``config.py``
  during Phase 6c).
- ``channels``: ``/api/channels/openclaw/status`` (extracted from
  ``misc.py`` during Phase 6c).
"""

from fastapi import APIRouter

from .channels import router as channels_router
from .config import router as config_router
from .usage_dashboard import router as usage_dashboard_router

openclaw_routers: list[APIRouter] = [
    usage_dashboard_router,
    config_router,
    channels_router,
]
