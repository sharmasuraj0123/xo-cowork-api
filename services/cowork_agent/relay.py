"""
Cross-workspace commit relay — subscriber (client) side.

Holds one SSE connection open to swarm's GET /commits/subscribe. On each broadcast
event it runs `git fetch origin` in ~/xo-projects/<project_id> so the commit becomes
locally available — it does NOT merge/checkout (the agent decides when to apply).

Resumes from a persisted cursor (the last `seq` processed) via ?since=<cursor>, so
events that arrived while offline are replayed on reconnect. Publishing is handled by
the watcher (services/cowork_agent/commit_relay), not here.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx

from routers.auth import get_auth_token
from services.cowork_agent.project_layout import xo_projects_root

CURSOR_PATH = Path(
    os.getenv("RELAY_CURSOR_PATH", str(Path.home() / ".xo-cowork" / "relay-cursor"))
)


def _base_url() -> str:
    return os.getenv("CHAT_API_BASE_URL", "https://api-swarm-beta.xo.builders").rstrip("/")


def _subscribe_url() -> str:
    return f"{_base_url()}/commits/subscribe"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _headers() -> dict[str, str]:
    tok = get_auth_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _read_cursor() -> int:
    try:
        return int(CURSOR_PATH.read_text().strip())
    except Exception:
        return 0


def _write_cursor(seq: int) -> None:
    try:
        CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        CURSOR_PATH.write_text(str(seq))
    except Exception as exc:  # noqa: BLE001 — cursor persistence is best-effort
        _log(f"⚠️ relay: failed to persist cursor: {exc}")


async def _fetch_on_receive(project_id: str, commit: str) -> None:
    """git fetch so `commit` is locally available. Fetch only — never merge/checkout."""
    repo = xo_projects_root() / project_id
    if not (repo / ".git").is_dir():
        _log(f"⚠️ relay: no clone at {repo}; skipping fetch for {commit[:10]}")
        return
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), "fetch", "origin", "--quiet",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode == 0:
        _log(f"📥 relay: fetched {project_id} @ {commit[:10]}")
    else:
        _log(f"⚠️ relay fetch failed for {project_id}: {err.decode().strip()}")


async def run_relay_subscriber() -> None:
    """Hold one SSE connection open to swarm; git fetch on each ping. Resumes from the
    persisted cursor; reconnects with a fixed backoff if the connection drops."""
    _log(f"   relay: subscribing to {_subscribe_url()} (SSE, no polling)")
    while True:
        cursor = _read_cursor()
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "GET", _subscribe_url(), params={"since": cursor}, headers=_headers(),
                ) as resp:
                    resp.raise_for_status()
                    _log(f"📡 relay: connected (resuming from seq={cursor})")
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue  # skip SSE comments / keepalives
                        try:
                            event = json.loads(line[len("data: "):])
                            await _fetch_on_receive(event["project_id"], event["commit"])
                            seq = int(event.get("seq", 0))
                            if seq:
                                _write_cursor(seq)
                        except (json.JSONDecodeError, KeyError, ValueError) as exc:
                            _log(f"⚠️ relay: bad event ignored: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep the subscriber alive
            _log(f"⚠️ relay subscriber dropped, retrying in 5s: {exc}")
            await asyncio.sleep(5)
