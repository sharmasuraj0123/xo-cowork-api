"""
OpenClaw agent listing + CRUD logic for ``OpenclawAdapter``.

Ported from ``routers/cowork_agent/agents.py`` during Phases 4 & 5. The
route handler keeps inline copies of the same logic for now (so both
code paths exist in parallel); a later cleanup phase will delete the
inline branches once we're confident nothing else references them.

Errors raised by create/update map to HTTP status codes at the route
layer:

  - ``ValueError``       → 400 Bad Input
  - ``FileExistsError``  → 409 Conflict
  - ``KeyError``         → 404 Not Found
  - ``RuntimeError``     → 500 Internal Error
"""

from __future__ import annotations

from pathlib import Path

from .settings import AGENTS_DIR
from .store import (
    apply_agent_list_entry,
    ensure_openclaw_agent_disk,
    find_agent_entry_index,
    list_agent_entries,
    load_openclaw_config,
    resolve_agent_workspace_dir,
    write_openclaw_config,
)
from services.cowork_agent.helpers import (
    _path_must_be_under_home,
    normalize_agent_id,
)


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


def create_openclaw_agent(body: dict) -> dict:
    """Create a new OpenClaw agent. Returns the AgentInfo dict.

    Body fields:
      - ``name``      (required) — display name
      - ``id``        (optional) — agent id; defaults to ``name``
      - ``description`` (optional)
      - ``workspace``   (optional) — must resolve under ``$HOME``

    Raises:
      - ``ValueError``      — bad input (reserved id, workspace outside home)
      - ``FileExistsError`` — agent already in openclaw.json or on disk
      - ``RuntimeError``    — internal write failure
    """
    display_name = (body.get("name") or "").strip()
    raw_id = (body.get("id") or display_name).strip()
    agent_id = normalize_agent_id(raw_id)
    description = (body.get("description") or "").strip()
    workspace_in = (body.get("workspace") or "").strip()

    if agent_id == "main":
        raise ValueError('Agent id "main" is reserved; choose another id or name.')

    cfg = load_openclaw_config()
    existing_entries = list_agent_entries(cfg)
    if find_agent_entry_index(existing_entries, agent_id) >= 0:
        raise FileExistsError(f'Agent "{agent_id}" already exists in openclaw.json.')
    if (AGENTS_DIR / agent_id).exists():
        raise FileExistsError(
            f'Agent directory "{agent_id}" already exists under ~/.openclaw/agents.'
        )

    if workspace_in:
        ws = Path(workspace_in).expanduser().resolve()
        if not _path_must_be_under_home(ws):
            raise ValueError(
                "workspace must resolve to a path under your home directory."
            )
        workspace_dir = ws
    else:
        # OpenClaw agents are not projects: default the workspace to the
        # agent's openclaw home so the gateway has a real path to use, and
        # nothing materializes a folder under xo-projects/ (which would
        # pollute the project dropdown). Users pick a real project per chat.
        workspace_dir = AGENTS_DIR / agent_id

    try:
        next_cfg = apply_agent_list_entry(cfg, agent_id, display_name, workspace_dir)
        write_openclaw_config(next_cfg)
        ensure_openclaw_agent_disk(agent_id, workspace_dir)
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    return agent_info_for_id(
        next_cfg, agent_id, display_name, description or display_name
    )


def update_openclaw_agent(agent_id: str, patch: dict) -> dict:
    """Patch an OpenClaw agent's entry in openclaw.json. Returns the updated
    AgentInfo. Only fields present in ``patch`` are applied.

    Recognized patch fields: ``name``, ``description``, ``workspace``,
    ``model``, ``identity_name``, ``identity_emoji``.

    Raises:
      - ``KeyError``     — agent does not exist on disk
      - ``ValueError``   — invalid workspace path
      - ``RuntimeError`` — internal write failure
    """
    aid = normalize_agent_id(agent_id)
    if not (AGENTS_DIR / aid).is_dir():
        raise KeyError(f'Agent "{aid}" not found')

    cfg = load_openclaw_config()

    # Make sure there's an agents.list entry to patch.
    if find_agent_entry_index(list_agent_entries(cfg), aid) < 0:
        ws_dir = resolve_agent_workspace_dir(cfg, aid)
        cfg = apply_agent_list_entry(cfg, aid, aid, ws_dir)

    entries = list_agent_entries(cfg)
    idx = find_agent_entry_index(entries, aid)
    if idx < 0:
        raise RuntimeError("could not resolve agent in openclaw.json")

    next_list = [dict(e) for e in entries]
    entry = dict(next_list[idx])

    if "name" in patch and patch["name"] is not None:
        stripped = (patch["name"] or "").strip()
        entry["name"] = stripped or aid

    if "workspace" in patch and patch["workspace"] is not None:
        ws = Path((patch["workspace"] or "").strip()).expanduser().resolve()
        if not _path_must_be_under_home(ws):
            raise ValueError("workspace must resolve to a path under your home directory")
        entry["workspace"] = str(ws)

    if "description" in patch and patch["description"] is not None:
        desc = (patch["description"] or "").strip()
        ident = dict(entry.get("identity") or {})
        if desc:
            ident["bio"] = desc
            entry["identity"] = ident
        else:
            ident.pop("bio", None)
            if ident:
                entry["identity"] = ident
            else:
                entry.pop("identity", None)

    if "model" in patch and patch["model"] is not None:
        m = (patch["model"] or "").strip()
        if m:
            entry["model"] = m
        else:
            entry.pop("model", None)

    if (
        ("identity_name" in patch and patch["identity_name"] is not None)
        or ("identity_emoji" in patch and patch["identity_emoji"] is not None)
    ):
        ident = dict(entry.get("identity") or {})
        if patch.get("identity_name") is not None:
            nv = (patch["identity_name"] or "").strip()
            if nv:
                ident["name"] = nv
            else:
                ident.pop("name", None)
        if patch.get("identity_emoji") is not None:
            ev = (patch["identity_emoji"] or "").strip()
            if ev:
                ident["emoji"] = ev
            else:
                ident.pop("emoji", None)
        if ident:
            entry["identity"] = ident
        else:
            entry.pop("identity", None)

    next_list[idx] = entry
    agents_block = dict(cfg.get("agents") or {})
    agents_block["list"] = next_list
    next_cfg = {**cfg, "agents": agents_block}

    try:
        write_openclaw_config(next_cfg)
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    # Return the updated AgentInfo.
    display = entry.get("name") if isinstance(entry.get("name"), str) else None
    desc = ""
    if isinstance(entry.get("identity"), dict):
        bio = entry["identity"].get("bio")
        if isinstance(bio, str):
            desc = bio
    return agent_info_for_id(next_cfg, aid, display, desc)
