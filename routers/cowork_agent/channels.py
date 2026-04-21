"""
Channel provisioning endpoints.

The onboarding "Channels" step (and later the Settings → Channels tab) POSTs
a platform id + bot tokens here. This module:

1. Upserts the platform's tokens into `~/.openclaw/.env` (line-level,
   comment-preserving) via `openclaw_env.upsert_env_entry`.
2. Schedules `openclaw config set --batch-json <recipe>` (followed by any
   entries in `post_commands`) as a **background task** — the handler does
   not wait on the CLI. This mirrors the provider-key flow and keeps the UI
   responsive: the user gets a "Connected" + "Restart Gateway" verdict
   instantly, and stdout/stderr for each step lands in
   `~/.openclaw/bridge-provisioning.log` for diagnosis. Tokens are always
   referenced by env-var name in the batch (`"ref": {...}`) — their values
   never enter argv.
3. Returns `{ok: true, restart_required: true}` once the `.env` write
   succeeds, so the frontend can flip the row to Connected and show the
   gateway-restart notice immediately.

`GET /api/channels` (empty-list stub) lives in `routes/misc.py` and is
unchanged.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.cowork_agent.openclaw_env import upsert_env_entry

router = APIRouter()


# ── Per-platform recipe ──────────────────────────────────────────────────────

# `fields`        — mapping from the frontend field name (as sent in the POST
#                   body by `onboarding-screen.tsx`) to the `.env` variable
#                   the openclaw config references.
# `config_batch`  — the `--batch-json` payload. Empty list = skip the batch
#                   step.
# `post_commands` — list of full argv lists, each run sequentially after the
#                   batch. Tokens are never here either (only env-var names).
CHANNEL_PROVISIONING: dict[str, dict] = {
    "slack": {
        "fields": {
            "bot_token": "SLACK_BOT_TOKEN",
            "app_token": "SLACK_APP_TOKEN",
        },
        "config_batch": [
            {"path": "channels.slack.mode",        "value": "socket"},
            {"path": "channels.slack.enabled",     "value": True},
            {"path": "channels.slack.dmPolicy",    "value": "open"},
            {"path": "channels.slack.allowFrom",   "value": ["*"]},
            {"path": "channels.slack.groupPolicy", "value": "open"},
        ],
        "post_commands": [
            ["openclaw", "config", "set", "plugins.entries.slack.enabled", "true"],
        ],
    },
    "telegram": {
        "fields": {
            "token": "TELEGRAM_BOT_TOKEN",
        },
        "config_batch": [
            {"path": "channels.telegram.enabled",     "value": True},
            {"path": "channels.telegram.dmPolicy",    "value": "open"},
            {"path": "channels.telegram.allowFrom",   "value": ["*"]},
            {"path": "channels.telegram.groupPolicy", "value": "allowlist"},
            {"path": "channels.telegram.streaming",   "value": {"mode": "partial"}},
        ],
        "post_commands": [
            ["openclaw", "config", "set", "plugins.entries.telegram.enabled", "true"],
        ],
    },
    "discord": {
        "fields": {
            "token": "DISCORD_BOT_TOKEN",
        },
        "config_batch": [
            {"path": "channels.discord.enabled",     "value": True},
            {"path": "channels.discord.groupPolicy", "value": "open"},
            {"path": "channels.discord.streaming",   "value": {"mode": "partial"}},
            {"path": "channels.discord.dmPolicy",    "value": "open"},
            {"path": "channels.discord.allowFrom",   "value": ["*"]},
            {"path": "channels.discord.dm",          "value": {"enabled": True}},
        ],
        "post_commands": [
            ["openclaw", "config", "set", "plugins.entries.discord.enabled", "true"],
        ],
    },
}


_OPENCLAW_CWD = Path("/home/coder")
_PROVISIONING_LOG = Path.home() / ".openclaw" / "bridge-provisioning.log"
_CLI_TIMEOUT_SECONDS = 30


async def _run_one(platform: str, argv: list[str]) -> int:
    """Run one openclaw argv, append stdout+stderr to the provisioning log,
    and return the exit code (or a sentinel string-ish code on failure).

    The return value is only used to decide whether to continue the chain —
    it is never surfaced to the UI.
    """
    _PROVISIONING_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(_OPENCLAW_CWD),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_CLI_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            _append_log(ts, platform, argv, f"[timed out after {_CLI_TIMEOUT_SECONDS}s]", rc="timeout")
            return -1
    except FileNotFoundError:
        _append_log(ts, platform, argv, "openclaw CLI not found in PATH", rc="missing-binary")
        return -1
    except Exception as e:  # noqa: BLE001 — log and carry on
        _append_log(ts, platform, argv, f"[exception] {e}", rc="exception")
        return -1

    _append_log(ts, platform, argv, stdout.decode(errors="replace"), rc=str(proc.returncode))
    return proc.returncode


async def _run_provisioning_bg(platform: str, recipe: dict) -> None:
    """Background provisioning chain: batch first, then each post-command.

    Aborts on the first non-zero exit so we don't enable a plugin whose
    config failed to write. No return value — the only observer is the
    provisioning log.
    """
    batch = recipe.get("config_batch") or []
    if batch:
        batch_argv = ["openclaw", "config", "set", "--batch-json", json.dumps(batch)]
        rc = await _run_one(platform, batch_argv)
        if rc != 0:
            _note_chain_aborted(platform, "batch")
            return

    for argv in recipe.get("post_commands") or []:
        rc = await _run_one(platform, list(argv))
        if rc != 0:
            _note_chain_aborted(platform, " ".join(argv))
            return


def _note_chain_aborted(platform: str, where: str) -> None:
    with _PROVISIONING_LOG.open("a") as log:
        log.write(f"[chain aborted for {platform} at: {where}]\n")


def _append_log(ts: str, platform: str, argv: list[str], output: str, rc: str) -> None:
    """Append one provisioning run to the shared log file. Tokens are never
    in argv (the batch uses `ref: {id: ENV_NAME}`), so logging the full
    argv is safe."""
    with _PROVISIONING_LOG.open("a") as log:
        log.write(f"\n=== {ts} channel-provisioning: {platform} ===\n")
        log.write(f"$ {' '.join(repr(a) if ' ' in a else a for a in argv)}\n")
        log.write(output)
        if not output.endswith("\n"):
            log.write("\n")
        log.write(f"[exit {rc}]\n")


@router.post("/api/channels/add")
async def add_channel(request: Request):
    """Onboarding + Settings → Channels target.

    Body: `{platform: "slack"|"telegram"|"discord", <tokens>...}`. Writes
    tokens to `~/.openclaw/.env` synchronously, then (if the platform has a
    CLI recipe) spawns `_run_provisioning_bg` and returns immediately with
    `restart_required: true` so the UI can show the gateway-restart notice.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "Body must be an object"})

    platform = (body.get("platform") or "").strip().lower()
    recipe = CHANNEL_PROVISIONING.get(platform)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported platform: {platform or '(missing)'}"},
        )

    # Collect + validate every required token before touching disk — we don't
    # want to write half the .env, then fail on a missing second token.
    to_upsert: list[tuple[str, str]] = []
    for field, env_key in recipe["fields"].items():
        value = (body.get(field) or "").strip() if isinstance(body.get(field), str) else ""
        if not value:
            return JSONResponse(
                status_code=400,
                content={"detail": f"{platform} is missing required field: {field}"},
            )
        to_upsert.append((env_key, value))

    try:
        for env_key, value in to_upsert:
            upsert_env_entry(env_key, value)
    except Exception as e:  # noqa: BLE001 — surface the real reason
        return JSONResponse(
            status_code=500,
            content={"detail": f"Failed to write .env: {e}"},
        )

    has_cli_chain = bool(recipe.get("config_batch")) or bool(recipe.get("post_commands"))
    if not has_cli_chain:
        return {
            "ok": True,
            "platform": platform,
            "restart_required": False,
            "provisioning": "skipped",
            "detail": f"{platform} token saved. Config automation not configured yet.",
        }

    # Fire-and-forget: the CLI chain runs in the background, its output lands
    # in ~/.openclaw/bridge-provisioning.log. The UI gets the restart notice
    # immediately and stays responsive.
    asyncio.create_task(_run_provisioning_bg(platform, recipe))

    return {
        "ok": True,
        "platform": platform,
        "restart_required": True,
        "provisioning": "started",
    }
