"""
claude_code agents capability.

Implements the uniform agents contract (same surface every adapter exposes):

  list_agents()              -> list[dict]            # sidebar agents
  create_agent(body)         -> dict | JSONResponse   # POST /api/agents
  get_detail(agent_id)       -> dict | None           # None if not ours
  patch(agent_id, body)      -> resp | None            # None if not ours
  delete(agent_id)           -> resp | None            # None if not ours

claude_code agents are project folders under xo-projects; their record lives in
``<project>/.xo/agent.json`` (legacy: ``~/claude-cowork/<id>/.agent.json``,
read-only). The core router forwards here via ``load_capability('agents', …)``
instead of branching on ``backend == "claude_code"``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.responses import JSONResponse

from services.cowork_agent.helpers import normalize_agent_id
from services.cowork_agent.project_layout import (
    project_dir,
    scaffold_project,
    xo_dir,
    xo_projects_root,
)
from services.cowork_agent.settings import CLAUDE_COWORK_DIR


# ── On-disk record helpers ────────────────────────────────────────────────────


def _meta_path(agent_id: str) -> Path:
    """Canonical write location: <project>/.xo/agent.json under xo-projects."""
    return xo_dir(agent_id) / "agent.json"


def _meta_legacy_path(agent_id: str) -> Path:
    """Pre-xo-projects location: ~/claude-cowork/<id>/.agent.json (read-only fallback)."""
    return CLAUDE_COWORK_DIR / agent_id / ".agent.json"


def _load(agent_id: str) -> dict | None:
    """Read .xo/agent.json; fall back to legacy ~/claude-cowork/<id>/.agent.json."""
    for path in (_meta_path(agent_id), _meta_legacy_path(agent_id)):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                return None
    return None


def _write(agent_id: str, data: dict) -> None:
    """Always writes to the canonical xo-projects location."""
    path = _meta_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _workspace_path(agent_id: str) -> Path:
    """Where the project lives on disk. Prefers xo-projects, falls back to legacy."""
    new_path = project_dir(agent_id)
    if (xo_dir(agent_id) / "agent.json").exists() or (xo_dir(agent_id) / "project.json").exists():
        return new_path
    legacy = CLAUDE_COWORK_DIR / agent_id
    if legacy.is_dir():
        return legacy
    return new_path


def _agent_info(agent_id: str, meta: dict) -> dict:
    workspace = str(_workspace_path(agent_id))
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


# ── Uniform agents contract ───────────────────────────────────────────────────


def list_agents() -> list[dict]:
    """Sidebar agents: every xo-project dir that has a ``.xo/agent.json``."""
    agents: list[dict] = []
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
            agents.append(_agent_info(d.name, meta))
    return agents


def create_agent(body) -> dict | JSONResponse:
    """Create a claude_code agent: scaffold the project tree + write the record."""
    display_name = body.name.strip()
    agent_id = normalize_agent_id((body.id or body.name).strip())
    description = (body.description or "").strip()

    # Reject only if the claude_code agent record already exists. The project
    # folder being present is fine — multiple backends can attach to the same
    # xo-projects/<id>/ project.
    if _load(agent_id) is not None:
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
        _write(agent_id, meta)
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

    return _agent_info(agent_id, meta)


def get_detail(agent_id: str) -> dict | None:
    """Full agent snapshot if ``agent_id`` is a claude_code agent, else None."""
    aid = normalize_agent_id(agent_id)
    meta = _load(aid)
    if meta is None:
        return None
    workspace_path = _workspace_path(aid)
    return {
        "id": aid,
        "display_name": (meta.get("name") or "").strip() or aid,
        "description": meta.get("description") or "",
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


def patch(agent_id: str, body) -> dict | JSONResponse | None:
    """Patch a claude_code agent's name/description; None if not ours."""
    aid = normalize_agent_id(agent_id)
    if _load(aid) is None:
        return None
    if not body.model_fields_set:
        detail = get_detail(aid)
        return detail if detail else JSONResponse(status_code=404, content={"detail": "Not found"})
    meta = _load(aid) or {}
    if body.name is not None:
        meta["name"] = body.name.strip()
    if body.description is not None:
        meta["description"] = body.description.strip()
    _write(aid, meta)
    detail = get_detail(aid)
    return detail if detail else JSONResponse(status_code=500, content={"detail": "Failed to read agent after update"})


def delete(agent_id: str) -> dict | JSONResponse | None:
    """claude_code has no delete contract today."""
    return None
