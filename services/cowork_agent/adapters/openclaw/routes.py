"""
OpenClaw adapter-owned routes.

Endpoints that are specific to the openclaw backend (gateway reachability,
codex-via-openclaw OAuth accounts). They are mounted only when openclaw is the
active agent — the router aggregation resolves the active agent's ``routes``
module via ``try_load_capability('routes')`` — so they don't leak into
hermes/claude_code deployments.

Moved out of routers/cowork_agent/misc.py so no core router names a backend.
"""

import json
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services.cowork_agent.settings import OPENCLAW_API_URL, OPENCLAW_JSON
from services.cowork_agent.helpers import _mask_sensitive
from services.cowork_agent.adapters.openclaw.store import load_openclaw_config

router = APIRouter()


@router.get("/api/config/openclaw")
def get_openclaw_config():
    """Return the full openclaw config file (openclaw.json) with sensitive
    fields masked. Mounted only when openclaw is the active agent; the generic
    cross-agent reader is ``GET /api/config/agents/{name}``."""
    cfg = load_openclaw_config()
    if not cfg:
        return JSONResponse(status_code=404, content={"detail": f"{OPENCLAW_JSON.name} not found"})
    return _mask_sensitive(cfg)


@router.get("/api/channels/openclaw/status")
def openclaw_status():
    """Check if OpenClaw gateway is reachable."""
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


@router.get("/api/codex/status")
def codex_status():
    """List all Codex OAuth accounts found in openclaw.json and the main agent's auth-profiles.json."""
    accounts: list[dict] = []
    seen_emails: set[str] = set()

    def _collect(profiles_obj):
        if not isinstance(profiles_obj, dict):
            return
        for pid, prof in profiles_obj.items():
            if not isinstance(prof, dict):
                continue
            if prof.get("provider") != "openai-codex":
                continue
            email = prof.get("email") or pid
            if email in seen_emails:
                continue
            seen_emails.add(email)
            accounts.append({
                "id": pid,
                "email": email,
                "expires": prof.get("expires"),
            })

    try:
        config = json.loads(OPENCLAW_JSON.read_text(encoding="utf-8"))
        _collect(config.get("auth", {}).get("profiles", {}))
    except (OSError, json.JSONDecodeError):
        pass

    try:
        agent_auth_path = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
        agent_auth = json.loads(agent_auth_path.read_text(encoding="utf-8"))
        _collect(agent_auth.get("profiles", {}))
    except (OSError, json.JSONDecodeError):
        pass

    first_email = accounts[0]["email"] if accounts else ""
    return {
        "is_connected": bool(accounts),
        "email": first_email,
        "accounts": accounts,
    }
