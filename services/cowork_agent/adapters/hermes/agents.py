"""
Hermes agents capability.

Implements the uniform agents contract (same surface every adapter exposes):

  list_agents()              -> list[dict]
  create_agent(body)         -> dict | JSONResponse
  get_detail(agent_id)       -> dict | None    # None if not ours
  patch(agent_id, body)      -> resp | None     # None if not ours
  delete(agent_id)           -> resp | None     # None if not ours

A hermes "agent" is a profile (``~/.hermes/profiles/<id>/``, with the special
``default`` profile living at ``~/.hermes/`` itself). Lifecycle is delegated to
the ``hermes profile …`` CLI. These functions are invoked by the core router's
ownership iteration for ANY active agent, so they anchor to
``get_agent("hermes")`` explicitly rather than the active agent.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.responses import JSONResponse

from services.cowork_agent.registry.agent_registry import get_agent
from services.cowork_agent.helpers import normalize_agent_id


def _agent_info(profile_name: str) -> dict:
    """xo-cowork AgentInfo shape for a hermes profile.

    Hermes profiles are independent state DBs under
    ``~/.hermes/profiles/<name>/state.db`` — *not* workspace directories.
    They don't map to a single project folder, so ``workspace`` stays empty.
    Frontend routing should read ``metadata.backend`` directly when this
    agent is selected (don't derive backend from workspaceDirectory for
    hermes — multiple profiles would collide on the same path).

    ``sessions_count`` is included so the sidebar can show an authoritative
    count without falling back to "loaded so far" pagination grouping
    (which under-counts and bucks everything unknown under "default").
    """
    from services.cowork_agent.adapters.hermes.paths import HERMES_DIR

    hermes_manifest = get_agent("hermes")
    profile_dir = HERMES_DIR if profile_name == "default" else hermes_manifest.agents_dir / profile_name
    sessions_count = 0
    state_db = profile_dir / "state.db"
    if state_db.is_file():
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
                sessions_count = int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception:
            sessions_count = 0

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
            "sessions_count": sessions_count,
        },
    }


def _detail(profile_name: str) -> dict:
    """Full agent snapshot for a hermes profile, parallel to the openclaw
    branch. Surfaces only what xo-cowork can read cheaply from disk: the
    profile dir, SOUL.md preview, .env keys (no values), session count, and
    gateway pool entry. The fine-grained per-profile edits live under
    ``/api/agents/hermes/{profile}/...`` so the FE can fetch what it needs.
    """
    from services.cowork_agent.adapters.hermes import gateway_pool
    from services.cowork_agent.adapters.hermes.paths import HERMES_DIR

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

    # Session and message counts — read from this profile's state.db only
    # (don't walk every profile; the global state_db helpers are
    # aggregate). A hermes session represents one continuing conversation;
    # message_count tracks turns within it, so surfacing both prevents the
    # "I chatted 7 times but it shows 1 session" confusion.
    session_count = 0
    message_count = 0
    state_db = profile_dir / "state.db"
    if state_db.is_file():
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
                session_count = int(row[0]) if row else 0
                row = conn.execute(
                    "SELECT COALESCE(SUM(message_count), 0) FROM sessions"
                ).fetchone()
                message_count = int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception:
            session_count = 0
            message_count = 0

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
            "message_count": message_count,
            "session_ids": [],
        },
        "openclaw_global_auth": {},
        "backend": "hermes",
        "hermes_profile": profile_name,
    }


def _run_profile_cli(argv: list[str]) -> tuple[int, str]:
    """Run a hermes CLI command synchronously, return ``(returncode, output)``.

    Used by profile create / delete / rename. argv is built from trusted
    pieces (manifest binary + normalized id), so no shell interpolation.
    """
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


def _profile_dir(profile_id: str) -> Path:
    """Return the on-disk path for a hermes profile (used for collision checks)."""
    return get_agent("hermes").agents_dir / profile_id


# ── Uniform agents contract ───────────────────────────────────────────────────


def list_agents() -> list[dict]:
    """Sidebar agents: one per hermes profile."""
    from services.cowork_agent.adapters.hermes.state_db import list_all_profile_names
    return [_agent_info(profile_name) for profile_name in list_all_profile_names()]


def create_agent(body) -> dict | JSONResponse:
    """Create a hermes profile via ``hermes profile create``."""
    display_name = body.name.strip()
    agent_id = normalize_agent_id((body.id or body.name).strip())

    # Hermes profiles are managed by the hermes CLI (`hermes profile create`).
    # Profile id == sidebar bucket name; the on-disk layout is
    # ``~/.hermes/profiles/<id>/`` with its own state.db once the first chat
    # happens. We delegate creation to the CLI so future hermes changes (extra
    # directories, schema bumps) don't drift here.
    hermes_manifest = get_agent("hermes")
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
    # ``.xo_display_name`` sidecar so future frontend renames have a place to
    # read from. The profile dir is created by the CLI above; if anything in
    # that chain went sideways, we still surface the AgentInfo so the user sees
    # the bucket in the sidebar.
    try:
        profile_dir = profiles_dir / agent_id
        if profile_dir.is_dir() and display_name and display_name != agent_id:
            (profile_dir / ".xo_display_name").write_text(display_name + "\n")
    except Exception:
        pass

    return _agent_info(agent_id)


def get_detail(agent_id: str) -> dict | None:
    """Full hermes profile snapshot, or None if ``agent_id`` isn't a profile."""
    from services.cowork_agent.adapters.hermes.state_db import list_all_profile_names
    aid = normalize_agent_id(agent_id)
    # Profile name == agent_id (1:1 by design). Look up via the state-db helper
    # — the same code path that powers /api/agents listing — so GET /api/agents
    # never returns a profile that GET /api/agents/{id} 404s on.
    if aid in list_all_profile_names():
        return _detail(aid)
    return None


def patch(agent_id: str, body) -> dict | JSONResponse | None:
    """Patch a hermes profile (rename / description / SOUL.md); None if not ours.

    Only ``name`` is the rename target; ``workspace``/``model``/``identity_*``
    don't apply to hermes profiles. The ``default`` profile is not patchable
    here (falls through to other backends)."""
    from services.cowork_agent.adapters.hermes.state_db import list_all_profile_names
    aid = normalize_agent_id(agent_id)

    if aid not in list_all_profile_names() or aid == "default":
        return None

    if not body.model_fields_set:
        return _agent_info(aid)

    new_name = (body.name or "").strip() if body.name is not None else ""
    if new_name and new_name != aid:
        new_id = normalize_agent_id(new_name)
        if new_id == "default":
            return JSONResponse(status_code=400, content={"detail": '"default" is reserved.'})
        if _profile_dir(new_id).is_dir():
            return JSONResponse(status_code=409, content={"detail": f'Hermes profile "{new_id}" already exists.'})

        rc, output = _run_profile_cli(
            [get_agent("hermes").binary, "profile", "rename", aid, new_id]
        )
        if rc != 0:
            return JSONResponse(
                status_code=500,
                content={"detail": f"hermes profile rename exited {rc}: {output}"},
            )
        return _agent_info(new_id)

    # description-only update → stamp the sidecar; no CLI call needed.
    if body.description is not None:
        try:
            _profile_dir(aid).mkdir(parents=True, exist_ok=True)
            (_profile_dir(aid) / ".xo_description").write_text((body.description or "").strip() + "\n")
        except Exception:
            pass

    # system_prompt → writes the profile's SOUL.md. Hermes reads this on
    # gateway startup, so the FE should prompt for a gateway restart after a
    # successful write (same pattern as channel/provider edits).
    if body.system_prompt is not None:
        try:
            profile_dir = _profile_dir(aid)
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / "SOUL.md").write_text(body.system_prompt)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={"detail": f"failed to write SOUL.md: {e}"},
            )

    return _agent_info(aid)


def delete(agent_id: str) -> dict | JSONResponse | None:
    """Delete a hermes profile via ``hermes profile delete -y <id>``.

    The ``default`` profile is rejected (it's hermes's root). Returns None for
    ids that aren't hermes profiles so the router can try other backends.
    """
    from services.cowork_agent.adapters.hermes.state_db import list_all_profile_names
    aid = normalize_agent_id(agent_id)

    if aid == "default":
        return JSONResponse(status_code=400, content={"detail": '"default" profile cannot be deleted.'})

    if aid not in list_all_profile_names():
        return None

    rc, output = _run_profile_cli(
        [get_agent("hermes").binary, "profile", "delete", "-y", aid]
    )
    if rc != 0:
        return JSONResponse(
            status_code=500,
            content={"detail": f"hermes profile delete exited {rc}: {output}"},
        )
    return {"ok": True, "id": aid}
