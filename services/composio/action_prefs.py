"""
Per-user, per-action enable/disable preferences for Composio toolkits.

The agent's view of each toolkit's catalogue is filtered through this
store before it reaches the model — and `composio_service.execute_tool`
also consults it as a fail-safe, so a model that guesses a slug for a
disabled action can't bypass the user's choice.

Storage shape on disk (JSON):

    {
      "version": 2,
      "users": {
        "user_abc": {
          "googlecalendar": {
            "GOOGLECALENDAR_DELETE_EVENT": false,
            ...
          },
          ...
        },
        ...
      }
    }

Prefs are keyed by user: an install-wide map would let one tenant's toggle
silently rewrite what another tenant's agent is allowed to call. ``user_id``
is therefore mandatory on every call — there is no shared bucket.

The pre-v2 shape (a bare ``{toolkit: {slug: bool}}`` document, written when
prefs were install-wide) is no longer honoured: it has no owner, and adopting
it for every user would recreate exactly the cross-tenant coupling above.
Such a document is ignored with a warning and replaced by the v2 shape on the
next write; re-toggle the affected actions once, per user.

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

import logging
from pathlib import Path
from typing import Dict

from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.reader import read_json

log = logging.getLogger(__name__)

_PREFS_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "composio_action_prefs.json"


def _store_path() -> Path:
    return _PREFS_PATH


def _require_user_id(user_id: str | None) -> str:
    """Prefs are always per-user; refuse to guess one."""
    uid = (user_id or "").strip()
    if not uid:
        raise ValueError("action_prefs: a real user_id is required.")
    return uid


def _coerce_toolkit_map(entry: object) -> Dict[str, bool]:
    if not isinstance(entry, dict):
        return {}
    return {
        slug: bool(enabled)
        for slug, enabled in entry.items()
        if isinstance(slug, str)
    }


def load_all() -> Dict[str, Dict[str, Dict[str, bool]]]:
    """Read the full document as ``{user_id: {toolkit: {slug: bool}}}``.

    Only the v2 (per-user) shape is honoured. A pre-v2 install-wide document is
    ownerless, so it is dropped rather than applied to whoever asks first.
    """
    data = read_json(_store_path())
    if not isinstance(data, dict):
        return {}

    if data.get("version") == 2 or "users" in data:
        users = data.get("users")
        if not isinstance(users, dict):
            return {}
        return {
            str(uid): {
                tk: _coerce_toolkit_map(entry)
                for tk, entry in per_user.items()
                if isinstance(tk, str)
            }
            for uid, per_user in users.items()
            if isinstance(per_user, dict)
        }

    # Pre-v2: a bare {toolkit: {slug: bool}} install-wide document. No owner →
    # not attributable to any tenant, so it is not applied.
    if data:
        log.warning(
            "action_prefs: ignoring pre-v2 install-wide prefs document at %s "
            "(no owning user). Re-toggle actions per user; the file is "
            "rewritten in the per-user shape on the next write.",
            _store_path(),
        )
    return {}


def load_prefs(user_id: str) -> Dict[str, Dict[str, bool]]:
    """Read one user's prefs as ``{toolkit: {slug: bool}}``."""
    return load_all().get(_require_user_id(user_id), {})


def get_toolkit_prefs(toolkit_id: str, user_id: str) -> Dict[str, bool]:
    """Return the disabled-slug map for one toolkit. Always a fresh dict."""
    return dict(load_prefs(user_id).get(toolkit_id, {}))


def is_action_enabled(toolkit_id: str, slug: str, user_id: str) -> bool:
    """Default-on. Returns False only when the slug is explicitly set to False."""
    return load_prefs(user_id).get(toolkit_id, {}).get(slug, True) is True


def bulk_set(
    toolkit_id: str, updates: Dict[str, bool], user_id: str,
) -> Dict[str, bool]:
    """Apply a batch of {slug: enabled} updates to one toolkit for one user.

    Enabled-true entries are pruned from the on-disk map (the file only
    records disables) so the document stays minimal. Returns the post-
    update map for the toolkit (the same shape the file would store).
    """
    uid = _require_user_id(user_id)
    path = _store_path()
    with locked(path):
        # Re-read inside the lock: load_all() also normalises a pre-v2
        # document, so the write below always lands in the v2 shape.
        current = load_all()
        user_map = dict(current.get(uid, {}))
        toolkit_map = dict(user_map.get(toolkit_id, {}))
        for slug, enabled in updates.items():
            if not isinstance(slug, str):
                continue
            if enabled:
                toolkit_map.pop(slug, None)
            else:
                toolkit_map[slug] = False
        if toolkit_map:
            user_map[toolkit_id] = toolkit_map
        else:
            user_map.pop(toolkit_id, None)
        if user_map:
            current[uid] = user_map
        else:
            current.pop(uid, None)
        write_json_atomic(path, {"version": 2, "users": current})
        return toolkit_map
