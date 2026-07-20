"""Git commit history for the Projects-space Timeline — one entry per commit,
across every project and its in-repo worktrees (``<project>/.claude/
worktrees/<name>``, a full checkout with its own ``.git``).

Every commit becomes one "timeline event" in the schema the client's
light-cone renderer expects: insertions/deletions/files touched, so a bowtie
glyph (green cone up, red cone down, meeting at the commit's point on the
time axis) can be drawn without any further computation client-side. See
adapters/<runtime>/commit_diffs.py for the Sessions-space equivalent, which
emits the same event shape from agent Edit/Write tool calls instead of git.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from services.cowork_agent.project_layout import list_projects, project_dir
from services.cowork_agent.visualizer.space_index import _is_hidden
from services.cowork_agent.visualizer.environments_graph import (
    ENV_CATEGORIES,
    _ENV_LABEL,
    classify_projects,
)

_GIT_TIMEOUT_S = 8
MAX_COMMITS_PER_REPO = 400       # per project checkout (main or one worktree)
MAX_EVENTS_TOTAL = 4000          # whole-payload bound (client renders this)
BUILD_DEADLINE_S = 15.0
_SEP = "\x01"                    # never appears in a commit subject/author


def _worktrees(pdir: Path) -> list[tuple[str, Path]]:
    """[(name, path), ...] for every in-repo worktree under .claude/worktrees."""
    wt_dir = pdir / ".claude" / "worktrees"
    out: list[tuple[str, Path]] = []
    try:
        if not wt_dir.is_dir():
            return out
        for e in sorted(wt_dir.iterdir(), key=lambda p: p.name):
            if e.is_dir() and not _is_hidden(e.name) and (e / ".git").exists():
                out.append((e.name, e))
    except OSError:
        pass
    return out


def _repo_commits(repo_dir: Path, limit: int) -> list[dict]:
    """One dict per commit: sha/date/author/message/insertions/deletions/files.

    ``--numstat`` after the pretty header line gives one "add\\tdel\\tpath"
    row per touched file; a blank line ends each commit's file block. Binary
    files report "-\\t-" for both counts (excluded from the line totals but
    still counted as a touched file)."""
    try:
        out = subprocess.run(
            [
                "git", "-C", str(repo_dir), "log",
                f"-{limit}", "--date=iso-strict", "--numstat",
                f"--pretty=format:{_SEP}%H{_SEP}%ad{_SEP}%an{_SEP}%s",
            ],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []

    commits: list[dict] = []
    cur: dict | None = None
    for line in out.stdout.splitlines():
        if line.startswith(_SEP):
            if cur is not None:
                commits.append(cur)
            _, sha, date, author, subject = line.split(_SEP, 4)
            cur = {"sha": sha, "date": date, "author": author, "message": subject,
                  "insertions": 0, "deletions": 0, "files": []}
        elif line.strip() and cur is not None:
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            add, dele, path = parts
            if add.isdigit():
                cur["insertions"] += int(add)
            if dele.isdigit():
                cur["deletions"] += int(dele)
            cur["files"].append(path)
    if cur is not None:
        commits.append(cur)
    return commits


def build_project_commit_timeline() -> dict:
    """Every project's (and its worktrees') commits as timeline events."""
    projects = list_projects()
    events: list[dict] = []
    projects_seen: list[dict] = []
    deadline = time.monotonic() + BUILD_DEADLINE_S

    for meta in projects:
        if time.monotonic() > deadline:
            print("commit_timeline: build deadline hit; remaining projects skipped")
            break
        pid = str(meta["name"])
        display = str(meta.get("display_name") or pid)
        pdir = project_dir(pid)
        checkouts = [(None, pdir)] + _worktrees(pdir)
        project_event_count = 0

        for wt_name, repo_dir in checkouts:
            for c in _repo_commits(repo_dir, MAX_COMMITS_PER_REPO):
                events.append({
                    "id": f"{pid}:{wt_name or 'main'}:{c['sha'][:12]}",
                    "kind": "commit",
                    "date": c["date"],
                    "project": pid,
                    "project_label": display,
                    "worktree": wt_name,
                    "title": c["message"] or "(no message)",
                    "author": c["author"],
                    "sha": c["sha"][:10],
                    "insertions": c["insertions"],
                    "deletions": c["deletions"],
                    "files": c["files"][:12],
                    "files_count": len(c["files"]),
                })
                project_event_count += 1
        if project_event_count:
            projects_seen.append({"id": pid, "label": display,
                                  "commits": project_event_count,
                                  "worktrees": len(checkouts) - 1})

    events.sort(key=lambda e: e["date"], reverse=True)
    truncated = len(events) > MAX_EVENTS_TOTAL
    if truncated:
        events = events[:MAX_EVENTS_TOTAL]

    dates = [e["date"] for e in events]
    return {
        "kind": "commits",
        "events": events,
        "projects": sorted(projects_seen, key=lambda p: -p["commits"]),
        "range": {"start": min(dates) if dates else None,
                  "end": max(dates) if dates else None},
        "truncated": truncated,
    }


def build_environment_commit_timeline() -> dict:
    """Every project's commits (git history, same as build_project_commit_timeline)
    tagged with which of the 5 Environments clusters the project belongs to —
    the Growth Trunk's data source: one trunk per cluster, width = cumulative
    net lines of every project in that cluster, summed."""
    events: list[dict] = []
    clusters_seen: dict[str, int] = {cid: 0 for cid in ENV_CATEGORIES}
    deadline = time.monotonic() + BUILD_DEADLINE_S

    for c in classify_projects(deadline):
        if time.monotonic() > deadline:
            print("commit_timeline: environment build deadline hit; remaining projects skipped")
            break
        pid, display, category = c["id"], c["label"], c["category"]
        pdir = project_dir(pid)
        checkouts = [(None, pdir)] + _worktrees(pdir)
        project_commits = 0

        for wt_name, repo_dir in checkouts:
            for commit in _repo_commits(repo_dir, MAX_COMMITS_PER_REPO):
                events.append({
                    "id": f"{pid}:{wt_name or 'main'}:{commit['sha'][:12]}",
                    "kind": "commit",
                    "date": commit["date"],
                    "project": pid,
                    "project_label": display,
                    "category": category,
                    "worktree": wt_name,
                    "title": commit["message"] or "(no message)",
                    "author": commit["author"],
                    "sha": commit["sha"][:10],
                    "insertions": commit["insertions"],
                    "deletions": commit["deletions"],
                    "files": commit["files"][:12],
                    "files_count": len(commit["files"]),
                })
                project_commits += 1
        if project_commits:
            clusters_seen[category] += 1

    events.sort(key=lambda e: e["date"], reverse=True)
    truncated = len(events) > MAX_EVENTS_TOTAL
    if truncated:
        events = events[:MAX_EVENTS_TOTAL]

    dates = [e["date"] for e in events]
    return {
        "kind": "environment_commits",
        "events": events,
        "clusters": [{"id": cid, "label": _ENV_LABEL[cid], "projects": clusters_seen[cid]}
                    for cid in ENV_CATEGORIES],
        "range": {"start": min(dates) if dates else None,
                  "end": max(dates) if dates else None},
        "truncated": truncated,
    }
