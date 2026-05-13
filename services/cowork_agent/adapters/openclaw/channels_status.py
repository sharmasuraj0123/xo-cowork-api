"""
OpenClaw Channels Status

Runs `openclaw channels status --json` — the source of truth for channel
`running` state (it queries the gateway). Two output modes:
  1. JSON when the gateway is reachable.
  2. Text fallback when the gateway is unreachable — the CLI exits 0 but prints
     config-only bullets starting with "Gateway not reachable…". In that case
     no channel can actually be running, so we report running=false.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from typing import Any, Dict, List, Optional

OPENCLAW_BIN_ENV = "OPENCLAW_BIN"
DEFAULT_BIN = "openclaw"
# ~10s when gateway up, ~24s on the gateway-down fallback path. 35s gives headroom.
DEFAULT_TIMEOUT_SECONDS = 35.0

_TEXT_FALLBACK_MARKER = "Gateway not reachable"
_BULLET_RE = re.compile(r"^- (?P<channel>\S+)\s+\S+:\s+(?P<flags>.+)$")


class OpenclawStatusError(Exception):
    """CLI invocation/parse failure. `code` maps to an HTTP status in the router."""

    def __init__(self, message: str, *, code: str, detail: Optional[str] = None):
        super().__init__(message)
        self.code = code  # binary_not_found | timeout | execution_failed | invalid_output
        self.detail = detail


def _resolve_binary() -> str:
    configured = (os.getenv(OPENCLAW_BIN_ENV, "") or "").strip()
    return configured or shutil.which(DEFAULT_BIN) or DEFAULT_BIN


async def fetch_channels_raw(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Run the CLI; return {"mode": "json"|"text", "payload": dict|str}."""
    binary = _resolve_binary()
    if os.path.isabs(binary) and not os.path.isfile(binary):
        raise OpenclawStatusError(
            f"openclaw binary not found at {binary}", code="binary_not_found", detail=binary
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "channels", "status", "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as e:
        raise OpenclawStatusError(
            f"openclaw binary unavailable: {binary}", code="binary_not_found", detail=str(e)
        )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        raise OpenclawStatusError(f"openclaw timed out after {timeout}s", code="timeout")

    out = (stdout or b"").decode("utf-8", errors="replace").strip()
    err = (stderr or b"").decode("utf-8", errors="replace").strip()

    if out.startswith("{"):
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                return {"mode": "json", "payload": parsed}
        except json.JSONDecodeError:
            pass

    # Gateway-unreachable fallback: CLI writes the entire bullet view to stderr.
    for stream in (out, err):
        if _TEXT_FALLBACK_MARKER in stream:
            return {"mode": "text", "payload": stream}

    if proc.returncode != 0:
        raise OpenclawStatusError(
            f"openclaw exited with code {proc.returncode}",
            code="execution_failed",
            detail=err or out[:300] or None,
        )

    raise OpenclawStatusError(
        "openclaw returned unrecognized output",
        code="invalid_output",
        detail=(out or err)[:300] or None,
    )


def _build_from_json(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    channels = payload.get("channels") or {}
    accounts = payload.get("channelAccounts") or {}
    order = payload.get("channelOrder") or list(channels.keys())
    out: List[Dict[str, Any]] = []
    for ch_id in order:
        ch = channels.get(ch_id) or {}
        ch_accounts = accounts.get(ch_id) or []
        # A channel is enabled if any of its accounts is enabled.
        enabled = any(bool(a.get("enabled")) for a in ch_accounts) if ch_accounts else False
        if not enabled:
            continue
        out.append({
            "id": ch_id,
            "enabled": True,
            "configured": bool(ch.get("configured")),
            "running": bool(ch.get("running")),
        })
    return out


def _build_from_text(text: str) -> List[Dict[str, Any]]:
    """Gateway-unreachable bullets. Nothing can be running when gateway is down."""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for line in text.splitlines():
        m = _BULLET_RE.match(line.strip())
        if not m:
            continue
        flags = [f.strip() for f in m.group("flags").split(",")]
        if "enabled" not in flags:
            continue
        ch_id = m.group("channel").lower()
        if ch_id in seen:
            continue
        seen.add(ch_id)
        # "not configured" overrides "configured" if both appear; check explicitly.
        configured = "configured" in flags and "not configured" not in flags
        out.append({
            "id": ch_id,
            "enabled": True,
            "configured": configured,
            "running": False,
        })
    return out


def build_status_view(raw: Dict[str, Any]) -> Dict[str, Any]:
    if raw.get("mode") == "json":
        return {"channels": _build_from_json(raw["payload"])}
    return {"channels": _build_from_text(raw["payload"])}


async def get_channels_status(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Dict[str, Any]:
    return build_status_view(await fetch_channels_raw(timeout=timeout))
