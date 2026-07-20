"""One project, expanded: its file/folder tree plus its agent sessions.

The Environments space shows each project as a single node; drilling into a
project opens this graph — the same file/folder structure the Projects space
renders for one project (via space_index._walk_project), joined with the
sessions that ran in that project (from the multi-runtime session telemetry).

Space-schema payload (root -> hubs -> groups -> leaves), so the existing
graph renderer, Timeline, and Six Degrees consume it unchanged. Two hubs:
Files (the folder groups) and Sessions (one group of session leaves).
"""

from __future__ import annotations

import math
import os
import time
from datetime import date, datetime, timedelta, timezone

from services.cowork_agent.project_layout import (
    list_projects,
    project_dir,
    xo_projects_root,
)
from services.cowork_agent.visualizer.space_index import (
    BUILD_DEADLINE_S,
    MAX_TOTAL_LEAVES,
    _git_facts,
    _walk_project,
)
from services.cowork_agent.visualizer.sessions_graph import (
    _fmt_tokens,
    _leaf_shape,
    _parse_started,
    _session_blurb,
)
from services.cowork_agent.visualizer.session_telemetry import (
    build_session_telemetry,
)

_FILES_COLOR = "#6fb7e0"
_SESS_COLOR = "#c8a06b"
_MAX_SESSION_LEAVES = 200


class ProjectNotFound(RuntimeError):
    """Raised when a drill-down targets an unknown project id."""


def _sessions_for_project(pid: str) -> list[dict]:
    """Every session whose project_path is this project's dir or a checkout
    under it (worktrees live at <project>/.claude/worktrees/<name>). Exact-or-
    under with a trailing separator so a sibling like `foo-api` can't match
    `foo`."""
    pdir = str(project_dir(pid)).rstrip("/")
    prefix = pdir + os.sep
    try:
        telemetry = build_session_telemetry()
    except Exception as exc:
        print(f"project_detail_graph: session telemetry unavailable ({exc})")
        return []
    out = []
    for s in telemetry.get("sessions", []):
        p = str(s.get("project_path") or "").rstrip("/")
        if p == pdir or p.startswith(prefix):
            out.append(s)
    return out


def build_project_detail_graph(pid: str) -> dict:
    meta = next((m for m in list_projects() if str(m["name"]) == pid), None)
    if meta is None:
        raise ProjectNotFound(pid)
    display = str(meta.get("display_name") or pid)
    root = xo_projects_root()

    created_dates, first_commit, _commits = _git_facts(project_dir(pid))
    deadline = time.monotonic() + BUILD_DEADLINE_S
    try:
        file_groups, file_leaves = _walk_project(pid, "files", created_dates, deadline)
    except OSError:
        file_groups, file_leaves = [], []

    # Sessions become a second branch: one "Sessions" group of session leaves.
    sessions = _sessions_for_project(pid)
    sess_group = {"id": "g_sessions", "cat": "sessions", "label": "Sessions",
                  "blurb": f"{len(sessions)} session{'s' if len(sessions) != 1 else ''}",
                  "ftype": "app", "facts": {}, "shape": "disc", "xotype": "session"}
    sess_leaves: list[dict] = []
    tok_total = 0.0
    sessions.sort(key=lambda s: str(s.get("started_at") or ""), reverse=True)
    for s in sessions[:_MAX_SESSION_LEAVES]:
        started = _parse_started(s.get("started_at"))
        if started is None:
            continue
        tok_total += float(s.get("tokens") or 0)
        sess_leaves.append({
            "id": f"sess:{s.get('key') or s.get('id')}",
            "group": "g_sessions",
            "shape": _leaf_shape(s),
            "tag": str(s.get("model") or "unknown"),
            "label": started.strftime("%b %d · %H:%M"),
            "date": started.date().isoformat(),
            "blurb": _session_blurb(s),
            "path": str(s.get("project_path") or ""),
            "xotype": "session",
        })

    groups = file_groups + ([sess_group] if sessions else [])
    leaves = file_leaves + sess_leaves

    if len(leaves) > MAX_TOTAL_LEAVES:
        dropped = len(leaves) - MAX_TOTAL_LEAVES
        leaves.sort(key=lambda leaf: leaf["date"], reverse=True)
        leaves = leaves[:MAX_TOTAL_LEAVES]
        kept = {leaf["group"] for leaf in leaves}
        groups = [g for g in groups if g["id"] in kept]
        print(f"project_detail_graph[{pid}]: dropped {dropped} oldest leaves")

    categories = {"files": {"name": "Files", "color": _FILES_COLOR}}
    hub_angles = {"files": -math.pi / 2}
    hubs = [{"id": "files", "cat": "files", "label": "Files",
             "blurb": f"{len(file_leaves)} files", "xotype": "output"}]
    if sessions:
        categories["sessions"] = {"name": "Sessions", "color": _SESS_COLOR}
        hub_angles["sessions"] = math.pi / 2
        hubs.append({"id": "sessions", "cat": "sessions", "label": "Sessions",
                     "blurb": f"{len(sessions)} sessions · {_fmt_tokens(tok_total)} tokens",
                     "xotype": "session"})

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
            "title": display,
            "tagline": "one project, expanded",
            "mappedOn": today.strftime("%d %B %Y"),
            "workspace": str(root),
            "noun": "artifacts",
            "rootEdgeLabel": "part of this project",
            "leafDateLabel": "Born",
            "kickers": {"hub": "Section", "group": "Folder"},
            # Drill-down breadcrumb: the client shows a back control that
            # returns to the Environments space.
            "drill": {"pid": pid, "label": display, "backTo": "environments"},
            "shapeLegend": [
                {"shape": "disc", "label": "app"},
                {"shape": "ring", "label": "one-pager"},
                {"shape": "stack", "label": "docs"},
                {"shape": "slab", "label": "slides"},
                {"shape": "diamond", "label": "unknown"},
            ],
            "introEyebrow": "One project, expanded",
            "introTitle": f"Inside {display}.",
            "intro": f"{len(file_leaves)} files across {len(file_groups)} folders"
                     + (f", and {len(sessions)} agent sessions" if sessions else "")
                     + " — the full contents of this project.",
            "timelineTitle": f"{display}, over time.",
            "timelineSub": "Scrub through when this project's files and sessions "
                           "landed.",
        },
        "categories": categories,
        "hubAngles": hub_angles,
        "timeline": {"start": start, "end": end},
        "root": {"id": f"proj:{pid}", "label": display,
                 "blurb": f"{len(file_leaves)} files · {len(sessions)} sessions"},
        "hubs": hubs,
        "groups": groups,
        "leaves": leaves,
        "ties": [],
        "milestones": ([{"d": first_commit, "t": f"{display} first commit"}]
                       if first_commit else []),
    }
