"""Persist the last-seen origin/main SHA at <project>/.xo/commit_watcher.json."""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_FILENAME = "commit_watcher.json"


def _state_path(project_dir: Path) -> Path:
    return Path(project_dir) / ".xo" / _FILENAME


def load_last_seen(project_dir: Path) -> str | None:
    path = _state_path(project_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data.get("last_seen")
        return value or None
    except Exception as exc:
        log.debug("commit_watcher: unreadable state file %s: %s", path, exc)
        return None


def save_last_seen(project_dir: Path, sha: str) -> None:
    path = _state_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_seen": sha}) + "\n", encoding="utf-8")
