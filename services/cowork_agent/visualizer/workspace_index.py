"""Workspace discovery — every project the watcher should track.

Thin wrapper around ``services.cowork_agent.project_layout`` that
hides whether a project is scaffolded (has ``.xo/project.json``) or
bare. The watcher tracks every directory under ``xo_projects_root``
that *could* receive Claude/OpenClaw events — scaffolding state is
the watcher's own decision (the ``project_json`` sink fills identity
on first sight).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from services.cowork_agent.project_layout import (
    list_projects,
    list_unscaffolded_dirs,
    xo_projects_root,
)


def list_project_ids() -> list[str]:
    """All project ids under ``~/xo-projects/`` (scaffolded + bare).

    Sorted alphabetically. Excludes the workspace-tier ``.xo/``
    directory itself (which starts with ``.`` and is already filtered
    by the underlying helpers).
    """
    out: set[str] = set()
    for entry in list_projects():
        name = entry.get("name")
        if name:
            out.add(name)
    for entry in list_unscaffolded_dirs():
        name = entry.get("name")
        if name:
            out.add(name)
    return sorted(out)


def iter_project_xo_dirs() -> Iterable[tuple[str, "Path"]]:
    """Yield ``(project_id, <project>/.xo/)`` for every project.

    The watcher's per-project sink loop uses this. Imported lazily to
    keep ``Path`` out of the public type surface where it isn't
    needed.
    """
    from services.cowork_agent.project_layout import xo_dir

    for pid in list_project_ids():
        yield pid, xo_dir(pid)


def workspace_root() -> "Path":
    """Re-exported for sinks that need the root without importing
    ``project_layout`` directly."""
    return xo_projects_root()
