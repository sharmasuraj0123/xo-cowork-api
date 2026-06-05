"""
Hermes channels-status view.

Maps `features.platforms` + `features.gateway` from `hermes dump` into the
openclaw channels-status shape:

    {"channels": [{"id": "telegram", "enabled": true, "configured": true,
                    "running": <gateway_running>}, ...]}

Hermes lists configured platforms as a comma-separated string. Each listed
platform is treated as both `enabled` and `configured`. The `running` flag
follows hermes' single gateway state — if the gateway is up, every listed
channel is running; if not, none are. Hermes does not expose per-channel
gateway state.
"""

from __future__ import annotations

import json
from typing import Any

from services.cowork_agent.adapters.hermes.dump import (
    HermesStatusError,
    fetch_dump,
)
from services.cowork_agent.settings import HERMES_DIR


def _gateway_running(features: dict[str, Any]) -> bool:
    raw = features.get("gateway") if isinstance(features, dict) else None
    if not isinstance(raw, str):
        return False
    return raw.strip().lower().startswith("running")


def _platforms(features: dict[str, Any]) -> list[str]:
    raw = features.get("platforms") if isinstance(features, dict) else None
    if not isinstance(raw, str):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for token in raw.split(","):
        name = token.strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def build_status_view(dump: dict[str, Any]) -> dict[str, Any]:
    """Translate a parsed `hermes dump` dict into the openclaw channels-status shape."""
    features = dump.get("features") or {}
    running = _gateway_running(features)
    channels = [
        {
            "id": name,
            "enabled": True,
            "configured": True,
            "running": running,
        }
        for name in _platforms(features)
    ]
    return {"channels": channels}


async def get_channels_status(timeout: float | None = None) -> dict[str, Any]:
    """Fetch `hermes dump` and project it into the openclaw channels-status shape."""
    if timeout is None:
        dump = await fetch_dump()
    else:
        dump = await fetch_dump(timeout=timeout)
    return build_status_view(dump)


def list_channels() -> dict[str, Any]:
    """Connected-channels view for ``GET /api/channels``.

    Reads ``~/.hermes/gateway_state.json`` and returns the uniform shape
    ``{"channels": {id: {...}}, "gateway_running": bool}``. The shape is kept
    stable so the frontend can index ``data.channels[id]`` without runtime
    guards; agents without a connected-channels source return an empty map.
    """
    state_file = HERMES_DIR / "gateway_state.json"
    if not state_file.is_file():
        return {"channels": {}, "gateway_running": False}
    try:
        state = json.loads(state_file.read_text())
    except Exception:
        return {"channels": {}, "gateway_running": False}

    platforms = state.get("platforms") or {}
    channels: dict[str, dict] = {}
    for platform_id, info in platforms.items():
        if not isinstance(info, dict):
            continue
        # The gateway lists api_server too — that's the hermes API itself,
        # not a user-facing messaging channel. Hide it from the UI list.
        if platform_id == "api_server":
            continue
        channels[platform_id] = {
            "id": platform_id,
            "name": platform_id,
            "type": platform_id,
            "status": info.get("state") or "unknown",
            "account": info.get("error_message") or None,
        }

    return {
        "channels": channels,
        "gateway_running": (state.get("gateway_state") == "running"),
    }


__all__ = [
    "HermesStatusError",
    "build_status_view",
    "get_channels_status",
    "list_channels",
]
