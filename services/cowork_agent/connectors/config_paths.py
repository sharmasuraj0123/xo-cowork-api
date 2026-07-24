"""
Connector credential paths — all connector secrets live under ~/.config/xo-cowork/.

Google Drive, OneDrive (rclone.conf), GitHub, Vercel, and Manus (mcp-tokens.json)
must never be written inside the xo-cowork-api checkout. Override with
MCP_TOKENS_FILE or RCLONE_CONFIG when needed.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_CONNECTORS_DIR = Path(__file__).resolve().parent
_SERVICES_DIR = _CONNECTORS_DIR.parents[1]
_REPO_ROOT = _CONNECTORS_DIR.parents[2]

_LEGACY_TOKEN_FILES = (
    _SERVICES_DIR / "mcp-tokens.json",
    _REPO_ROOT / "mcp-tokens.json",
)
_LEGACY_RCLONE_FILES = (
    _SERVICES_DIR / "rclone.conf",
    _REPO_ROOT / "rclone.conf",
)


def connector_config_dir() -> Path:
    xdg = os.getenv("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "xo-cowork"


CONNECTOR_CONFIG_DIR = connector_config_dir()
TOKEN_FILE = Path(os.getenv("MCP_TOKENS_FILE", str(CONNECTOR_CONFIG_DIR / "mcp-tokens.json")))
RCLONE_CONFIG_PATH = os.getenv("RCLONE_CONFIG", str(CONNECTOR_CONFIG_DIR / "rclone.conf"))


def ensure_connector_config_dir() -> None:
    CONNECTOR_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)


def _migrate_legacy_file(dest: Path, legacy_paths: tuple[Path, ...]) -> None:
    if dest.exists():
        return
    for legacy in legacy_paths:
        if legacy.is_file():
            ensure_connector_config_dir()
            shutil.copy2(legacy, dest)
            log.info("Migrated connector credentials from %s to %s", legacy, dest)
            return


def ensure_token_file_migrated() -> None:
    _migrate_legacy_file(TOKEN_FILE, _LEGACY_TOKEN_FILES)


def ensure_rclone_config_migrated() -> None:
    _migrate_legacy_file(Path(RCLONE_CONFIG_PATH), _LEGACY_RCLONE_FILES)
