"""
Agent CRUD endpoints.

Maps between OpenClaw's on-disk agent records and the xo-cowork `AgentInfo`
shape the frontend expects. Create/patch operations mutate `openclaw.json`
via `openclaw_store`. Claude Code agents are stored under ~/claude-cowork/.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.cowork_agent.settings import AGENTS_DIR, CLAUDE_COWORK_DIR, _WORKSPACE_DOC_FILES
from services.cowork_agent.helpers import (
    _path_must_be_under_home,
    _read_json_file_safe,
    _read_text_limited,
    _redact_secrets_nested,
    _summarize_auth_profiles,
    normalize_agent_id,
)
from services.cowork_agent.openclaw_store import (
    _agent_model_to_display,
    apply_agent_list_entry,
    ensure_openclaw_agent_disk,
    find_agent_entry_index,
    list_agent_entries,
    load_openclaw_config,
    resolve_agent_workspace_dir,
    write_openclaw_config,
)
from services.cowork_agent.project_layout import (
    project_dir,
    scaffold_project,
    xo_dir,
    xo_projects_root,
)

router = APIRouter()


# ── Pydantic request bodies ──────────────────────────────────────────────────


class CreateAgentBody(BaseModel):
    """Payload for POST /api/agents — supports openclaw and claude_code backends."""

    name: str = Field(..., min_length=1, max_length=200)
    id: str | None = Field(None, max_length=80)
    description: str | None = Field(None, max_length=4000)
    workspace: str | None = Field(None, max_length=2048)
    backend: Literal["openclaw", "claude_code"] = "openclaw"


class UpdateAgentBody(BaseModel):
    """PATCH /api/agents/{id} — only fields present in the JSON body are applied."""

    name: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=4000)
    workspace: str | None = Field(None, max_length=2048)
    model: str | None = Field(None, max_length=400)
    identity_name: str | None = Field(None, max_length=200)
    identity_emoji: str | None = Field(None, max_length=32)


# ── Claude Code agent helpers ────────────────────────────────────────────────


def _claude_agent_meta_path(agent_id: str) -> Path:
    """Canonical write location: <project>/.xo/agent.json under xo-projects."""
    return xo_dir(agent_id) / "agent.json"


def _claude_agent_meta_legacy_path(agent_id: str) -> Path:
    """Pre-xo-projects location: ~/claude-cowork/<id>/.agent.json (read-only fallback)."""
    return CLAUDE_COWORK_DIR / agent_id / ".agent.json"


def _load_claude_agent(agent_id: str) -> dict | None:
    """Read .xo/agent.json; fall back to legacy ~/claude-cowork/<id>/.agent.json."""
    for path in (_claude_agent_meta_path(agent_id), _claude_agent_meta_legacy_path(agent_id)):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                return None
    return None


def _write_claude_agent(agent_id: str, data: dict) -> None:
    """Always writes to the canonical xo-projects location."""
    path = _claude_agent_meta_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _claude_workspace_path(agent_id: str) -> Path:
    """Where the project lives on disk. Prefers xo-projects, falls back to legacy."""
    new_path = project_dir(agent_id)
    if (xo_dir(agent_id) / "agent.json").exists() or (xo_dir(agent_id) / "project.json").exists():
        return new_path
    legacy = CLAUDE_COWORK_DIR / agent_id
    if legacy.is_dir():
        return legacy
    return new_path


def _agent_info_claude(agent_id: str, meta: dict) -> dict:
    workspace = str(_claude_workspace_path(agent_id))
    return {
        "name": agent_id,
        "description": meta.get("description") or meta.get("name") or agent_id,
        "mode": "primary",
        "tools": [],
        "permissions": {"rules": []},
        "system_prompt": None,
        "temperature": None,
        "metadata": {
            "backend": "claude_code",
            "display_name": meta.get("name") or agent_id,
            "workspace": workspace,
        },
    }


# ── Internal helpers (module-private) ────────────────────────────────────────


def _agent_info_for_id(cfg: dict, agent_id: str, display_name: str | None, description: str) -> dict:
    """xo-cowork AgentInfo shape; `name` is the OpenClaw agent id so session.directory grouping matches."""
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


def get_agent_detail(agent_id: str) -> dict | None:
    """
    Full agent snapshot for the UI: OpenClaw config, workspace docs, on-disk models,
    redacted auth, sessions index, and global auth summary.
    """
    aid = normalize_agent_id(agent_id)

    # Check Claude Code backend first
    claude_meta = _load_claude_agent(aid)
    if claude_meta is not None:
        workspace_path = _claude_workspace_path(aid)
        return {
            "id": aid,
            "display_name": (claude_meta.get("name") or "").strip() or aid,
            "description": claude_meta.get("description") or "",
            "workspace": str(workspace_path),
            "model": None,
            "model_raw": None,
            "identity": {"name": None, "emoji": None, "bio": None},
            "config_entry": {},
            "agents_defaults": {},
            "workspace_files": {},
            "on_disk": {
                "agent_dir": str(workspace_path),
                "models_catalog": None,
                "auth_state": None,
                "auth_profiles": None,
            },
            "sessions": {
                "index_path": str(workspace_path / ".sessions"),
                "count": 0,
                "session_ids": [],
            },
            "openclaw_global_auth": {},
            "backend": "claude_code",
        }

    agent_root = AGENTS_DIR / aid
    if not agent_root.is_dir():
        return None

    cfg = load_openclaw_config()
    entries = list_agent_entries(cfg)
    idx = find_agent_entry_index(entries, aid)
    entry = dict(entries[idx]) if idx >= 0 else {}

    display = entry.get("name") if isinstance(entry.get("name"), str) else None
    desc = ""
    identity_cfg: dict = {}
    if isinstance(entry.get("identity"), dict):
        identity_cfg = dict(entry["identity"])
        bio = identity_cfg.get("bio")
        if isinstance(bio, str):
            desc = bio

    ws_path = resolve_agent_workspace_dir(cfg, aid)
    workspace_path_str = str(ws_path)
    workspace_files: dict[str, str | None] = {}
    for fname in _WORKSPACE_DOC_FILES:
        content = _read_text_limited(ws_path / fname)
        if content is not None:
            workspace_files[fname] = content
        elif (ws_path / fname).is_file():
            workspace_files[fname] = ""

    agent_disk = agent_root / "agent"
    models_catalog = _read_json_file_safe(agent_disk / "models.json")
    auth_state = _read_json_file_safe(agent_disk / "auth-state.json")
    auth_profiles_raw = _read_json_file_safe(agent_disk / "auth-profiles.json")
    auth_profiles_safe = None
    if isinstance(auth_profiles_raw, dict):
        auth_profiles_safe = _redact_secrets_nested(auth_profiles_raw)

    sessions_index_path = agent_root / "sessions" / "sessions.json"
    session_ids: list[str] = []
    session_count = 0
    idx_data = _read_json_file_safe(sessions_index_path)
    if isinstance(idx_data, dict):
        seen_ids: set[str] = set()
        for _key, meta in idx_data.items():
            if isinstance(meta, dict):
                sid = meta.get("sessionId")
                if isinstance(sid, str) and sid.strip():
                    seen_ids.add(sid.strip())
        session_count = len(seen_ids)
        session_ids = sorted(seen_ids)[:80]

    global_auth = (cfg.get("auth") or {}).get("profiles")
    global_auth_summary = _summarize_auth_profiles(global_auth) if isinstance(global_auth, dict) else {}

    agents_defaults = cfg.get("agents", {}).get("defaults")
    if not isinstance(agents_defaults, dict):
        agents_defaults = {}

    return {
        "id": aid,
        "display_name": ((display or "").strip() or aid),
        "description": desc,
        "workspace": workspace_path_str,
        "model": _agent_model_to_display(entry.get("model")),
        "model_raw": entry.get("model"),
        "identity": {
            "name": identity_cfg.get("name") if isinstance(identity_cfg.get("name"), str) else None,
            "emoji": identity_cfg.get("emoji") if isinstance(identity_cfg.get("emoji"), str) else None,
            "bio": desc or None,
        },
        "config_entry": entry,
        "agents_defaults": agents_defaults,
        "workspace_files": workspace_files,
        "on_disk": {
            "agent_dir": str(agent_disk.resolve()),
            "models_catalog": models_catalog,
            "auth_state": auth_state,
            "auth_profiles": auth_profiles_safe,
        },
        "sessions": {
            "index_path": str(sessions_index_path.resolve()),
            "count": session_count,
            "session_ids": session_ids,
        },
        "openclaw_global_auth": global_auth_summary,
        "backend": "openclaw",
    }


def patch_agent_into_config(cfg: dict, agent_id: str, body: UpdateAgentBody) -> dict:
    aid = normalize_agent_id(agent_id)
    if find_agent_entry_index(list_agent_entries(cfg), aid) < 0:
        ws_dir = resolve_agent_workspace_dir(cfg, aid)
        cfg = apply_agent_list_entry(cfg, aid, aid, ws_dir)
    entries = list_agent_entries(cfg)
    idx = find_agent_entry_index(entries, aid)
    if idx < 0:
        raise RuntimeError("could not resolve agent in openclaw.json")
    next_list = [dict(e) for e in entries]
    entry = dict(next_list[idx])
    if body.name is not None:
        stripped = body.name.strip()
        entry["name"] = stripped or aid
    if body.workspace is not None:
        ws = Path(body.workspace.strip()).expanduser().resolve()
        if not _path_must_be_under_home(ws):
            raise ValueError("workspace must resolve to a path under your home directory")
        entry["workspace"] = str(ws)
    if body.description is not None:
        desc = body.description.strip()
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
    if body.model is not None:
        m = body.model.strip()
        if m:
            entry["model"] = m
        else:
            entry.pop("model", None)
    if body.identity_name is not None or body.identity_emoji is not None:
        ident = dict(entry.get("identity") or {})
        if body.identity_name is not None:
            nv = body.identity_name.strip()
            if nv:
                ident["name"] = nv
            else:
                ident.pop("name", None)
        if body.identity_emoji is not None:
            ev = body.identity_emoji.strip()
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
    return {**cfg, "agents": agents_block}


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/api/agents")
def list_agents():
    import os
    active_backend = os.getenv("AGENT_NAME", "openclaw")
    agents: list[dict] = []

    if active_backend == "claude_code":
        # Claude Code: one agent per xo-project that has a .xo/agent.json record.
        projects_root = xo_projects_root()
        if projects_root.exists():
            for d in sorted(projects_root.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                meta_path = d / ".xo" / "agent.json"
                if not meta_path.exists():
                    continue
                try:
                    meta = json.loads(meta_path.read_text())
                except Exception:
                    meta = {}
                agents.append(_agent_info_claude(d.name, meta))
    else:
        # OpenClaw: agents registered in openclaw.json + their ~/.openclaw/agents/<id>/ dirs.
        cfg = load_openclaw_config()
        entries = {normalize_agent_id(str(e.get("id", ""))): e for e in list_agent_entries(cfg)}
        if AGENTS_DIR.exists():
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
                agents.append(_agent_info_for_id(cfg, aid, display, desc))

    return agents


@router.post("/api/agents")
def create_agent(body: CreateAgentBody):
    display_name = body.name.strip()
    agent_id = normalize_agent_id((body.id or body.name).strip())
    if agent_id == "main":
        return JSONResponse(status_code=400, content={"detail": 'Agent id "main" is reserved; choose another id or name.'})

    description = (body.description or "").strip()

    if body.backend == "claude_code":
        # Reject only if the claude_code agent record already exists. The
        # project folder being present is fine — multiple backends can
        # attach to the same xo-projects/<id>/ project.
        if _load_claude_agent(agent_id) is not None:
            return JSONResponse(
                status_code=409,
                content={"detail": f'Claude Code agent "{agent_id}" already exists.'},
            )

        try:
            scaffold_project(agent_id, display_name=display_name, description=description)
            meta = {
                "id": agent_id,
                "name": display_name,
                "description": description,
                "backend": "claude_code",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_claude_agent(agent_id, meta)
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": str(e)})

        return _agent_info_claude(agent_id, meta)

    # OpenClaw agent (default)
    cfg = load_openclaw_config()
    existing_entries = list_agent_entries(cfg)
    if find_agent_entry_index(existing_entries, agent_id) >= 0:
        return JSONResponse(status_code=409, content={"detail": f'Agent "{agent_id}" already exists in openclaw.json.'})
    if (AGENTS_DIR / agent_id).exists():
        return JSONResponse(status_code=409, content={"detail": f'Agent directory "{agent_id}" already exists under ~/.openclaw/agents.'})

    if body.workspace and body.workspace.strip():
        ws = Path(body.workspace.strip()).expanduser().resolve()
        if not _path_must_be_under_home(ws):
            return JSONResponse(
                status_code=400,
                content={"detail": "workspace must resolve to a path under your home directory."},
            )
        workspace_dir = ws
    else:
        # OpenClaw agents are not projects: default the workspace to the
        # agent's openclaw home so the gateway has a real path to use, and
        # nothing materializes a folder under xo-projects/ (which would
        # pollute the project dropdown). Users pick a real project per chat.
        workspace_dir = AGENTS_DIR / agent_id

    try:
        # OpenClaw agents live under ~/.openclaw/agents/<id>/ and are listed in
        # ~/.openclaw/openclaw.json. They are NOT projects — do not scaffold a
        # folder under xo-projects/. The project the user chats against is a
        # separate workspace selection per chat.
        next_cfg = apply_agent_list_entry(cfg, agent_id, display_name, workspace_dir)
        write_openclaw_config(next_cfg)
        ensure_openclaw_agent_disk(agent_id, workspace_dir)
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

    desc = description or display_name
    return _agent_info_for_id(next_cfg, agent_id, display_name, desc)


@router.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    detail = get_agent_detail(agent_id)
    if not detail:
        return JSONResponse(status_code=404, content={"detail": f'Agent "{agent_id}" not found'})
    return detail


@router.patch("/api/agents/{agent_id}")
def patch_agent(agent_id: str, body: UpdateAgentBody):
    aid = normalize_agent_id(agent_id)

    # Claude Code agents don't support patch via OpenClaw mechanisms
    if _load_claude_agent(aid) is not None:
        if not body.model_fields_set:
            detail = get_agent_detail(aid)
            return detail if detail else JSONResponse(status_code=404, content={"detail": "Not found"})
        # Update name/description in .agent.json
        meta = _load_claude_agent(aid) or {}
        if body.name is not None:
            meta["name"] = body.name.strip()
        if body.description is not None:
            meta["description"] = body.description.strip()
        _write_claude_agent(aid, meta)
        detail = get_agent_detail(aid)
        return detail if detail else JSONResponse(status_code=500, content={"detail": "Failed to read agent after update"})

    if not (AGENTS_DIR / aid).is_dir():
        return JSONResponse(status_code=404, content={"detail": f'Agent "{aid}" not found'})
    if not body.model_fields_set:
        detail = get_agent_detail(aid)
        return detail if detail else JSONResponse(status_code=404, content={"detail": "Not found"})
    try:
        cfg = load_openclaw_config()
        next_cfg = patch_agent_into_config(cfg, aid, body)
        write_openclaw_config(next_cfg)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})
    detail = get_agent_detail(aid)
    return detail if detail else JSONResponse(status_code=500, content={"detail": "Failed to read agent after update"})
