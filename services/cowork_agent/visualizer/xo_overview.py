"""Bundle the workspace's .xo/ state for the Space Overview tab.

Read-only: the watcher service owns .xo/ and this module never writes there.
Missing files degrade to nulls; a missing .xo/ directory raises so the route
can answer a truthful 503. Reads are bounded so a corrupt or huge file cannot
stall the API.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from services.cowork_agent.project_layout import xo_projects_root
from services.cowork_agent.visualizer.space_index import _is_hidden

_MAX_JSON_BYTES = 1_000_000     # stats.json is ~60KB today; 1MB is generous
_TIMELINE_TAIL_BYTES = 131_072  # read at most this much of timeline.jsonl
_TIMELINE_EVENTS = 40           # newest events shipped

# Data-mode tree bounds: the tree is a browsing aid, not an index — depth and
# breadth are capped and every cut is marked so the UI can say "N more".
_TREE_MAX_DEPTH = 3
_TREE_MAX_ENTRIES = 40          # per directory, dirs first
_TREE_MAX_NODES = 2500          # whole-tree budget
_TREE_SCAN_CAP = 400            # entries enumerated per directory before cutting


def _read_json(path: Path):
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return None
        with open(path, encoding="utf-8", errors="ignore") as fp:
            # NaN/Infinity tokens become null: JSONResponse serializes with
            # allow_nan=False, and one poisoned value must not 500 the route.
            return json.load(fp, parse_constant=lambda _c: None)
    except (OSError, ValueError):
        return None


def _timeline_tail(path: Path) -> list[dict]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as fp:
            fp.seek(max(0, size - _TIMELINE_TAIL_BYTES))
            chunk = fp.read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    events = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events[-_TIMELINE_EVENTS:][::-1]


def _scan_dir(base: Path) -> list[Path]:
    """Visible entries of a directory, enumeration-capped, symlinks excluded.

    Symlinks are skipped entirely: following them would let the tree escape
    the workspace root and disclose out-of-root names/sizes (space_index.py
    makes the same choice via os.walk's followlinks=False). The cap bounds
    stat cost on pathological million-entry directories."""
    out: list[Path] = []
    it = base.iterdir()
    for e in it:
        if _is_hidden(e.name):
            continue
        try:
            if e.is_symlink():
                continue
        except OSError:
            continue
        out.append(e)
        if len(out) >= _TREE_SCAN_CAP:
            break
    return out


def _build_tree(base: Path, depth: int, budget: dict) -> dict:
    """One directory as {name, type, children[], more} with pruned traversal."""
    node: dict = {"name": base.name, "type": "dir", "children": []}
    try:
        entries = _scan_dir(base)
    except OSError:
        return node
    if depth >= _TREE_MAX_DEPTH or budget["nodes"] <= 0:
        if entries:
            node["more"] = len(entries)
        return node
    entries.sort(key=lambda e: (e.is_file(), e.name.lower()))
    for e in entries[:_TREE_MAX_ENTRIES]:
        if budget["nodes"] <= 0:
            break
        budget["nodes"] -= 1
        if e.is_dir():
            node["children"].append(_build_tree(e, depth + 1, budget))
        else:
            try:
                size = e.stat().st_size
            except OSError:
                size = None
            node["children"].append({"name": e.name, "type": "file", "size": size})
    cut = len(entries) - len(node["children"])
    if cut > 0:
        node["more"] = cut
    return node


def _xo_inventory(xo_dir: Path) -> list[dict]:
    """Flat listing of what the watcher has collected under .xo/."""
    items: list[dict] = []
    try:
        entries = sorted(xo_dir.iterdir(), key=lambda e: e.name.lower())
    except OSError:
        return items
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if e.is_symlink():
                continue
            if e.is_dir():
                for sub in sorted(e.iterdir(), key=lambda s: s.name.lower())[:12]:
                    if sub.is_file():
                        st = sub.stat()
                        items.append({"name": f"{e.name}/{sub.name}",
                                      "size": st.st_size, "mtime": st.st_mtime})
            else:
                st = e.stat()
                items.append({"name": e.name, "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            continue
    return items


def build_xo_overview() -> dict:
    root = xo_projects_root()
    xo_dir = root / ".xo"
    if not xo_dir.is_dir():
        raise FileNotFoundError(f".xo directory not found at {xo_dir}")

    sessions_list = _read_json(xo_dir / "sessions" / "sessionslist.json")
    tree = _build_tree(root, 0, {"nodes": _TREE_MAX_NODES})
    tree["name"] = root.name
    return {
        "root": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest": _read_json(xo_dir / "xo.json"),
        "workspace": _read_json(xo_dir / "workspace.json"),
        "stats": _read_json(xo_dir / "stats.json"),
        "activity": _read_json(xo_dir / "activity.json"),
        "timeline": _timeline_tail(xo_dir / "timeline.jsonl"),
        "known_sessions": len(sessions_list) if isinstance(sessions_list, dict) else None,
        "tree": tree,
        "xo_files": _xo_inventory(xo_dir),
    }
