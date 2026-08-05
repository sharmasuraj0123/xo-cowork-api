"""``project.json`` sink — one-shot identity fill.

The bundled template ships ``project.json`` with ``_template: true``
and null identity fields. On first sight of a project (the first
``SessionFirstSeen`` event the watcher processes for it, or simply
on the first tick where the project is discovered) this sink:

* generates a UUID for ``pid`` if missing
* sets ``name`` to the project id (the user can rename via the UI
  later; the watcher doesn't override an explicit name)
* sets ``owner_user_id`` from the auth state (or ``"local"``)
* sets ``created_at`` to the current ISO timestamp
* removes ``_template`` so subsequent ticks no-op

Every other key is carried through untouched. ``project.json`` is the
synced contract *and* the key to the runtime store (``pid`` resolves
``~/.xo/<pid>/``), and it has more than one writer: ``project_layout.
_upsert_metadata`` stamps ``display_name``/``description`` at scaffold
time, before the watcher has ever seen the project. A sink that rebuilt
the document from its own field list would delete those on the first
tick — and would delete any field a later contract version adds. So the
fill is a *merge*: read, fill the gaps, write back.

Idempotent. Runs to completion or no-ops; never partially writes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.reader import read_json


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_user_id() -> str:
    """Pull the local user id from the auth state, falling back to
    ``"local"`` (see docs/watcher-design.md §8.1).

    Imported lazily because ``routers.auth`` triggers FastAPI app
    construction at import time in some test paths.
    """
    try:
        from routers.auth.auth import get_auth_state
        return (get_auth_state().get("user_id") or "local")
    except Exception:
        return "local"


def fill_identity(xo_dir: Path, project_id: str) -> bool:
    """Run the one-shot identity fill if needed.

    Returns ``True`` iff ``project.json`` was rewritten.
    """
    path = xo_dir / "project.json"
    current = read_json(path)
    if not isinstance(current, dict):
        # Missing, empty, malformed, or a non-object document — start clean.
        current = {}

    if not current.get("_template", False) and current.get("pid"):
        # Already filled — no-op.
        return False

    # Merge, don't replace: start from what's on disk so keys this sink knows
    # nothing about (display_name, description, anything a later contract
    # version adds) survive the fill. Only `_template` is deliberately dropped.
    new = dict(current)
    new.pop("_template", None)

    # `or`-style defaulting throughout: an explicit value always wins, and a
    # null placeholder from the template counts as missing.
    new["schema"]        = current.get("schema") or 1
    new["pid"]           = current.get("pid") or str(uuid.uuid4())
    new["name"]          = current.get("name") or project_id
    new["owner_user_id"] = current.get("owner_user_id") or _resolve_user_id()
    new["created_at"]    = current.get("created_at") or _now_iso()

    write_json_atomic(path, new)
    return True
