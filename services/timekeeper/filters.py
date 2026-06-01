"""Decide whether an event survives. Pure function, no I/O."""

from __future__ import annotations

from services.timekeeper import config


def keep(event: dict) -> bool:
    path = event.get("path", "")
    for prefix in config.IGNORE_PATH_PREFIXES:
        if path.startswith(prefix):
            return False
    for sub in config.IGNORE_PATH_SUBSTRINGS:
        if sub in path:
            return False
    return True
