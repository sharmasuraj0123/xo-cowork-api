"""
Configuration / provider / model-list endpoints.

Groups `/api/config/*` (api-key, providers, openclaw, openyak-account,
ollama, local, openai-subscription) and the per-agent model listing
(`/api/models`). Responses here shape what the UI sees in provider menus
and the settings screen.

Provider onboarding recipes now come from the active agent's manifest
(`config/agents/<agent>.json` → `providers.*`) instead of an inline dict,
so adding a provider (or swapping agents) is a config change.
"""

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.cowork_agent.settings import AGENTS_DIR, CLAUDE_COWORK_DIR, OPENCLAW_MODEL_CAPABILITIES, load_agent_config
from services.cowork_agent.agent_registry import get_agent, get_active_agent
from services.cowork_agent.helpers import _mask_sensitive, normalize_agent_id
from services.cowork_agent.adapters.openclaw.env import upsert_env_entry
from services.cowork_agent.hermes_env import upsert_hermes_env_entry
from services.cowork_agent.adapters.openclaw.store import list_agent_entries, load_openclaw_config
from services.cowork_agent.project_layout import xo_projects_root

router = APIRouter()

_AGENT = get_active_agent()
_HERMES = get_agent("hermes")


async def _run_provider_provisioning(provider_id: str, argvs: list[list[str]], manifest=None) -> None:
    """Run an agent's CLI chain for a provider, appending output to the log.

    Argvs are pre-rendered from the manifest's command templates — no
    user input is interpolated, so `create_subprocess_exec` is safe.
    Chain aborts on the first non-zero exit so a broken `models set`
    doesn't leave later aliases pointing at nothing.

    ``manifest`` controls which agent's provisioning log + cwd are used.
    Defaults to the active default agent for backward compatibility with
    the openclaw route; the hermes route passes ``_HERMES`` so each
    backend's provisioning log stays separate.
    """
    target = manifest if manifest is not None else _AGENT
    log_path = target.provisioning_log
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with log_path.open("a") as log:
        log.write(f"\n=== {ts} provisioning: {provider_id} ===\n")
        for argv in argvs:
            log.write(f"$ {' '.join(argv)}\n")
            log.flush()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(target.cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await proc.communicate()
                log.write(stdout.decode(errors="replace"))
                log.write(f"[exit {proc.returncode}]\n")
                if proc.returncode != 0:
                    log.write("[chain aborted]\n")
                    return
            except Exception as e:  # noqa: BLE001 — log every failure, keep going
                log.write(f"[exception] {e}\n[chain aborted]\n")
                return
        log.write("[chain ok]\n")


@router.post("/api/config/providers/{provider_id}/key")
async def save_provider_key(provider_id: str, request: Request):
    """Persist a provider API key and kick off the agent's CLI chain.

    Returns 200 as soon as the key is safely written to the agent's env
    file. The CLI chain runs in the background; its output is appended to
    the agent's provisioning log for later inspection.
    """
    recipe = _AGENT.providers.get(provider_id)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported provider: {provider_id}"},
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    api_key = (body.get("api_key") or "").strip() if isinstance(body, dict) else ""
    if not api_key:
        return JSONResponse(status_code=400, content={"detail": "api_key is required"})

    try:
        upsert_env_entry(recipe["env_key"], api_key)
    except Exception as e:  # noqa: BLE001 — surface the real reason to the UI
        return JSONResponse(status_code=500, content={"detail": f"Failed to save key: {e}"})

    try:
        argvs = _AGENT.render_recipe_commands(recipe.get("commands") or [])
    except (KeyError, ValueError) as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Invalid provider recipe for '{provider_id}': {e}"},
        )

    asyncio.create_task(_run_provider_provisioning(provider_id, argvs))

    return {"ok": True, "provider": provider_id, "provisioning": "started"}


@router.get("/api/config/hermes")
def get_hermes_config():
    """Return ``~/.hermes/config.yaml`` parsed to JSON with sensitive fields masked.

    Mirrors ``/api/config/openclaw`` but anchored to the hermes manifest's
    ``config_file``. A hermes-named route must always read hermes — never
    the active default — so the foot-gun the settings re-anchoring closed
    stays closed here too. Returns 404 if the file doesn't exist yet
    (fresh install before ``hermes setup`` runs).
    """
    import yaml

    config_path = _HERMES.config_file
    if not config_path.exists():
        return JSONResponse(
            status_code=404,
            content={"detail": f"{config_path.name} not found at {config_path}"},
        )

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Failed to parse {config_path.name}: {e}"},
        )

    if not isinstance(raw, dict):
        return JSONResponse(
            status_code=500,
            content={"detail": f"Unexpected top-level shape in {config_path.name}: {type(raw).__name__}"},
        )

    return _mask_sensitive(raw)


@router.post("/api/config/hermes/providers/{provider_id}/key")
async def save_hermes_provider_key(provider_id: str, request: Request):
    """Persist a provider API key into ``~/.hermes/.env`` and (optionally)
    kick off the hermes CLI follow-up chain declared in the manifest.

    Mirrors ``/api/config/providers/{provider_id}/key`` but anchored to the
    hermes manifest specifically (``get_agent("hermes")``) — never to the
    active agent. A hermes-named route must always write to
    ``~/.hermes/.env`` regardless of ``AGENT_NAME``, same rule we
    applied to the OPENCLAW_* constants. Provider list lives in
    ``config/agents/hermes/commands.json`` → ``providers.*``.

    Returns 200 once the key is written; any CLI follow-up runs in the
    background and is logged to ``~/.hermes/provisioning.log``.
    """
    recipe = _HERMES.providers.get(provider_id)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported hermes provider: {provider_id}"},
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    api_key = (body.get("api_key") or "").strip() if isinstance(body, dict) else ""
    if not api_key:
        return JSONResponse(status_code=400, content={"detail": "api_key is required"})

    try:
        upsert_hermes_env_entry(recipe["env_key"], api_key)
    except Exception as e:  # noqa: BLE001 — surface the real reason to the UI
        return JSONResponse(status_code=500, content={"detail": f"Failed to save key: {e}"})

    try:
        argvs = _HERMES.render_recipe_commands(recipe.get("commands") or [])
    except (KeyError, ValueError) as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Invalid hermes provider recipe for '{provider_id}': {e}"},
        )

    if argvs:
        asyncio.create_task(_run_provider_provisioning(provider_id, argvs, manifest=_HERMES))

    return {"ok": True, "provider": provider_id, "env_key": recipe["env_key"], "provisioning": "started" if argvs else "skipped"}


# ── Model listing ────────────────────────────────────────────────────────────


def list_openclaw_models() -> list[dict]:
    """One model row per agent entry so the UI can target `<prefix>/<agentId>`."""
    cfg = load_openclaw_config()
    entries_by_id = {
        normalize_agent_id(str(e.get("id", ""))): e
        for e in list_agent_entries(cfg)
        if e.get("id")
    }
    models: list[dict] = []
    seen: set[str] = set()
    prefix = _AGENT.model_prefix

    if AGENTS_DIR.exists():
        for agent_dir in sorted(AGENTS_DIR.iterdir()):
            if not agent_dir.is_dir():
                continue
            aid = normalize_agent_id(agent_dir.name)
            seen.add(aid)
            meta = entries_by_id.get(aid, {})
            display = meta.get("name") if isinstance(meta.get("name"), str) else None
            label = (display or "").strip() or aid
            models.append(
                {
                    "id": f"{prefix}/{aid}",
                    "name": label,
                    "provider_id": _AGENT.name,
                    "capabilities": dict(OPENCLAW_MODEL_CAPABILITIES),
                    "pricing": {"prompt": 0, "completion": 0},
                    "metadata": {"openclaw_agent_id": aid},
                }
            )

    if not models:
        models.append(
            {
                "id": f"{prefix}/main",
                "name": "main",
                "provider_id": _AGENT.name,
                "capabilities": dict(OPENCLAW_MODEL_CAPABILITIES),
                "pricing": {"prompt": 0, "completion": 0},
                "metadata": {"openclaw_agent_id": "main"},
            }
        )

    return models


def list_hermes_models() -> list[dict]:
    """One model row per hermes profile under ``~/.hermes/profiles/``.

    Mirrors :func:`list_openclaw_models` but reads from the hermes
    state-db helper rather than scanning openclaw's agents dir — those
    two layouts are independent and listing the openclaw dir under
    hermes was producing 2 stale model rows for 4 real hermes profiles.
    """
    from services.cowork_agent.hermes_state_db import list_all_profile_names

    prefix = _HERMES.model_prefix
    models: list[dict] = []
    for profile_name in list_all_profile_names():
        models.append(
            {
                "id": f"{prefix}/{profile_name}",
                "name": profile_name,
                "provider_id": _HERMES.name,
                "capabilities": dict(OPENCLAW_MODEL_CAPABILITIES),
                "pricing": {"prompt": 0, "completion": 0},
                "metadata": {"hermes_profile": profile_name},
            }
        )
    if not models:
        # Fresh install — surface at least one row so the dropdown isn't
        # blank before the user creates a profile.
        models.append(
            {
                "id": f"{prefix}/default",
                "name": "default",
                "provider_id": _HERMES.name,
                "capabilities": dict(OPENCLAW_MODEL_CAPABILITIES),
                "pricing": {"prompt": 0, "completion": 0},
                "metadata": {"hermes_profile": "default"},
            }
        )
    return models


@router.get("/api/models")
def list_models():
    """Return one model row per agent/profile under the active backend.

    Backend dispatch:
    - ``AGENT_NAME=hermes`` → :func:`list_hermes_models` (scans
      ``~/.hermes/profiles/``).
    - everything else → :func:`list_openclaw_models` (scans
      ``~/.openclaw/agents/``, also covers claude_code).
    """
    if os.getenv("AGENT_NAME", "openclaw") == "hermes":
        return list_hermes_models()
    return list_openclaw_models()


# ── /api/config/* routes ─────────────────────────────────────────────────────


@router.get("/api/config/api-key")
def config_api_key():
    return {"has_key": True, "provider": _AGENT.name}


@router.get("/api/config/providers")
def config_providers():
    return []


@router.get("/api/config/openai-subscription")
def openai_subscription():
    return {"is_connected": False, "email": "", "needs_reauth": False}


@router.get("/api/config/openyak-account")
def openyak_account():
    return {"linked": False}


@router.get("/api/config/ollama")
def ollama_config():
    return {"installed": False}


@router.get("/api/config/local")
def local_provider():
    return {"available": False}


@router.get("/api/config/openclaw")
def get_openclaw_config():
    """Return the full agent config file (e.g. openclaw.json) with sensitive fields masked."""
    cfg = load_openclaw_config()
    if not cfg:
        return JSONResponse(status_code=404, content={"detail": f"{_AGENT.config_file.name} not found"})
    return _mask_sensitive(cfg)


@router.get("/api/config/agents/{name}")
def get_agent_config(name: str):
    """Unified per-agent config loader the FE Settings → Config tab calls.

    Returns ``{agent, config_file, config}`` where ``config`` is the parsed
    config file (json for openclaw, yaml for hermes) with sensitive values
    masked. The FE refresh button hits this route — without it both
    backends 404 and the panel hangs at "Failed to load configuration".

    Dispatch by manifest name so we don't accidentally re-anchor to the
    active default agent (same rule that protects the hermes-named routes).
    """
    try:
        manifest = get_agent(name)
    except (KeyError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unknown agent backend: {name}"},
        )

    config_path = manifest.config_file
    if not config_path.exists():
        return JSONResponse(
            status_code=404,
            content={"detail": f"{config_path.name} not found at {config_path}"},
        )

    suffix = config_path.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            import yaml
            raw = yaml.safe_load(config_path.read_text()) or {}
        else:
            import json as _json
            raw = _json.loads(config_path.read_text() or "{}")
    except (OSError, ValueError) as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Failed to read {config_path}: {e}"},
        )

    if not isinstance(raw, dict):
        return JSONResponse(
            status_code=500,
            content={"detail": f"Unexpected top-level shape in {config_path.name}: {type(raw).__name__}"},
        )

    return {
        "agent": name,
        "config_file": str(config_path),
        "config": _mask_sensitive(raw),
    }


@router.get("/api/config/workspace")
def get_workspace_config():
    """
    Return workspace root paths for each agent backend.

    The unified ``xo-projects`` root (``XO_PROJECTS_ROOT`` env, default
    ``~/xo-projects``) is exposed under whichever backend is active so the
    new-project picker keeps working. We also surface each installed
    backend's native home so the frontend's
    ``agentNameForWorkspace(workspaceDirectory)`` lookup can recognize
    backend-specific paths like ``~/.openclaw/agents/<id>`` and route
    chats accordingly — without that, every chat falls back to the
    active backend's default agent regardless of which sidebar agent
    the user picked.

    Response shape:
      {
        "roots": {
           "<active_backend>": "/home/coder/xo-projects",
           "openclaw": "/home/coder/.openclaw",
           "hermes": "/home/coder/.hermes",
           ...
        },
        "default": "<active_backend>"
      }
    """
    from services.cowork_agent.agent_registry import _discover_manifests

    projects_root = str(xo_projects_root())
    default_backend = os.getenv("AGENT_NAME", _AGENT.name)

    # Start with the active backend rooted at xo-projects (legacy contract).
    roots: dict[str, str] = {default_backend: projects_root}

    # Add each installed backend's native home so the frontend can match
    # backend-specific paths (~/.openclaw/agents/<id>, ~/.hermes/profiles/<name>).
    for name, manifest in _discover_manifests().items():
        # Don't overwrite the active backend's xo-projects entry.
        roots.setdefault(name, str(manifest.home_dir))

    return {"roots": roots, "default": default_backend}
