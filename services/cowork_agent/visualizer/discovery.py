"""Shared session-row discovery for adapter visualizer sources.

The watcher owns the per-project ``.xo/sessions/sessionslist.json``
layout: where each project lives, what shape the index file has,
how to iterate every project on disk. Source modules that ship under
``services/cowork_agent/adapters/<name>/visualizer_source.py`` should
**not** re-implement that walk — they just consume the tuples this
module yields and do the backend-specific work (path translation,
SQLite query, JSONL tail, etc.) on the rows that match their name.

Keeping this in shared code instead of duplicating it per adapter
means a new agent's ``visualizer_source.py`` only needs to know about
its **own** transcript/state storage. Watcher infrastructure stays in
the watcher.
"""

from __future__ import annotations

import json
import logging
from typing import Iterator

from services.cowork_agent.project_layout import xo_dir
from services.cowork_agent.visualizer.workspace_index import list_project_ids

logger = logging.getLogger(__name__)


def iter_sessionslist_rows(backend: str) -> Iterator[tuple[str, str, dict]]:
    """Yield ``(project_id, composite_key, row)`` for every adapter row
    in any project's ``sessionslist.json`` whose ``backend`` field
    equals ``backend``.

    Order: by project id (whatever ``list_project_ids`` returns), then
    by the index file's iteration order within each project. Sources
    that need a deterministic order should sort themselves.

    Skips quietly on:

    * Missing / unreadable ``sessionslist.json``
    * Malformed JSON
    * Row whose value isn't a dict
    * Row whose ``backend`` doesn't match (the source-name filter)

    The watcher's read-modify-write loop on ``sessionslist.json`` is
    atomic (the adapter that publishes rows uses an atomic rename),
    so this reader sees a consistent snapshot per project per call.
    """
    for project_id in list_project_ids():
        sl_path = xo_dir(project_id) / "sessions" / "sessionslist.json"
        if not sl_path.is_file():
            continue
        try:
            sl = json.loads(sl_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("discovery: skipping %s: %s", sl_path, exc)
            continue
        if not isinstance(sl, dict):
            continue
        for composite_key, row in sl.items():
            if not isinstance(row, dict):
                continue
            if row.get("backend") != backend:
                continue
            yield project_id, composite_key, row
