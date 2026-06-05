"""
commit_relay/config.py — local relay state persisted at
~/xo-projects/.xo/context/relay.json

Schema (list of channel entries):
[
  {
    "channel_id":       "uuid-string",
    "project_id":       "my-project",
    "peer_workspace_id": "workspace-b",
    "peer_cowork_url":   "https://5002--main--workspace-b--org.coder.app",
    "peer_push_secret":  "<secret peer gave us — used by peer's scanner to call our /push>",
    "my_push_secret":    "<secret we gave peer — used by our scanner to call their /push>",
    "watched_repos":     ["/home/coder/xo-swarm-api"]
  }
]
"""

from __future__ import annotations

import json
from pathlib import Path

from services.cowork_agent.project_layout import workspace_xo_dir
from services.cowork_agent.visualizer.atomic_write import write_json_atomic


def _relay_config_path() -> Path:
    return workspace_xo_dir() / "context" / "relay.json"


def load_relay_config() -> list[dict]:
    path = _relay_config_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_relay_config(entries: list[dict]) -> None:
    write_json_atomic(_relay_config_path(), entries)


def add_channel(
    channel_id: str,
    peer_workspace_id: str,
    project_id: str = "",
    peer_cowork_url: str = "",
    peer_push_secret: str = "",
    my_push_secret: str = "",
) -> None:
    entries = load_relay_config()
    for e in entries:
        if e["channel_id"] == channel_id:
            return
    entries.append({
        "channel_id": channel_id,
        "project_id": project_id,
        "peer_workspace_id": peer_workspace_id,
        "peer_cowork_url": peer_cowork_url,
        "peer_push_secret": peer_push_secret,
        "my_push_secret": my_push_secret,
        "watched_repos": [],
    })
    save_relay_config(entries)


def remove_channel(channel_id: str) -> None:
    entries = [e for e in load_relay_config() if e["channel_id"] != channel_id]
    save_relay_config(entries)


def get_channel(channel_id: str) -> dict | None:
    for e in load_relay_config():
        if e["channel_id"] == channel_id:
            return e
    return None


def get_peer_url(channel_id: str) -> str:
    e = get_channel(channel_id)
    return e.get("peer_cowork_url", "") if e else ""


def get_peer_push_secret(channel_id: str) -> str:
    """Secret we send in Authorization when calling peer's /push endpoint."""
    e = get_channel(channel_id)
    return e.get("peer_push_secret", "") if e else ""


def get_my_push_secret(channel_id: str) -> str:
    """Secret the peer sends when calling our /push endpoint."""
    e = get_channel(channel_id)
    return e.get("my_push_secret", "") if e else ""


def get_watched_repos(channel_id: str) -> list[str]:
    for e in load_relay_config():
        if e["channel_id"] == channel_id:
            return e.get("watched_repos", [])
    return []


def set_watched_repos(channel_id: str, repo_paths: list[str]) -> None:
    entries = load_relay_config()
    for e in entries:
        if e["channel_id"] == channel_id:
            e["watched_repos"] = repo_paths
            break
    save_relay_config(entries)
