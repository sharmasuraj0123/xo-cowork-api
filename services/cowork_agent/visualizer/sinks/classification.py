"""``project.json`` → ``classification`` block — this project's environment
cluster, folder archetype, and XO data-type tallies, persisted.

Computed by :func:`environments_graph.classify_one_project` (the exact
classifier the Environments graph uses) and written here so every graph
loads from local ``.xo/`` state instead of re-walking the tree per request.

The walk is expensive relative to the watcher's 1s tick, so refresh is
throttled: a project recomputes only when its block is missing, older than
``_REFRESH_S``, or the project produced events this tick (``dirty``) and
the block is older than ``_DIRTY_REFRESH_S``. The watcher additionally
classifies at most one project per tick (round-robin), so a cold workspace
back-fills gradually without ever blocking the loop.

This sink writes ONLY the ``classification`` key. The user's manual
top-level ``category`` field in the same file is never touched — it is the
override that wins over the computed value everywhere the block is read.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.reader import read_json

_REFRESH_S = 600.0        # steady-state recompute interval per project
_DIRTY_REFRESH_S = 120.0  # projects with fresh events refresh sooner
_WALK_BUDGET_S = 8.0      # per-project walk budget


def _age_s(computed_at) -> float:
    try:
        dt = datetime.strptime(str(computed_at), "%Y-%m-%dT%H:%M:%SZ")
        return max(0.0, (datetime.now(timezone.utc)
                         - dt.replace(tzinfo=timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def is_stale(xo_dir: Path, *, dirty: bool = False) -> bool:
    doc = read_json(xo_dir / "project.json") or {}
    block = doc.get("classification")
    if not isinstance(block, dict):
        return True
    return _age_s(block.get("computed_at")) > (_DIRTY_REFRESH_S if dirty else _REFRESH_S)


def apply(xo_dir: Path, pid: str, *, dirty: bool = False) -> bool:
    """Recompute + persist this project's classification when stale.
    Returns ``True`` if the file changed."""
    if not is_stale(xo_dir, dirty=dirty):
        return False
    # Lazy import: environments_graph pulls in the whole tree walker, which
    # the sink package must not load at watcher boot.
    from services.cowork_agent.visualizer.environments_graph import (
        classify_one_project,
    )
    block = classify_one_project(pid, deadline=time.monotonic() + _WALK_BUDGET_S)
    if block is None:
        return False
    doc = read_json(xo_dir / "project.json") or {}
    doc["classification"] = block
    write_json_atomic(xo_dir / "project.json", doc)
    return True
