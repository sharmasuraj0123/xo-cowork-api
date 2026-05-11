"""
On-demand OpenClaw setup endpoint.

Mirrors the Coder/Terraform flow by:
1) Optionally writing/updating .env values used by openclaw.sh
2) Optionally writing WhatsApp creds.json
3) Running `openclaw.sh setup` and streaming logs via SSE
"""

import asyncio
import json
import os
import stat
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


OPENCLAW_SETUP_TIMEOUT_SECONDS = int(os.getenv("OPENCLAW_SETUP_TIMEOUT", "1800"))
_SSE_HEARTBEAT_INTERVAL = 15

router = APIRouter(prefix="/openclaw", tags=["openclaw-setup"])


class OpenClawSetupRequest(BaseModel):
    overwrite_env: bool = False
    enabled_channels: Optional[list[str]] = Field(default=None)
    telegram_bot_token: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    openclaw_gateway_token: Optional[str] = None
    xo_auth_session_id: Optional[str] = None
    xo_poll_token: Optional[str] = None
    xo_api_key: Optional[str] = None
    slack_bot_token: Optional[str] = None
    slack_app_token: Optional[str] = None
    openclaw_control_ui_origin: Optional[str] = None
    chat_api_base_url: Optional[str] = None
    claude_code_oauth_token: Optional[str] = None
    whatsapp_creds: Optional[str] = None
    openclaw_version: Optional[str] = None


def _resolve_repo_root() -> Path:
    configured = os.getenv("XO_COWORK_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _resolve_openclaw_script(repo_root: Path) -> Optional[Path]:
    direct = (repo_root / "openclaw.sh").resolve()
    if direct.exists() and direct.is_file():
        return direct
    fallback = Path.home() / "xo-cowork-api" / "openclaw.sh"
    if fallback.exists() and fallback.is_file():
        return fallback.resolve()
    return None


def _parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and ((value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")))
        ):
            value = value[1:-1]
        data[key] = value
    return data


def _build_env_updates(payload: OpenClawSetupRequest) -> dict[str, str]:
    updates: dict[str, str] = {}

    if payload.enabled_channels is not None:
        updates["ENABLED_CHANNELS"] = json.dumps(payload.enabled_channels)

    mapping = {
        "TELEGRAM_BOT_TOKEN": payload.telegram_bot_token,
        "ANTHROPIC_API_KEY": payload.anthropic_api_key,
        "OPENAI_API_KEY": payload.openai_api_key,
        "OPENCLAW_GATEWAY_TOKEN": payload.openclaw_gateway_token,
        "XO_AUTH_SESSION_ID": payload.xo_auth_session_id,
        "XO_POLL_TOKEN": payload.xo_poll_token,
        "XO_API_KEY": payload.xo_api_key,
        "SLACK_BOT_TOKEN": payload.slack_bot_token,
        "SLACK_APP_TOKEN": payload.slack_app_token,
        "OPENCLAW_CONTROL_UI_ORIGIN": payload.openclaw_control_ui_origin,
        "CHAT_API_BASE_URL": payload.chat_api_base_url,
        "CLAUDE_CODE_OAUTH_TOKEN": payload.claude_code_oauth_token,
    }
    for key, value in mapping.items():
        if value is not None:
            updates[key] = value

    return updates


def _write_env(repo_root: Path, payload: OpenClawSetupRequest) -> tuple[Path, int]:
    env_path = repo_root / ".env"
    updates = _build_env_updates(payload)
    if not updates:
        return env_path, 0

    base = {} if payload.overwrite_env else _parse_env_file(env_path)
    base.update(updates)

    lines = [f"{k}={v}" for k, v in sorted(base.items())]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
    return env_path, len(updates)


def _write_whatsapp_creds(payload: OpenClawSetupRequest) -> Optional[Path]:
    if payload.whatsapp_creds is None:
        return None
    creds_dir = Path.home() / ".openclaw" / "credentials" / "whatsapp" / "default"
    creds_dir.mkdir(parents=True, exist_ok=True)
    creds_path = creds_dir / "creds.json"
    creds_path.write_text(payload.whatsapp_creds, encoding="utf-8")
    os.chmod(creds_path, stat.S_IRUSR | stat.S_IWUSR)
    return creds_path


@router.post("/setup")
async def openclaw_setup(payload: OpenClawSetupRequest):
    """
    Run OpenClaw setup on demand and stream progress over SSE.

    Optional request fields let callers inject or refresh .env/credential values
    before setup, matching the same config surface used by the Terraform startup.
    """

    async def generate() -> AsyncGenerator[str, None]:
        repo_root = _resolve_repo_root()
        script = _resolve_openclaw_script(repo_root)
        if script is None:
            yield f"data: {json.dumps({'type': 'error', 'error': 'openclaw.sh not found'})}\n\n"
            return

        try:
            env_path, updated_count = _write_env(repo_root, payload)
            creds_path = _write_whatsapp_creds(payload)
            if updated_count > 0:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "config",
                            "line": f"Updated {updated_count} .env key(s) at {env_path}",
                        }
                    )
                    + "\n\n"
                )
            if creds_path:
                yield (
                    "data: "
                    + json.dumps({"type": "config", "line": f"Updated WhatsApp credentials at {creds_path}"})
                    + "\n\n"
                )
        except OSError as exc:
            yield f"data: {json.dumps({'type': 'error', 'error': f'failed to write config: {exc}'})}\n\n"
            return

        process_env = os.environ.copy()
        process_env["PATH"] = f"{Path.home() / '.local' / 'bin'}:{Path.home() / '.openclaw' / 'bin'}:{process_env.get('PATH', '')}"
        if payload.openclaw_version:
            process_env["OPENCLAW_VERSION"] = payload.openclaw_version

        yield f"data: {json.dumps({'type': 'installing', 'line': 'Running openclaw.sh setup'})}\n\n"

        try:
            proc = await asyncio.create_subprocess_exec(
                str(script),
                "setup",
                cwd=str(script.parent),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=process_env,
            )
        except FileNotFoundError:
            yield f"data: {json.dumps({'type': 'error', 'error': 'bash/openclaw.sh execution failed'})}\n\n"
            return

        deadline = time.monotonic() + OPENCLAW_SETUP_TIMEOUT_SECONDS
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    yield f"data: {json.dumps({'type': 'error', 'error': 'openclaw setup timed out'})}\n\n"
                    return
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=min(_SSE_HEARTBEAT_INTERVAL, remaining),
                    )
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    yield f"data: {json.dumps({'type': 'setup_log', 'line': text})}\n\n"

            rc = await proc.wait()
            yield f"data: {json.dumps({'type': 'done', 'returncode': rc})}\n\n"
        finally:
            if proc.returncode is None:
                proc.kill()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
