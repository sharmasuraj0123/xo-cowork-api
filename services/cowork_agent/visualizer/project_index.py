"""Backend-neutral project routing for the watcher.

A session's working directory tells us which xo-project it belongs to.
This module's one job: given a literal ``cwd`` string from a runtime
event, return the top-level project id under ``xo_projects_root()``.

Anything backend-specific — e.g. Claude Code's encoded jsonl directory
naming (``~/.claude/projects/<encoded-cwd>/<sid>.jsonl``) — lives next
to the adapter that needs it. See
``services/cowork_agent/adapters/claude_code/_project_encoding.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from services.cowork_agent.project_layout import xo_projects_root


def project_id_for_cwd(cwd: str) -> Optional[str]:
    """Authoritative lookup. ``cwd`` is the literal working directory
    a runtime event reports. Returns the top-level project id under
    ``xo_projects_root()``, or ``None`` if the cwd is outside the
    workspace.
    """
    if not cwd:
        return None
    p = Path(cwd)
    try:
        p = p.resolve()
    except OSError:
        return None
    root = xo_projects_root()
    if not p.is_relative_to(root):
        return None
    rel = p.relative_to(root)
    if not rel.parts:
        return None
    return rel.parts[0]
