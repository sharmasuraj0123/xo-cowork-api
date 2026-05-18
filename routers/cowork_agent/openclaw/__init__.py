"""OpenClaw-specific routes mounted onto the cowork-agent surface.

Currently contains:
- ``usage_dashboard``: ``/openclaw/usage/*`` analytics router (moved from
  the top-level ``routers/openclaw_usage.py`` during Phase 1 of the
  OpenClaw modularization).

Future Phase 6 work may extract additional OpenClaw branches from
shared routers (``agents.py``, ``config.py``, ``misc.py``, ``channels.py``)
into sibling files in this package.
"""

from fastapi import APIRouter

from .usage_dashboard import router as usage_dashboard_router

openclaw_routers: list[APIRouter] = [
    usage_dashboard_router,
]
