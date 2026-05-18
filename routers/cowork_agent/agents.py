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

from services.cowork_agent.settings import CLAUDE_COWORK_DIR, _WORKSPACE_DOC_FILES
from services.cowork_agent.adapters.openclaw.settings import AGENTS_DIR
from services.cowork_agent.helpers import (
    _read_json_file_safe,
    _read_text_limited,
    _redact_secrets_nested,
    _summarize_auth_profiles,
    normalize_agent_id,
)
from services.cowork_agent.adapters.openclaw.store import (
    _agent_model_to_display,
    find_agent_entry_index,
    list_agent_entries,
    load_openclaw_config,
    resolve_agent_workspace_dir,
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
    """Payload for POST /api/agents — supports openclaw, claude_code, and hermes backends."""

    name: str = Field(..., min_length=1, max_length=200)
    id: str | None = Field(None, max_length=80)
    description: str | None = Field(None, max_length=4000)
    workspace: str | None = Field(None, max_length=2048)
    backend: Literal["openclaw", "claude_code", "hermes"] = "openclaw"


class UpdateAgentBody(BaseModel):
    """PATCH /api/agents/{id} — only fields present in the JSON body are applied."""

    name: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=4000)
    workspace: str | None = Field(None, max_length=2048)
    model: str | None = Field(None, max_length=400)
    identity_name: str | None = Field(None, max_length=200)
    identity_emoji: str | None = Field(None, max_length=32)
    # Hermes-only today: writes to ``<profile>/SOUL.md``. OpenClaw uses
    # ``identity_*`` for the same role; claude_code doesn't take it.
    system_prompt: str | None = Field(None, max_length=64_000)


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


def _agent_info_hermes(profile_name: str) -> dict:
    """xo-cowork AgentInfo shape for a hermes profile.

    Hermes profiles are independent state DBs under
    ``~/.hermes/profiles/<name>/state.db`` — *not* workspace directories.
    They don't map to a single project folder, so ``workspace`` stays empty.
    Frontend routing should read ``metadata.backend`` directly when this
    agent is selected (don't derive backend from workspaceDirectory for
    hermes — multiple profiles would collide on the same path).
    """
    return {
        "name": profile_name,
        "description": profile_name,
        "mode": "primary",
        "tools": [],
        "permissions": {"rules": []},
        "system_prompt": None,
        "temperature": None,
        "metadata": {
            "backend": "hermes",
            "hermes_profile": profile_name,
            "display_name": profile_name,
            "workspace": "",
        },
    }


def _agent_detail_hermes(profile_name: str) -> dict:
    """Full agent snapshot for a hermes profile, parallel to the openclaw
    branch below. Surfaces only what xo-cowork can read cheaply from disk:
    the profile dir, SOUL.md preview, .env keys (no values), session count,
    and gateway pool entry. The fine-grained per-profile edits live under
    ``/api/agents/hermes/{profile}/...`` so the FE can fetch what it needs.
    """
    from services.cowork_agent.agent_registry import get_agent
    from services.cowork_agent.adapters.hermes import gateway_pool
    from services.cowork_agent.settings import HERMES_DIR

    hermes_manifest = get_agent("hermes")
    # ``default`` profile lives at HERMES_DIR (~/.hermes/) itself, not under
    # the profiles subdir — match the layout hermes uses on disk.
    profile_dir = HERMES_DIR if profile_name == "default" else hermes_manifest.agents_dir / profile_name

    description_sidecar = profile_dir / ".xo_description"
    description = ""
    if description_sidecar.is_file():
        try:
            description = description_sidecar.read_text(errors="replace").strip()
        except OSError:
            description = ""

    display_sidecar = profile_dir / ".xo_display_name"
    display_name = profile_name
    if display_sidecar.is_file():
        try:
            candidate = display_sidecar.read_text(errors="replace").strip()
            if candidate:
                display_name = candidate
        except OSError:
            pass

    soul_path = profile_dir / "SOUL.md"
    soul_preview: str | None = None
    if soul_path.is_file():
        try:
            soul_preview = soul_path.read_text(errors="replace")[:800]
        except OSError:
            soul_preview = None

    # Session count — read from this profile's state.db only (don't walk
    # every profile; the global hermes_state_db helpers are aggregate).
    session_count = 0
    state_db = profile_dir / "state.db"
    if state_db.is_file():
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
                session_count = int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception:
            session_count = 0

    # .env keys — no values, mirrors /api/secrets/env/keys' shape.
    env_path = profile_dir / ".env"
    env_keys: list[str] = []
    if env_path.is_file():
        try:
            for line in env_path.read_text(errors="replace").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key = stripped.split("=", 1)[0].strip()
                if key:
                    env_keys.append(key)
        except OSError:
            pass

    # Gateway pool entry — lazy, only includes status for non-default
    # profiles (default lives outside the pool, managed by hermes.sh).
    gateway: dict = {"managed_by": "hermes.sh"} if profile_name == "default" else {}
    if profile_name != "default":
        for entry in gateway_pool.list_pool():
            if entry.get("profile") == profile_name:
                gateway = {
                    "managed_by": "pool",
                    "port": entry.get("port"),
                    "pid": entry.get("pid"),
                    "alive": entry.get("alive"),
                    "listening": entry.get("listening"),
                    "started_at": entry.get("started_at"),
                }
                break
        else:
            gateway = {"managed_by": "pool", "running": False}

    return {
        "id": profile_name,
        "display_name": display_name,
        "description": description,
        "workspace": str(profile_dir),
        "model": None,
        "model_raw": None,
        "identity": {"name": display_name, "emoji": None, "bio": description or None},
        "config_entry": {},
        "agents_defaults": {},
        "workspace_files": {},
        "on_disk": {
            "agent_dir": str(profile_dir),
            "config_yaml": str(profile_dir / "config.yaml"),
            "env_file": str(env_path),
            "soul_md": str(soul_path) if soul_path.is_file() else None,
            "state_db": str(state_db) if state_db.is_file() else None,
            "env_keys": env_keys,
        },
        "soul_preview": soul_preview,
        "gateway": gateway,
        "sessions": {
            "index_path": str(profile_dir / "sessions"),
            "count": session_count,
            "session_ids": [],
        },
        "openclaw_global_auth": {},
        "backend": "hermes",
        "hermes_profile": profile_name,
    }


def get_agent_detail(agent_id: str) -> dict | None:
    """
    Full agent snapshot for the UI: OpenClaw config, workspace docs, on-disk models,
    redacted auth, sessions index, and global auth summary.

    Dispatch by backend (in order):
    - Claude Code: matches when ``.xo/agent.json`` exists for ``agent_id``.
    - Hermes: matches when ``agent_id`` is a profile under ``~/.hermes/profiles/``.
    - OpenClaw: fallback when the openclaw agents dir contains ``agent_id``.
    Returns ``None`` if no backend recognizes the id.
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

    # Check hermes backend next. Profile name == agent_id (1:1 by design).
    # We look this up via the state-db helper because it's the same code
    # path that powers /api/agents listing — staying consistent prevents
    # GET /api/agents returning a profile that GET /api/agents/{id} 404s on.
    from services.cowork_agent.hermes_state_db import list_all_profile_names
    if aid in list_all_profile_names():
        return _agent_detail_hermes(aid)

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


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/api/agents")
async def list_agents():
    """Return the sidebar agents for the active backend (``AGENT_NAME`` env).

    With ``AGENT_NAME=hermes`` we surface only hermes profiles; openclaw and
    claude_code stay invisible. This matches the user's mental model: if
    they've switched to hermes, openclaw isn't supposed to be touched at
    all — showing openclaw agents leads to chats accidentally routing
    through the wrong backend.
    """
    import os
    active_backend = os.getenv("AGENT_NAME", "openclaw")
    agents: list[dict] = []

    if active_backend == "claude_code":
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
    elif active_backend == "hermes":
        from services.cowork_agent.hermes_state_db import list_all_profile_names
        for profile_name in list_all_profile_names():
            agents.append(_agent_info_hermes(profile_name))
    else:
        # OpenClaw path now dispatches through the adapter (Phase 5).
        from services.cowork_agent.dispatcher import AgentDispatcher
        agents = await AgentDispatcher("openclaw").list_agents()

    return agents


@router.post("/api/agents")
async def create_agent(body: CreateAgentBody):
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

    if body.backend == "hermes":
        # Hermes profiles are managed by the hermes CLI (`hermes profile create`).
        # Profile id == sidebar bucket name; the on-disk layout is
        # ``~/.hermes/profiles/<id>/`` with its own state.db once the first
        # chat happens. We delegate creation to the CLI so future hermes
        # changes (extra directories, schema bumps) don't drift here.
        import subprocess
        from services.cowork_agent.agent_registry import get_agent

        hermes_manifest = get_agent("hermes")
        hermes_home = hermes_manifest.home_dir
        profiles_dir = hermes_manifest.agents_dir  # ~/.hermes/profiles

        # Reject collisions with the default profile or an existing on-disk dir.
        if agent_id == "default":
            return JSONResponse(status_code=400, content={"detail": 'Profile id "default" is reserved.'})
        if profiles_dir.exists() and (profiles_dir / agent_id).is_dir():
            return JSONResponse(status_code=409, content={"detail": f'Hermes profile "{agent_id}" already exists.'})

        argv = [hermes_manifest.binary, "profile", "create", agent_id]
        try:
            result = subprocess.run(
                argv,
                cwd=str(hermes_manifest.cwd),
                capture_output=True,
                text=True,
                timeout=hermes_manifest.cli_timeout_seconds,
            )
        except FileNotFoundError:
            return JSONResponse(status_code=500, content={"detail": "hermes CLI not found on PATH"})
        except subprocess.TimeoutExpired:
            return JSONResponse(status_code=504, content={"detail": "hermes profile create timed out"})
        except Exception as e:  # noqa: BLE001
            return JSONResponse(status_code=500, content={"detail": f"hermes profile create failed: {e}"})

        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()[:500]
            return JSONResponse(
                status_code=500,
                content={"detail": f"hermes profile create exited {result.returncode}: {stderr}"},
            )

        # Best-effort: stamp the display name into the profile dir as a
        # ``.xo_display_name`` sidecar so future frontend renames have a
        # place to read from. The profile dir is created by the CLI above;
        # if anything in that chain went sideways, we still surface the
        # AgentInfo so the user sees the bucket in the sidebar.
        try:
            profile_dir = profiles_dir / agent_id
            if profile_dir.is_dir() and display_name and display_name != agent_id:
                (profile_dir / ".xo_display_name").write_text(display_name + "\n")
        except Exception:
            pass

        return _agent_info_hermes(agent_id)

    # OpenClaw agent (default) — dispatches through the adapter (Phase 5).
    from services.cowork_agent.dispatcher import AgentDispatcher
    try:
        return await AgentDispatcher("openclaw").create_agent(body.model_dump())
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except FileExistsError as e:
        return JSONResponse(status_code=409, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    detail = get_agent_detail(agent_id)
    if not detail:
        return JSONResponse(status_code=404, content={"detail": f'Agent "{agent_id}" not found'})
    return detail


def _run_hermes_profile_cli(argv: list[str]) -> tuple[int, str]:
    """Run a hermes CLI command synchronously, return ``(returncode, output)``.

    Used by profile create / delete / rename. argv is built from trusted
    pieces (manifest binary + normalized id), so no shell interpolation.
    """
    import subprocess
    from services.cowork_agent.agent_registry import get_agent

    manifest = get_agent("hermes")
    try:
        result = subprocess.run(
            argv,
            cwd=str(manifest.cwd),
            capture_output=True,
            text=True,
            timeout=manifest.cli_timeout_seconds,
        )
    except FileNotFoundError:
        return -1, "hermes CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return -1, "hermes CLI timed out"
    except Exception as e:  # noqa: BLE001
        return -1, f"hermes CLI failed: {e}"
    output = (result.stderr or result.stdout or "").strip()[:500]
    return result.returncode, output


def _hermes_profile_dir(profile_id: str) -> Path:
    """Return the on-disk path for a hermes profile (used for collision checks)."""
    from services.cowork_agent.agent_registry import get_agent
    return get_agent("hermes").agents_dir / profile_id


@router.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str):
    """Delete an agent. Currently only supports hermes profiles —
    openclaw / claude_code don't have a delete contract today and we
    don't want to invent one silently. Hermes profile deletion shells
    out to ``hermes profile delete -y <id>``, which removes the entire
    profile directory (state.db, skills, sessions). The ``default``
    profile is rejected — that's hermes's root.
    """
    from services.cowork_agent.hermes_state_db import list_all_profile_names
    # Local import: the module-level ``get_agent`` is shadowed by the
    # ``GET /api/agents/{agent_id}`` route handler at line ~691, so calling
    # bare ``get_agent("hermes")`` here would invoke the route handler and
    # blow up with ``'JSONResponse' object has no attribute 'binary'``.
    from services.cowork_agent.agent_registry import get_agent as registry_get_agent

    aid = normalize_agent_id(agent_id)

    if aid == "default":
        return JSONResponse(status_code=400, content={"detail": '"default" profile cannot be deleted.'})

    if aid not in list_all_profile_names():
        # Not a known hermes profile — fall through with a clear message
        # so the user knows we currently only delete hermes profiles.
        return JSONResponse(
            status_code=404,
            content={"detail": f'No hermes profile named "{aid}". Delete is currently only supported for hermes profiles.'},
        )

    rc, output = _run_hermes_profile_cli(
        [registry_get_agent("hermes").binary, "profile", "delete", "-y", aid]
    )
    if rc != 0:
        return JSONResponse(
            status_code=500,
            content={"detail": f"hermes profile delete exited {rc}: {output}"},
        )
    return {"ok": True, "id": aid}


@router.patch("/api/agents/{agent_id}")
async def patch_agent(agent_id: str, body: UpdateAgentBody):
    aid = normalize_agent_id(agent_id)

    # Hermes profiles: only ``name`` is meaningful — it's the rename target.
    # workspace / model / identity_* don't apply to hermes profiles (they
    # don't carry per-profile workspaces or identity files the way openclaw
    # agents do). Other fields are silently ignored to keep the PATCH
    # contract permissive.
    from services.cowork_agent.hermes_state_db import list_all_profile_names

    if aid in list_all_profile_names() and aid != "default":
        if not body.model_fields_set:
            return _agent_info_hermes(aid)

        new_name = (body.name or "").strip() if body.name is not None else ""
        if new_name and new_name != aid:
            new_id = normalize_agent_id(new_name)
            if new_id == "default":
                return JSONResponse(status_code=400, content={"detail": '"default" is reserved.'})
            if _hermes_profile_dir(new_id).is_dir():
                return JSONResponse(status_code=409, content={"detail": f'Hermes profile "{new_id}" already exists.'})

            from services.cowork_agent.agent_registry import get_agent
            rc, output = _run_hermes_profile_cli(
                [get_agent("hermes").binary, "profile", "rename", aid, new_id]
            )
            if rc != 0:
                return JSONResponse(
                    status_code=500,
                    content={"detail": f"hermes profile rename exited {rc}: {output}"},
                )
            return _agent_info_hermes(new_id)

        # description-only update → stamp the sidecar; no CLI call needed.
        if body.description is not None:
            try:
                _hermes_profile_dir(aid).mkdir(parents=True, exist_ok=True)
                (_hermes_profile_dir(aid) / ".xo_description").write_text((body.description or "").strip() + "\n")
            except Exception:
                pass

        # system_prompt → writes the profile's SOUL.md. Hermes reads this on
        # gateway startup, so the FE should prompt for a gateway restart
        # after a successful write (same pattern as channel/provider edits).
        if body.system_prompt is not None:
            try:
                profile_dir = _hermes_profile_dir(aid)
                profile_dir.mkdir(parents=True, exist_ok=True)
                (profile_dir / "SOUL.md").write_text(body.system_prompt)
            except Exception as e:  # noqa: BLE001
                return JSONResponse(
                    status_code=500,
                    content={"detail": f"failed to write SOUL.md: {e}"},
                )

        return _agent_info_hermes(aid)

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

    # OpenClaw agent — dispatch through the adapter (Phase 5).
    if not (AGENTS_DIR / aid).is_dir():
        return JSONResponse(status_code=404, content={"detail": f'Agent "{aid}" not found'})
    if not body.model_fields_set:
        # No-op patch: return current detail. get_agent_detail handles the
        # OpenClaw branch directly today; Phase 6 may swap it to dispatcher.
        detail = get_agent_detail(aid)
        return detail if detail else JSONResponse(status_code=404, content={"detail": "Not found"})

    from services.cowork_agent.dispatcher import AgentDispatcher
    try:
        await AgentDispatcher("openclaw").update_agent(
            aid, body.model_dump(exclude_unset=True)
        )
    except KeyError as e:
        return JSONResponse(status_code=404, content={"detail": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

    # Return the full detail snapshot (richer than what update_agent returns).
    detail = get_agent_detail(aid)
    return detail if detail else JSONResponse(status_code=500, content={"detail": "Failed to read agent after update"})
