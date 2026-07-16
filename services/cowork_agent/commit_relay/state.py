"""Per-repo relay state at <repo>/.xo/commit_relay.json.

Two independent fields share one file: `last_reported` (watcher: last remote SHA
reported to swarm) and `cursor` (poller: last ledger seq fetched). Read-modify-write
so neither writer clobbers the other."""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_FILENAME = "commit_relay.json"


def _path(repo_dir) -> Path:
    return Path(repo_dir) / ".xo" / _FILENAME


def _read(repo_dir) -> dict:
    path = _path(repo_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.debug("commit_relay: bad state %s: %s", path, exc)
        return {}


def _write(repo_dir, data: dict) -> None:
    path = _path(repo_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def load_last_reported(repo_dir) -> str | None:
    return _read(repo_dir).get("last_reported") or None


def save_last_reported(repo_dir, sha: str) -> None:
    data = _read(repo_dir)
    data["last_reported"] = sha
    _write(repo_dir, data)


def load_cursor(repo_dir) -> int:
    try:
        return int(_read(repo_dir).get("cursor") or 0)
    except (TypeError, ValueError):
        return 0


def save_cursor(repo_dir, seq: int) -> None:
    data = _read(repo_dir)
    data["cursor"] = int(seq)
    _write(repo_dir, data)
