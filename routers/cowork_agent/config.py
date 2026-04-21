"""
Configuration / provider / model-list endpoints.

Groups `/api/config/*` (api-key, providers, openclaw, openyak-account,
ollama, local, openai-subscription) and the per-agent model listing
(`/api/models`). Responses here shape what the UI sees in provider menus
and the settings screen.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.cowork_agent.settings import AGENTS_DIR, OPENCLAW_MODEL_CAPABILITIES
from services.cowork_agent.helpers import _mask_sensitive, normalize_agent_id
from services.cowork_agent.openclaw_env import upsert_env_entry
from services.cowork_agent.openclaw_store import list_agent_entries, load_openclaw_config

router = APIRouter()


# ── Provider onboarding ──────────────────────────────────────────────────────

# Per-provider recipe used by `POST /api/config/providers/{id}/key`:
#   env_key      — the variable name the provider's SDK reads
#   commands     — shell commands run sequentially after the key is written.
#                  Executed via /bin/sh in `openclaw_cwd`. No user input is
#                  ever interpolated into these strings — every value is a
#                  fixed model/alias identifier — so there is no command-
#                  injection surface even with `shell=True`.
PROVIDER_PROVISIONING: dict[str, dict] = {
    "anthropic": {
        "env_key": "ANTHROPIC_API_KEY",
        "commands": [
            'openclaw models set anthropic/claude-opus-4.6',
        ],
    },
    "openai": {
        "env_key": "OPENAI_API_KEY",
        "commands": [
            'openclaw models set openai/gpt-5.4',
        ],
    },
}

_OPENCLAW_CWD = Path("/home/coder")
_PROVISIONING_LOG = Path.home() / ".openclaw" / "bridge-provisioning.log"


async def _run_provider_provisioning(provider_id: str, commands: list[str]) -> None:
    """Run the openclaw CLI chain for a provider, appending output to a log file.

    Chain aborts on the first non-zero exit — a broken `models set` would
    leave subsequent `config set` aliases pointing at nothing.
    """
    _PROVISIONING_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with _PROVISIONING_LOG.open("a") as log:
        log.write(f"\n=== {ts} provisioning: {provider_id} ===\n")
        for cmd in commands:
            log.write(f"$ {cmd}\n")
            log.flush()
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    cwd=str(_OPENCLAW_CWD),
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
    """Persist a provider API key and kick off the openclaw CLI chain.

    Returns 200 as soon as the key is safely written to `~/.openclaw/.env`.
    The CLI chain runs in the background; its output is appended to
    `~/.openclaw/bridge-provisioning.log` for later inspection.
    """
    recipe = PROVIDER_PROVISIONING.get(provider_id)
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

    asyncio.create_task(_run_provider_provisioning(provider_id, recipe["commands"]))

    return {"ok": True, "provider": provider_id, "provisioning": "started"}


# ── Model listing ────────────────────────────────────────────────────────────


def list_openclaw_models() -> list[dict]:
    """One model row per OpenClaw agent so the UI can target `openclaw/<agentId>`."""
    cfg = load_openclaw_config()
    entries_by_id = {
        normalize_agent_id(str(e.get("id", ""))): e
        for e in list_agent_entries(cfg)
        if e.get("id")
    }
    models: list[dict] = []
    seen: set[str] = set()

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
                    "id": f"openclaw/{aid}",
                    "name": label,
                    "provider_id": "openclaw",
                    "capabilities": dict(OPENCLAW_MODEL_CAPABILITIES),
                    "pricing": {"prompt": 0, "completion": 0},
                    "metadata": {"openclaw_agent_id": aid},
                }
            )

    if not models:
        models.append(
            {
                "id": "openclaw/main",
                "name": "main",
                "provider_id": "openclaw",
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
    return {"has_key": True, "provider": "openclaw"}


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
    """Return the full openclaw.json with sensitive fields masked."""
    cfg = load_openclaw_config()
    if not cfg:
        return JSONResponse(status_code=404, content={"detail": "openclaw.json not found"})
    return _mask_sensitive(cfg)
