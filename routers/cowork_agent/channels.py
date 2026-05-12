"""
Channel provisioning endpoints.

The onboarding "Channels" step (and later the Settings → Channels tab) POSTs
a platform id + bot tokens here. This module:

1. Upserts the platform's tokens into the active agent's env file
   (line-level, comment-preserving) via `openclaw_env.upsert_env_entry`.
2. Schedules the manifest's `config_set_batch` (followed by any entries in
   `post_commands`) as a **background task** — the handler does not wait
   on the CLI. This mirrors the provider-key flow and keeps the UI
   responsive: the user gets a "Connected" + "Restart Gateway" verdict
   instantly, and stdout/stderr for each step lands in the agent's
   provisioning log for diagnosis. Tokens are always referenced by
   env-var name in the batch payload — their values never enter argv.
3. Returns `{ok: true, restart_required: true}` once the env-file write
   succeeds, so the frontend can flip the row to Connected and show the
   gateway-restart notice immediately.

Recipes (fields, config_batch, post_commands) are sourced from
`config/agents/<agent>.json` → `channels.*`, not hardcoded here.
"""

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.cowork_agent.agent_registry import get_agent, get_default_agent
from services.cowork_agent.openclaw_env import upsert_env_entry
from services.cowork_agent.hermes_env import upsert_hermes_env_entry

router = APIRouter()

_AGENT = get_default_agent()
_HERMES = get_agent("hermes")


async def _run_one(platform: str, argv: list[str]) -> int:
    """Run one CLI argv, append stdout+stderr to the provisioning log,
    and return the exit code (or -1 on a tooling failure).

    The return value is only used to decide whether to continue the chain —
    it is never surfaced to the UI.
    """
    log_path = _AGENT.provisioning_log
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(_AGENT.cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_AGENT.cli_timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            _append_log(ts, platform, argv, f"[timed out after {_AGENT.cli_timeout_seconds}s]", rc="timeout")
            return -1
    except FileNotFoundError:
        _append_log(ts, platform, argv, f"{_AGENT.binary} CLI not found in PATH", rc="missing-binary")
        return -1
    except Exception as e:  # noqa: BLE001 — log and carry on
        _append_log(ts, platform, argv, f"[exception] {e}", rc="exception")
        return -1

    _append_log(ts, platform, argv, stdout.decode(errors="replace"), rc=str(proc.returncode))
    return proc.returncode


async def _run_provisioning_bg(platform: str, recipe: dict) -> None:
    """Background provisioning chain: batch first, then each post-command.

    Aborts on the first non-zero exit so we don't enable a plugin whose
    config failed to write.
    """
    batch = recipe.get("config_batch") or []
    if batch:
        batch_argv = _AGENT.command("config_set_batch", batch_json=json.dumps(batch))
        rc = await _run_one(platform, batch_argv)
        if rc != 0:
            _note_chain_aborted(platform, "batch")
            return

    try:
        post_argvs = _AGENT.render_recipe_commands(recipe.get("post_commands") or [])
    except (KeyError, ValueError) as e:
        _note_chain_aborted(platform, f"invalid post_commands: {e}")
        return

    for argv in post_argvs:
        rc = await _run_one(platform, argv)
        if rc != 0:
            _note_chain_aborted(platform, " ".join(argv))
            return


def _note_chain_aborted(platform: str, where: str) -> None:
    with _AGENT.provisioning_log.open("a") as log:
        log.write(f"[chain aborted for {platform} at: {where}]\n")


def _append_log(ts: str, platform: str, argv: list[str], output: str, rc: str) -> None:
    """Append one provisioning run to the shared log file. Tokens are never
    in argv (the batch uses `ref: {id: ENV_NAME}`), so logging the full
    argv is safe."""
    with _AGENT.provisioning_log.open("a") as log:
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
    tokens to the agent's env file synchronously, then (if the platform has
    a CLI recipe) spawns `_run_provisioning_bg` and returns immediately with
    `restart_required: true` so the UI can show the gateway-restart notice.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "Body must be an object"})

    platform = (body.get("platform") or "").strip().lower()
    recipe = _AGENT.channels.get(platform)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported platform: {platform or '(missing)'}"},
        )

    # Collect + validate every required token before touching disk — we don't
    # want to write half the .env, then fail on a missing second token.
    # Fields not supplied in the body fall back to the manifest's ``defaults``
    # map so the FE form doesn't have to collect knobs like ``allowed_users``
    # (which the manifest defaults to ``*``). To restrict access, edit the
    # default in the active agent's ``config/agents/<agent>/commands.json``.
    defaults = recipe.get("defaults") or {}
    to_upsert: list[tuple[str, str]] = []
    for field, env_key in (recipe.get("fields") or {}).items():
        value = (body.get(field) or "").strip() if isinstance(body.get(field), str) else ""
        if not value:
            fallback = defaults.get(field)
            if isinstance(fallback, str) and fallback:
                value = fallback
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
            content={"detail": f"Failed to write env file: {e}"},
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

    asyncio.create_task(_run_provisioning_bg(platform, recipe))

    return {
        "ok": True,
        "platform": platform,
        "restart_required": True,
        "provisioning": "started",
    }


# ── Hermes channel route ─────────────────────────────────────────────────────


async def _run_one_hermes(platform: str, argv: list[str]) -> int:
    """Run one hermes CLI argv against the hermes manifest's cwd and log."""
    log_path = _HERMES.provisioning_log
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(_HERMES.cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_HERMES.cli_timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            with log_path.open("a") as log:
                log.write(f"\n=== {ts} hermes channel: {platform} ===\n$ {' '.join(argv)}\n[timed out]\n")
            return -1
    except FileNotFoundError:
        with log_path.open("a") as log:
            log.write(f"\n=== {ts} hermes channel: {platform} ===\n[hermes CLI not found in PATH]\n")
        return -1
    except Exception as e:  # noqa: BLE001 — log and carry on
        with log_path.open("a") as log:
            log.write(f"\n=== {ts} hermes channel: {platform} ===\n[exception] {e}\n")
        return -1

    with log_path.open("a") as log:
        log.write(f"\n=== {ts} hermes channel: {platform} ===\n$ {' '.join(argv)}\n")
        log.write(stdout.decode(errors="replace"))
        log.write(f"\n[exit {proc.returncode}]\n")
    return proc.returncode


async def _run_hermes_channel_bg(platform: str, recipe: dict) -> None:
    """Run a hermes channel's CLI follow-up chain in order, aborting on first non-zero.

    Unlike openclaw, hermes has no `config_set_batch` — each setting goes
    through one `hermes config set <path> <value>` call. The manifest's
    ``commands`` block is rendered as an ordered argv list.
    """
    try:
        argvs = _HERMES.render_recipe_commands(recipe.get("commands") or [])
    except (KeyError, ValueError) as e:
        with _HERMES.provisioning_log.open("a") as log:
            log.write(f"[hermes channel {platform} aborted: invalid commands: {e}]\n")
        return

    for argv in argvs:
        rc = await _run_one_hermes(platform, argv)
        if rc != 0:
            with _HERMES.provisioning_log.open("a") as log:
                log.write(f"[hermes channel {platform} aborted at: {' '.join(argv)}]\n")
            return


@router.post("/api/channels/hermes/add")
async def add_hermes_channel(request: Request):
    """Hermes channel onboarding (slack, telegram — whatsapp deliberately omitted).

    Mirrors ``/api/channels/add`` but anchored to the hermes manifest:
    every token lands in ``~/.hermes/.env`` (never ``~/.openclaw/.env``),
    and the CLI follow-up uses hermes's own ``cli_timeout_seconds`` and
    provisioning log. A hermes-named route must always target hermes
    regardless of ``DEFAULT_AGENT``.

    Body: ``{platform: "slack"|"telegram", <field>: <value>, ...}``.
    Field names per platform come from the manifest's
    ``channels.<platform>.fields`` map. Missing fields fall back to
    ``channels.<platform>.defaults`` so the FE form doesn't have to
    collect things like ``allowed_users``.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "Body must be an object"})

    platform = (body.get("platform") or "").strip().lower()
    recipe = _HERMES.channels.get(platform)
    if recipe is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported hermes channel: {platform or '(missing)'}"},
        )

    # Collect + validate every field before touching disk so we never write a
    # half-populated env file. Fields not supplied in the body fall back to
    # the manifest's ``defaults`` map — keeps the FE form minimal (e.g. user
    # never has to type "allowed_users: *"). To restrict access, edit the
    # default in ``config/agents/hermes/commands.json``.
    defaults = recipe.get("defaults") or {}
    to_upsert: list[tuple[str, str]] = []
    for field, env_key in (recipe.get("fields") or {}).items():
        value = (body.get(field) or "").strip() if isinstance(body.get(field), str) else ""
        if not value:
            default = defaults.get(field)
            if isinstance(default, str) and default:
                value = default
        if not value:
            return JSONResponse(
                status_code=400,
                content={"detail": f"{platform} is missing required field: {field}"},
            )
        to_upsert.append((env_key, value))

    try:
        for env_key, value in to_upsert:
            upsert_hermes_env_entry(env_key, value)
    except Exception as e:  # noqa: BLE001 — surface the real reason
        return JSONResponse(
            status_code=500,
            content={"detail": f"Failed to write hermes env file: {e}"},
        )

    has_cli_chain = bool(recipe.get("commands"))
    if not has_cli_chain:
        return {
            "ok": True,
            "platform": platform,
            "restart_required": True,
            "provisioning": "skipped",
            "detail": f"{platform} tokens saved. Restart the hermes gateway to pick up the change.",
        }

    asyncio.create_task(_run_hermes_channel_bg(platform, recipe))

    return {
        "ok": True,
        "platform": platform,
        "restart_required": True,
        "provisioning": "started",
    }


# ── Hermes gateway lifecycle ─────────────────────────────────────────────────
#
# The hermes gateway is started by ``./hermes.sh start`` (PID file in /tmp).
# After the user saves a key or channel, ~/.hermes/.env has changed but the
# already-running gateway has the old env baked in — it needs a restart to
# pick up the new values. These routes give the FE a "Restart Gateway"
# button so the user doesn't have to drop to a terminal.
#
# All three lifecycle routes shell out to the same ``hermes.sh`` script
# that ships in this repo. The script handles PID tracking, log rotation,
# orphan cleanup, and the auto-restart loop.

import json as _json
from pathlib import Path as _Path
from urllib.parse import urlparse as _urlparse

_HERMES_SH = _Path(__file__).resolve().parents[2] / "hermes.sh"
_HERMES_GATEWAY_STATE_FILE = _HERMES.home_dir / "gateway_state.json"


def _run_hermes_sh_sync(subcommand: str, timeout_s: float) -> tuple[int, str]:
    """Blocking helper: invoke ``./hermes.sh <subcommand>`` and return
    ``(returncode, output)``.

    We deliberately use stdlib ``subprocess.run`` (not
    ``asyncio.create_subprocess_exec``) because hermes.sh daemonizes the
    gateway via ``nohup bash -c '...' &``. Under uvloop, the daemonized
    child inherits the read/write pipe FDs from the FastAPI worker —
    ``proc.communicate()`` then never sees EOF, hits the timeout, and the
    follow-up ``proc.kill()`` raises ``ProcessLookupError`` because the
    bash parent has already exited. Stdlib ``subprocess`` honors
    ``close_fds=True`` and ``start_new_session=True``, which severs that
    inheritance chain so the script returns cleanly.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["bash", str(_HERMES_SH), subcommand],
            cwd=str(_HERMES_SH.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            close_fds=True,
            start_new_session=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, f"hermes.sh {subcommand} timed out after {timeout_s}s"
    except FileNotFoundError:
        return -1, f"hermes.sh not found at {_HERMES_SH}"
    except Exception as e:  # noqa: BLE001
        return -1, f"hermes.sh {subcommand} failed: {e!r}"

    output = (result.stdout or b"").decode(errors="replace")[-2000:]
    return result.returncode, output


async def _run_hermes_sh(subcommand: str, timeout_s: float = 60.0) -> tuple[int, str]:
    """Async wrapper for ``hermes.sh <subcommand>`` — see ``_run_hermes_sh_sync``
    for why we route through a thread."""
    return await asyncio.to_thread(_run_hermes_sh_sync, subcommand, timeout_s)


def _read_hermes_gateway_state() -> dict | None:
    """Return the parsed ``~/.hermes/gateway_state.json``, or None if absent/invalid."""
    if not _HERMES_GATEWAY_STATE_FILE.is_file():
        return None
    try:
        return _json.loads(_HERMES_GATEWAY_STATE_FILE.read_text())
    except Exception:
        return None


@router.get("/api/channels/hermes/status")
async def hermes_status():
    """Liveness for the hermes gateway. Combines a probe to ``/v1/models``
    on port 8642 with the on-disk ``gateway_state.json`` so the UI can
    show "running but channel X is retrying" diagnostics."""
    import httpx

    parsed = _urlparse(_HERMES.api_url)
    port = parsed.port or 8642
    models_url = _HERMES.api_url.replace("/v1/chat/completions", "/v1/models")

    running = False
    try:
        resp = httpx.get(models_url, timeout=3.0, headers={"Authorization": f"Bearer {_HERMES.api_token}"} if _HERMES.api_token else None)
        running = resp.status_code in (200, 401, 405)  # 401 = up but unauthorized
    except Exception:
        running = False

    state = _read_hermes_gateway_state() or {}
    return {
        "installed": True,
        "running": running,
        "port": port if running else None,
        "ws_url": None,
        "gateway_state": state.get("gateway_state"),
        "active_agents": state.get("active_agents"),
        "platforms": state.get("platforms") or {},
        "updated_at": state.get("updated_at"),
    }


@router.post("/api/channels/hermes/start")
async def hermes_start():
    rc, output = await _run_hermes_sh("start", timeout_s=60.0)
    return {"ok": rc == 0, "status": "started" if rc == 0 else "error", "output": output}


@router.post("/api/channels/hermes/stop")
async def hermes_stop():
    rc, output = await _run_hermes_sh("stop", timeout_s=30.0)
    return {"ok": rc == 0, "status": "stopped" if rc == 0 else "error", "output": output}


@router.post("/api/channels/hermes/restart")
async def hermes_restart():
    """Restart the hermes gateway. The single button users will hit most —
    after saving a provider key or channel token, ``~/.hermes/.env`` has
    new values but the running gateway holds the old env in memory.
    """
    rc, output = await _run_hermes_sh("restart", timeout_s=90.0)
    return {"ok": rc == 0, "status": "restarted" if rc == 0 else "error", "output": output}
