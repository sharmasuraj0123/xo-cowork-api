"""
Cross-workspace commit relay (client side).

Workspaces collaborating on the same xo-project each have their own clone of that
project's shared GitHub repo. When one workspace pushes a commit, it publishes a
minimal ping ``{project_id, commit}`` to a central relayer, which broadcasts it to
every other subscribed workspace. On receipt, a workspace ``git fetch``es so the
commit becomes locally available — it does NOT merge/checkout (the agent decides
when to apply).

The relayer is a separate service; it owns the single durable global ledger of
transfers, assigning each a monotonic ``seq``. This client persists only a tiny
**cursor** — the last ``seq`` it processed — so on reconnect it resumes via
``/subscribe?since=<cursor>`` and catches up on anything that happened while it was
offline. (The cursor is one integer, not a ledger; the full history stays at the
relay.)

Disabled entirely when ``RELAY_URL`` is unset: ``ping_commit`` is a no-op and the
subscriber never starts. Outbound-call style mirrors ``services/usage_sync.py``.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx

from routers.auth import get_auth_token
from services.cowork_agent.project_layout import xo_projects_root

RELAY_URL = (os.getenv("RELAY_URL", "") or "").strip().rstrip("/")
CURSOR_PATH = Path(
    os.getenv("RELAY_CURSOR_PATH", str(Path.home() / ".xo-cowork" / "relay-cursor"))
)


def _log(msg: str) -> None:
    # Background service: print() is block-buffered when stdout is a log file,
    # so flush every line or the operator sees nothing.
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


async def ping_commit(project_id: str, commit: str) -> bool:
    """Publish a {project_id, commit} ping to the relayer.

    Fire-and-forget: never raises, returns False on any failure or when the relay
    is not configured. A relay outage must never break the caller (e.g. a push).
    """
    if not RELAY_URL:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{RELAY_URL}/publish",
                json={"project_id": project_id, "commit": commit},
                headers=_headers(),
            )
            resp.raise_for_status()
            body = resp.json()
        _log(
            f"📤 relay: published {project_id} @ {commit[:10]} "
            f"(seq={body.get('seq')}, delivered_to={body.get('delivered_to')})"
        )
        return True
    except Exception as exc:  # noqa: BLE001 — non-fatal by design
        _log(f"⚠️ relay publish failed (non-fatal): {exc}")
        return False


async def _fetch_on_receive(project_id: str, commit: str) -> None:
    """git fetch so ``commit`` is locally available. Fetch only — never merge/checkout."""
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
    """Hold one SSE connection open to the relayer; git fetch on each ping.

    Resumes from the persisted cursor (``?since=``) so events that arrived while
    this workspace was offline are replayed on connect. Reconnects with a fixed
    backoff if the connection drops. Started from the server lifespan only when
    ``RELAY_URL`` is set, so this loop assumes it is.
    """
    _log(f"   relay: subscribing to {RELAY_URL}/subscribe (SSE, no polling)")
    while True:
        cursor = _read_cursor()
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "GET",
                    f"{RELAY_URL}/subscribe",
                    params={"since": cursor},
                    headers=_headers(),
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
