"""
Backward-compat URL alias for legacy ``/openclaw/usage/*`` paths.

Every endpoint here binds to the **same handler function** from
``routers/cowork_agent/usage.py``. ``/openclaw/usage/X`` and
``/api/usage/X`` return byte-identical JSON because they ARE the same
handler under two URLs — no duplicate logic.

All usage computation lives in the per-agent module at
``services/cowork_agent/adapters/<AGENT_NAME>/usage.py``, resolved via
``services.cowork_agent.usage_loader.load_usage_module()``. This file
contains only route registration; flip a route by editing the canonical
handler in ``routers/cowork_agent/usage.py``.
"""
from fastapi import APIRouter

from routers.cowork_agent.usage import (
    usage_analytics,
    usage_summary,
    usage_summary_card,
    usage_sessions,
    usage_session,
)

router = APIRouter(prefix="/openclaw/usage", tags=["openclaw-usage-legacy"])

router.get("/analytics")(usage_analytics)
router.get("/summary")(usage_summary)
router.get("/summary/card")(usage_summary_card)
router.get("/sessions")(usage_sessions)
router.get("/sessions/{session_id}")(usage_session)
