"""
Install-wide per-action enable/disable preferences for Composio toolkits.

The agent's view of each toolkit's catalogue is filtered through this
store before it reaches the model — and `composio_service.execute_tool`
also consults it as a fail-safe, so a model that guesses a slug for a
disabled action can't bypass the user's choice.

Storage shape on disk (JSON):

    {
      "googlecalendar": {
        "GOOGLECALENDAR_DELETE_EVENT": false,
        ...
      },
      ...
    }

Only **disabled** slugs are persisted. Absent ⇒ enabled. That keeps the
file tiny and makes default-on behaviour intrinsic (a fresh install
shows every action without needing to seed anything).

Threading / atomicity:

- Reads return whatever's on disk; safe to call from any request handler.
- Writes use `services.cowork_agent.visualizer.flock.locked()` for an
  advisory cross-process lock, then `write_json_atomic()` for a torn-
  write-proof rename. Same pattern as `todos_store.py`.

File location: `<repo>/data/composio_action_prefs.json` — kept in-tree
(under a runtime-state folder) so all install state is co-located with
the API code. `data/` is in `.gitignore`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.reader import read_json


_PREFS_PATH = Path(__file__).resolve().parent.parent / "data" / "composio_action_prefs.json"


def _store_path() -> Path:
    return _PREFS_PATH


def load_prefs() -> Dict[str, Dict[str, bool]]:
    """Read the full prefs document. Empty dict if the file is absent."""
    data = read_json(_store_path())
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict[str, bool]] = {}
    for toolkit_id, entry in data.items():
        if not isinstance(entry, dict):
            continue
        out[toolkit_id] = {
            slug: bool(enabled)
            for slug, enabled in entry.items()
            if isinstance(slug, str)
        }
    return out


def get_toolkit_prefs(toolkit_id: str) -> Dict[str, bool]:
    """Return the disabled-slug map for one toolkit. Always a fresh dict."""
    return dict(load_prefs().get(toolkit_id, {}))


def is_action_enabled(toolkit_id: str, slug: str) -> bool:
    """Default-on. Returns False only when the slug is explicitly set to False."""
    return load_prefs().get(toolkit_id, {}).get(slug, True) is True


def bulk_set(toolkit_id: str, updates: Dict[str, bool]) -> Dict[str, bool]:
    """Apply a batch of {slug: enabled} updates to one toolkit.

    Enabled-true entries are pruned from the on-disk map (the file only
    records disables) so the document stays minimal. Returns the post-
    update map for the toolkit (the same shape the file would store).
    """
    path = _store_path()
    with locked(path):
        current = load_prefs()
        toolkit_map = dict(current.get(toolkit_id, {}))
        for slug, enabled in updates.items():
            if not isinstance(slug, str):
                continue
            if enabled:
                toolkit_map.pop(slug, None)
            else:
                toolkit_map[slug] = False
        if toolkit_map:
            current[toolkit_id] = toolkit_map
        else:
            current.pop(toolkit_id, None)
        write_json_atomic(path, current)
        return toolkit_map
