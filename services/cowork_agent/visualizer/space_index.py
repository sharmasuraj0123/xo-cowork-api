"""Space graph builder — maps ``~/xo-projects`` to the xo-atlas space.json shape.

Pure reader: scans the projects root and returns the graph document that
``v3.html`` consumes. Writes nothing.
Served by ``routers/space.py`` (GET /space/data/space.json).

Watcher-owner seam: ``materialize(path)`` writes the same output atomically
for event-driven freshness; nothing calls it in v1 — see
docs/space-module-design.md.
"""

from __future__ import annotations

import math
import os
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from services.cowork_agent.project_layout import (
    list_projects,
    project_dir,
    xo_projects_root,
)

# Muted category palette, >=3:1 contrast on the UI background #0b0c0f.
_PALETTE = [
    "#a2b56b", "#7fb3c8", "#c8a06b", "#b58a9e",
    "#8fbf9f", "#c4bd72", "#9a93d0", "#c88585",
]

_CODE_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c",
    ".cpp", ".h", ".sh", ".ps1", ".html", ".css", ".json", ".yml",
    ".yaml", ".toml", ".sql",
}
_DOC_EXT = {".md", ".txt", ".rst", ".pdf", ".docx"}

# Mirrors routers/cowork_agent/bff/filters.is_hidden_name — duplicated
# because services must not import from routers (dependency direction).
_TEMP_SUFFIXES = (".tmp", ".swp", ".swo", ".bak", ".orig")
_TEMP_PREFIXES = ("~$",)
_SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build", "target"}

# Hard bounds — the API runs inside every user's workspace, so the builder
# must stay cheap regardless of how much is on disk. Each stage is capped.
MAX_LEAVES_PER_PROJECT = 400          # per-project output bound (newest-first)
MAX_TOTAL_LEAVES = 1500               # whole-graph output bound (browser must render it)
MAX_FILES_SCANNED_PER_PROJECT = 2000  # traversal bound (walk stops here)
BUILD_DEADLINE_S = 10.0               # whole-build wall-clock bound


def _is_hidden(name: str) -> bool:
    if not name or name.startswith("."):
        return True
    if name in _SKIP_DIRS:
        return True
    if any(name.endswith(s) for s in _TEMP_SUFFIXES):
        return True
    return any(name.startswith(p) for p in _TEMP_PREFIXES)


def _shape_for(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in _CODE_EXT:
        return "disc"
    if ext in _DOC_EXT:
        return "ring"
    return "diamond"


def _mtime_date(path: Path) -> str:
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def _iter_files_pruned(base: Path):
    """Yield files under ``base``, pruning hidden/junk dirs DURING traversal.

    ``os.walk`` with in-place ``dirnames`` filtering never *enters* a pruned
    directory — a project with a 100k-file node_modules costs nothing here.
    (``rglob`` + post-filter would enumerate all of it first: filtering after
    enumeration is O(everything on disk); pruning is O(what we keep).)"""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(n for n in dirnames if not _is_hidden(n))
        for name in sorted(filenames):
            if not _is_hidden(name):
                yield Path(dirpath) / name


_GIT_TIMEOUT_S = 5


def _git_facts(pdir: Path) -> tuple[dict[str, str], Optional[str]]:
    """First-added date per file + the project's first-commit date, from
    ``git log``. Any failure (not a repo, no git binary, no commits,
    timeout) → empty facts, and callers fall back to mtime dates.

    ``%x01`` makes git emit a control byte prefix on each commit-date line
    so file-path lines can never be confused with dates."""
    try:
        out = subprocess.run(
            [
                "git", "-C", str(pdir), "log",
                "--reverse", "--date=short", "--diff-filter=A",
                "--pretty=format:%x01%ad", "--name-only",
            ],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
    except Exception:
        return {}, None
    if out.returncode != 0:
        return {}, None

    created: dict[str, str] = {}
    first_commit: Optional[str] = None
    current: Optional[str] = None
    for line in out.stdout.splitlines():
        if line.startswith("\x01"):
            current = line[1:].strip()
            if first_commit is None:
                first_commit = current
        elif line.strip() and current:
            # oldest-first (--reverse): setdefault keeps the first add,
            # surviving later delete/re-add churn.
            created.setdefault(line.strip(), current)
    return created, first_commit


def _walk_project(pid: str, cat: str, created_dates: dict) -> tuple[list[dict], list[dict]]:
    """Groups + leaves for one project. Level-1 dirs become groups; files at
    any depth roll up into their level-1 group; root files get a root group.
    Traversal is pruned and stops at MAX_FILES_SCANNED_PER_PROJECT.
    Raises OSError if the project directory is unreadable (caller skips)."""
    pdir = project_dir(pid)
    groups: list[dict] = []
    leaves: list[dict] = []
    scanned = 0

    def add_leaf(group_id: str, rel: str, f: Path) -> None:
        leaves.append({
            "id": f"{pid}:{rel}",
            "group": group_id,
            "shape": _shape_for(f.name),
            "tag": (f.suffix.lstrip(".").upper() or "FILE"),
            "label": f.name,
            "date": created_dates.get(rel) or _mtime_date(f),
            "blurb": rel,
            "path": f"{pid}/{rel}",
        })

    entries = sorted(pdir.iterdir(), key=lambda e: e.name)
    root_files = [e for e in entries if e.is_file() and not _is_hidden(e.name)]
    subdirs = [e for e in entries if e.is_dir() and not _is_hidden(e.name)]

    if root_files:
        groups.append({
            "id": f"g_{pid}_root", "cat": cat,
            "label": "(root)", "blurb": "Files at the project root.",
        })
        for f in root_files:
            if scanned >= MAX_FILES_SCANNED_PER_PROJECT:
                break
            add_leaf(f"g_{pid}_root", f.name, f)
            scanned += 1

    for d in subdirs:
        if scanned >= MAX_FILES_SCANNED_PER_PROJECT:
            break
        gid = f"g_{pid}_{d.name}"
        count = 0
        for f in _iter_files_pruned(d):
            if scanned >= MAX_FILES_SCANNED_PER_PROJECT:
                print(f"space_index: {pid}: scan budget hit "
                      f"({MAX_FILES_SCANNED_PER_PROJECT}); rest of project skipped")
                break
            add_leaf(gid, f.relative_to(pdir).as_posix(), f)
            count += 1
            scanned += 1
        if count:
            groups.append({
                "id": gid, "cat": cat,
                "label": d.name, "blurb": f"{count} files",
            })

    if len(leaves) > MAX_LEAVES_PER_PROJECT:
        dropped = len(leaves) - MAX_LEAVES_PER_PROJECT
        leaves.sort(key=lambda leaf: leaf["date"], reverse=True)
        leaves = leaves[:MAX_LEAVES_PER_PROJECT]
        print(f"space_index: {pid}: dropped {dropped} oldest leaves (cap {MAX_LEAVES_PER_PROJECT})")

    return groups, leaves


def build_space_data() -> dict:
    root = xo_projects_root()
    projects = list_projects()

    categories: dict = {}
    hub_angles: dict = {}
    hubs: list[dict] = []
    groups: list[dict] = []
    leaves: list[dict] = []
    milestones: list[dict] = []

    n = max(len(projects), 1)
    deadline = time.monotonic() + BUILD_DEADLINE_S
    for i, meta in enumerate(projects):
        if time.monotonic() > deadline:
            print(f"space_index: build deadline ({BUILD_DEADLINE_S}s) hit; "
                  f"skipped {len(projects) - i} of {len(projects)} projects")
            break
        pid = str(meta["name"])
        cat = f"p_{pid}"
        display = str(meta.get("display_name") or pid)
        created_dates, first_commit = _git_facts(project_dir(pid))

        try:
            p_groups, p_leaves = _walk_project(pid, cat, created_dates)
        except OSError:
            print(f"space_index: skipping unreadable project {pid}")
            continue

        categories[cat] = {
            "name": display,
            "color": _PALETTE[i % len(_PALETTE)],
        }
        hub_angles[cat] = -math.pi / 2 + i * 2 * math.pi / n
        hubs.append({
            "id": cat, "cat": cat, "label": display,
            "blurb": str(meta.get("description") or f"Project {display}."),
        })
        groups.extend(p_groups)
        leaves.extend(p_leaves)
        if first_commit:
            milestones.append({"d": first_commit, "t": f"{display} first commit"})

    if len(leaves) > MAX_TOTAL_LEAVES:
        dropped = len(leaves) - MAX_TOTAL_LEAVES
        leaves.sort(key=lambda leaf: leaf["date"], reverse=True)
        leaves = leaves[:MAX_TOTAL_LEAVES]
        kept_groups = {leaf["group"] for leaf in leaves}
        groups = [g for g in groups if g["id"] in kept_groups]
        print(f"space_index: dropped {dropped} oldest leaves workspace-wide "
              f"(cap {MAX_TOTAL_LEAVES}); empty groups pruned")

    today = date.today()
    if leaves:
        dates = sorted(leaf["date"] for leaf in leaves)
        start = (date.fromisoformat(dates[0]) - timedelta(days=7)).isoformat()
        end = (date.fromisoformat(dates[-1]) + timedelta(days=7)).isoformat()
    else:
        start = (today - timedelta(days=7)).isoformat()
        end = (today + timedelta(days=7)).isoformat()

    return {
        "meta": {
            "title": "Space",
            "tagline": "an xo-projects knowledge graph",
            "mappedOn": today.strftime("%d %B %Y"),
            "workspace": str(root),
        },
        "categories": categories,
        "hubAngles": hub_angles,
        "timeline": {"start": start, "end": end},
        "root": {
            "id": "xo",
            "label": "xo-projects",
            "blurb": f"{len(projects)} projects under {root}",
        },
        "hubs": hubs,
        "groups": groups,
        "leaves": leaves,
        "ties": [],
        "milestones": milestones,
    }


def materialize(path: Path) -> None:
    """Atomically write ``build_space_data()`` output to ``path``.

    NOT called anywhere in v1. Integration seam for the watcher owner:
    call from the workspace re-aggregate step in ``watcher.tick()`` for
    event-driven freshness, then point the route at the file. See
    docs/space-module-design.md."""
    from services.cowork_agent.visualizer.atomic_write import write_json_atomic

    write_json_atomic(path, build_space_data())
