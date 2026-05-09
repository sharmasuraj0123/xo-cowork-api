"""
Canonical project layout for ~/xo-projects/<name>/.

A project is just a folder. It is backend-agnostic: any agent (claude_code,
openclaw, future tools) can launch against it. The on-disk shape is what
makes the agent perform well — this module owns it.

    ~/xo-projects/<name>/
    ├── AGENTS.md            stable prefix, universal contract
    ├── OBJECTIVES.md        north-star outcomes
    ├── WORKSPACE.md         current state of play
    ├── CLAUDE.md            one-line pointer to AGENTS.md
    └── .xo/
        ├── project.json     {name, display_name, description, created_at}
        ├── memory/{semantic,episodic,procedural,working}/
        ├── sessions/        sessionslist.json (metadata only) + compressed/ + index.md
        ├── artifacts/{drafts,final}/
        ├── state/           SOUL.md, STATUS.md, IDENTITY.md, USER.md
        ├── skills/{user-built,learned}/
        └── context/         config.json, cache.md

Concerns:
- path resolution (env-driven root, every subfolder)
- idempotent scaffolding (re-running fills in missing pieces, never clobbers)
- project metadata read/write
- filesystem-driven listing (no backend coupling)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.helpers import normalize_agent_id

# ── Template source ────────────────────────────────────────────────────────────

_SKIP_NAMES = {".git"}


_BUNDLED_TEMPLATE = Path(__file__).parent / "project_template"


def _template_dir() -> Path:
    """Return the project template directory.

    Priority: ``XO_PROJECT_TEMPLATE`` env var → bundled ``project_template/``
    shipped with this package (always present).
    """
    raw = (os.getenv("XO_PROJECT_TEMPLATE", "") or "").strip()
    if raw:
        t = Path(raw).expanduser().resolve()
        if t.is_dir():
            return t
    return _BUNDLED_TEMPLATE


def _copy_template(src: Path, dst: Path) -> None:
    """Recursively copy src → dst, skipping .git, never clobbering existing files."""
    for item in src.iterdir():
        if item.name in _SKIP_NAMES:
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_template(item, target)
        elif not target.exists():
            target.write_bytes(item.read_bytes())


# ── Roots ─────────────────────────────────────────────────────────────────────


def xo_projects_root() -> Path:
    """User-facing projects directory.

    Sourced from ``XO_PROJECTS_ROOT`` env var; defaults to ``~/xo-projects``.
    Created on read so callers never have to guard for first-run.
    """
    raw = (os.getenv("XO_PROJECTS_ROOT", "") or "").strip() or "~/xo-projects"
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


# ── Per-project paths ─────────────────────────────────────────────────────────


def project_dir(name: str) -> Path:
    return xo_projects_root() / normalize_agent_id(name)


def xo_dir(name: str) -> Path:
    return project_dir(name) / ".xo"


def sessions_dir(name: str) -> Path:
    return xo_dir(name) / "sessions"


def memory_dir(name: str) -> Path:
    return xo_dir(name) / "memory"


def state_dir(name: str) -> Path:
    return xo_dir(name) / "state"


def artifacts_dir(name: str) -> Path:
    return xo_dir(name) / "artifacts"


def skills_dir(name: str) -> Path:
    return xo_dir(name) / "skills"


def context_dir(name: str) -> Path:
    return xo_dir(name) / "context"


def project_metadata_path(name: str) -> Path:
    return xo_dir(name) / "project.json"


# ── Scaffold ──────────────────────────────────────────────────────────────────


def scaffold_project(
    name: str,
    *,
    display_name: str | None = None,
    description: str | None = None,
) -> dict:
    """Create or fill in the canonical project tree from the template.

    Copies every file from the template directory (``~/ultimate-work`` or
    ``XO_PROJECT_TEMPLATE`` env var) into the project folder. Idempotent:
    existing files are never overwritten; missing files and directories are
    added.

    ``sessions/sessions.json`` is always ensured — it is a system requirement
    not present in the user template.

    Returns the project metadata dict (created or already present).
    """
    pid = normalize_agent_id(name)
    pdir = project_dir(pid)
    xdir = xo_dir(pid)

    pdir.mkdir(parents=True, exist_ok=True)
    xdir.mkdir(parents=True, exist_ok=True)

    _copy_template(_template_dir(), pdir)

    # sessionslist.json is a system file the harness reads/writes; not in the template.
    # It holds session metadata only — messages stay in the provider's own storage.
    sessions_dir = xdir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    sessions_json = sessions_dir / "sessionslist.json"
    if not sessions_json.exists():
        sessions_json.write_text("{}\n", encoding="utf-8")

    return _upsert_metadata(pid, display_name=display_name, description=description)


def _upsert_metadata(
    pid: str,
    *,
    display_name: str | None,
    description: str | None,
) -> dict:
    """Read .xo/project.json, fill in any missing fields, optionally update
    display_name/description, write back, return the result."""
    meta_path = project_metadata_path(pid)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
    else:
        meta = {}

    changed = False

    if "name" not in meta:
        meta["name"] = pid
        changed = True

    if "created_at" not in meta:
        meta["created_at"] = datetime.now(timezone.utc).isoformat()
        changed = True

    if display_name is not None:
        if meta.get("display_name") != display_name:
            meta["display_name"] = display_name
            changed = True
    elif "display_name" not in meta:
        meta["display_name"] = pid
        changed = True

    if description is not None:
        if meta.get("description") != description:
            meta["description"] = description
            changed = True
    elif "description" not in meta:
        meta["description"] = ""
        changed = True

    if changed:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    return dict(meta)


# ── Read / list ───────────────────────────────────────────────────────────────


def load_project(name: str) -> dict | None:
    """Read .xo/project.json for an existing project, or None if absent."""
    path = project_metadata_path(normalize_agent_id(name))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def project_exists(name: str) -> bool:
    """True iff the project folder has a .xo/project.json record."""
    return project_metadata_path(normalize_agent_id(name)).exists()


def list_projects() -> list[dict]:
    """Filesystem-driven project list. Backend-agnostic.

    Returns one dict per directory under xo-projects that has
    ``.xo/project.json``. Hidden directories are skipped. Missing or
    malformed metadata yields a minimal entry with just ``name`` and
    ``path`` so the UI can still surface the folder.
    """
    root = xo_projects_root()
    out: list[dict] = []
    if not root.exists():
        return out

    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        meta_path = entry / ".xo" / "project.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta.setdefault("name", entry.name)
        meta.setdefault("display_name", meta["name"])
        meta["path"] = str(entry)
        out.append(meta)

    return out
