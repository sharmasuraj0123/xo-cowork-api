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
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.cowork_agent.settings import AGENTS_DIR, OPENCLAW_MODEL_CAPABILITIES
from services.cowork_agent.agent_registry import get_default_agent
from services.cowork_agent.helpers import _mask_sensitive, normalize_agent_id
from services.cowork_agent.openclaw_env import upsert_env_entry
from services.cowork_agent.openclaw_store import list_agent_entries, load_openclaw_config

router = APIRouter()

_AGENT = get_default_agent()


async def _run_provider_provisioning(provider_id: str, argvs: list[list[str]]) -> None:
    """Run the agent's CLI chain for a provider, appending output to the log.

    Argvs are pre-rendered from the manifest's command templates — no
    user input is interpolated, so `create_subprocess_exec` is safe.
    Chain aborts on the first non-zero exit so a broken `models set`
    doesn't leave later aliases pointing at nothing.
    """
    log_path = _AGENT.provisioning_log
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
                    cwd=str(_AGENT.cwd),
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


@router.get("/api/models")
def list_models():
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
