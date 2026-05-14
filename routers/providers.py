"""
Providers status API router.

Reports connection state (OAuth + API key) for every model provider that is
``enabled: true`` in xo.json's ``models`` section. Dispatches on
``AGENT_NAME`` because each agent has its own env source for API keys, even
though the OAuth probes (claude / codex) are agent-independent.

Disabled providers are omitted from the response entirely — the frontend
drives section visibility off xo.json's `enabled` flags, so this endpoint
only reports on what is in scope to render. ``connected: false`` therefore
always means "enabled but not authenticated", never "disabled in manifest".

Unlike ``/models/status`` and ``/channels/status`` this result is **not**
mirrored into xo.json: connection state can flip between probes (the user
runs ``claude auth login``, edits .env, etc.), so we keep it live-only.
"""

from fastapi import APIRouter, HTTPException

from services.xo_manifest import resolve_agent_name

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("/status")
async def providers_status():
    """Return per-agent provider connection status."""
    agent = resolve_agent_name()

    if agent == "openclaw":
        from services.cowork_agent.adapters.openclaw.providers_status import (
            get_providers_status,
        )
    elif agent == "hermes":
        from services.cowork_agent.adapters.hermes.providers_status import (
            get_providers_status,
        )
    elif agent == "claude_code":
        from services.cowork_agent.adapters.claude_code.providers_status import (
            get_providers_status,
        )
    else:
        # Future agent without a wired adapter. 501 distinguishes
        # "no source yet" from "agent unknown" — same convention as
        # the models / channels routers.
        raise HTTPException(
            status_code=501,
            detail={
                "ok": False,
                "error": f"no providers-status source for agent '{agent}'",
                "agent": agent,
            },
        )

    try:
        return await get_providers_status()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": f"unexpected error: {e}", "agent": agent},
        )
