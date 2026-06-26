"""Last-reported remote SHA per repo, at <repo>/.xo/commit_relay.json."""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_FILENAME = "commit_relay.json"


def _path(repo_dir) -> Path:
    return Path(repo_dir) / ".xo" / _FILENAME


def load_last_reported(repo_dir) -> str | None:
    path = _path(repo_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("last_reported") or None
    except Exception as exc:
        log.debug("commit_relay: bad state %s: %s", path, exc)
        return None


def save_last_reported(repo_dir, sha: str) -> None:
    path = _path(repo_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_reported": sha}) + "\n", encoding="utf-8")
