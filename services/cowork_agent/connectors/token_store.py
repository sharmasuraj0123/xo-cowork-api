"""
token_store — the single owner of mcp-tokens.json.

Every connector (github, vercel, manus, ...) persists its credentials as a
provider-keyed entry in one shared JSON file at <project_root>/mcp-tokens.json
(.gitignored). This module is the ONLY place that knows the file's location,
its on-disk shape, and its read/write semantics. Connectors get/set/delete by
provider key and never touch the format — so locking or a format migration can
later be added here once, not in every connector.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# <project_root>/mcp-tokens.json — four parents up from
# services/cowork_agent/connectors/token_store.py.
_PROJECT_ROOT = Path(os.path.abspath(__file__)).parents[3]
TOKEN_FILE = _PROJECT_ROOT / "mcp-tokens.json"

# Earlier versions were one dirname short and wrote into services/. Existing
# installs still hold live credentials there, so read from it when the correct
# location has nothing yet. Writes always go to TOKEN_FILE, which migrates the
# file on the first change while leaving the old copy intact for rollback.
_LEGACY_TOKEN_FILE = _PROJECT_ROOT / "services" / "mcp-tokens.json"


def _read_file(path: Path) -> dict[str, Any] | None:
    """Parse one token file, or None if it is missing or unreadable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return None


def read_all() -> dict[str, Any]:
    """Read the full mcp-tokens.json. Tolerant of a missing or corrupt file.

    Falls back to the legacy services/ location so an install predating the
    path fix keeps its stored credentials.
    """
    data = _read_file(TOKEN_FILE)
    if data is not None:
        return data

    legacy = _read_file(_LEGACY_TOKEN_FILE)
    if legacy is not None:
        log.info("Reading credentials from legacy %s", _LEGACY_TOKEN_FILE)
        return legacy
    return {}


def write_all(data: dict[str, Any]) -> None:
    """Write the full mcp-tokens.json (pretty-printed, trailing newline)."""
    TOKEN_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def get_entry(provider: str) -> dict[str, Any] | None:
    """Return the stored entry for a provider key, or None if absent."""
    return read_all().get(provider)


def set_entry(provider: str, entry: dict[str, Any]) -> None:
    """Insert or replace one provider's entry, preserving every other key."""
    data = read_all()
    data[provider] = entry
    write_all(data)


def delete_entry(provider: str) -> None:
    """Remove one provider's entry if present, preserving every other key."""
    data = read_all()
    data.pop(provider, None)
    write_all(data)
