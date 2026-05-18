"""
OpenClaw agent-listing logic for ``OpenclawAdapter.list_agents``.

Ported from ``routers/cowork_agent/agents.py`` during Phase 4. The route
handler keeps its own copy of this logic for now (so both code paths exist
in parallel); Phase 5 will rewrite the route to call
``dispatcher.list_agents()`` and delete its inline branch.
"""

from __future__ import annotations

from .settings import AGENTS_DIR
from .store import (
    list_agent_entries,
    load_openclaw_config,
    resolve_agent_workspace_dir,
)
from services.cowork_agent.helpers import normalize_agent_id


def agent_info_for_id(
    cfg: dict,
    agent_id: str,
    display_name: str | None,
    description: str,
) -> dict:
    """Build the xo-cowork ``AgentInfo`` dict for an OpenClaw agent.

    ``name`` is the OpenClaw agent id so session.directory grouping matches.
    """
    aid = normalize_agent_id(agent_id)
    return {
        "name": aid,
        "description": description or display_name or aid,
        "mode": "primary",
        "tools": [],
        "permissions": {"rules": []},
        "system_prompt": None,
        "temperature": None,
        "metadata": {
            "backend": "openclaw",
            "openclaw_id": aid,
            "display_name": display_name or aid,
            "workspace": str(resolve_agent_workspace_dir(cfg, aid)),
        },
    }


def list_openclaw_agents() -> list[dict]:
    """Return AgentInfo dicts for every OpenClaw agent on disk.

    Walks ``~/.openclaw/agents/`` and joins with the ``agents.list`` block
    from ``~/.openclaw/openclaw.json`` for display name + identity.
    """
    cfg = load_openclaw_config()
    entries = {
        normalize_agent_id(str(e.get("id", ""))): e
        for e in list_agent_entries(cfg)
    }

    agents: list[dict] = []
    if not AGENTS_DIR.exists():
        return agents

    for d in sorted(AGENTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        aid = d.name
        meta = entries.get(normalize_agent_id(aid), {})
        display = meta.get("name") if isinstance(meta.get("name"), str) else None
        desc = ""
        if isinstance(meta.get("identity"), dict):
            ident = meta["identity"]
            if isinstance(ident.get("bio"), str):
                desc = ident["bio"]
        agents.append(agent_info_for_id(cfg, aid, display, desc))

    return agents
