"""
Aggregated usage statistics for the active agent.

Thin wrapper around ``services.cowork_agent.usage_loader.load_usage_module()``.
The body of ``/api/usage`` delegates to the loaded module's
``aggregate_for_dashboard`` — the active agent's ``config/agents/<name>/usage/usage.py``.

Returns the UsageStats shape expected by the frontend (src/types/usage.ts).
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from services.cowork_agent.usage_loader import load_usage_module

router = APIRouter()


@router.get("/api/usage")
def usage(
    days: int = 30,
    tz: Optional[str] = Query(None, description="Day-bucket timezone: 'local' (default, host TZ) or 'utc'."),
):
    """Aggregate usage for the active agent within the last ``days``.

    Active agent is resolved from AGENT_NAME (or DEFAULT_AGENT) and the
    corresponding module loaded via importlib. No per-agent if/else lives
    in the router.
    """
    days = max(1, min(days, 365))
    tz_resolved = tz if tz in ("local", "utc") else "local"

    try:
        mod = load_usage_module()
    except ModuleNotFoundError as e:
        raise HTTPException(
            status_code=501,
            detail=f"no usage module for active agent (tried config.agents.<name>.usage.usage): {e}",
        )

    return mod.aggregate_for_dashboard(days=days, tz=tz_resolved)
