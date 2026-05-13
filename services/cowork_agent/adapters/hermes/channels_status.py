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

from typing import Any

from services.cowork_agent.adapters.hermes.dump import (
    HermesStatusError,
    fetch_dump,
)


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


__all__ = ["HermesStatusError", "build_status_view", "get_channels_status"]
