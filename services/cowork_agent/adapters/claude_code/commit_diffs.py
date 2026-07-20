"""Real code diffs derived from Claude Code transcripts, for the Sessions-
space Timeline. Claude Code's Edit/Write/MultiEdit tool calls carry their
full before/after content right in the transcript (old_string/new_string,
or content for a new file), so real insertions/deletions are computable
without touching disk — the same "commit" event shape commit_timeline.py
emits from git, so the client's light-cone renderer needs no per-source
branching.
"""

from __future__ import annotations

import difflib
import json
import os
import time
from pathlib import Path

SOURCE_ID = "claude_code"
SOURCE_LABEL = "Claude Code"

MAX_FILES_SCANNED = 400          # transcript files (sessions) scanned
MAX_EVENTS_PER_FILE = 2000       # tool-call events read per transcript
MAX_EVENTS_TOTAL = 4000          # whole-payload bound
MAX_DIFF_BYTES = 200_000         # skip difflib on pathological single edits
BUILD_DEADLINE_S = 20.0
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}


def _project_and_worktree(cwd: str) -> tuple[str, str | None]:
    """(project, worktree) from a session's cwd.

    A session working inside an in-repo worktree checkout has cwd like
    ".../<project>/.claude/worktrees/<name>[/...]" — naively taking the
    last path segment would report the WORKTREE name as the project,
    losing both the true owning project and the worktree distinction the
    Braided Streams view needs to braid worktree activity into its parent
    project's stream instead of showing it as an unrelated top-level one."""
    parts = [p for p in cwd.rstrip("/").split("/") if p]
    for i in range(len(parts) - 2):
        if parts[i + 1] == ".claude" and parts[i + 2] == "worktrees" and i + 3 < len(parts) + 1:
            worktree = parts[i + 3] if i + 3 < len(parts) else None
            return parts[i], worktree
    return (parts[-1] if parts else "", None)


def _line_diff(old: str, new: str) -> tuple[int, int]:
    """(insertions, deletions) via difflib, line-oriented like git."""
    if len(old) + len(new) > MAX_DIFF_BYTES:
        # Oversized single edit: approximate from line-count delta rather
        # than pay for a full diff — still a directionally honest cone.
        old_n, new_n = old.count("\n") + 1, new.count("\n") + 1
        return (max(0, new_n - old_n), max(0, old_n - new_n))
    sm = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    ins = dele = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            dele += i2 - i1
        if tag in ("replace", "insert"):
            ins += j2 - j1
    return ins, dele


def _events_from_transcript(path: Path) -> list[dict]:
    session_id = path.stem
    events: dict[tuple[str, str | None, str], dict] = {}  # (session_id, worktree, minute-bucket) -> event
    read = 0
    try:
        with open(path, encoding="utf-8", errors="ignore") as fp:
            for line in fp:
                if read >= MAX_EVENTS_PER_FILE:
                    break
                line = line.strip()
                if not line.startswith("{") or '"tool_use"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                ts = d.get("timestamp")
                cwd = d.get("cwd") or ""
                project, worktree = _project_and_worktree(cwd)
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name")
                    if name not in _EDIT_TOOLS:
                        continue
                    inp = block.get("input") or {}
                    file_path = str(inp.get("file_path") or "")
                    if not file_path:
                        continue
                    read += 1
                    ins = dele = 0
                    if name == "Write":
                        content_str = str(inp.get("content") or "")
                        ins = content_str.count("\n") + (1 if content_str else 0)
                    elif name == "Edit":
                        ins, dele = _line_diff(str(inp.get("old_string") or ""),
                                               str(inp.get("new_string") or ""))
                    elif name == "MultiEdit":
                        for e in (inp.get("edits") or [])[:20]:
                            if not isinstance(e, dict):
                                continue
                            i2, d2 = _line_diff(str(e.get("old_string") or ""),
                                               str(e.get("new_string") or ""))
                            ins += i2
                            dele += d2
                    if ins == 0 and dele == 0:
                        continue
                    # One event per (session, worktree, minute): Claude Code
                    # edits one file per tool call, and a turn often touches
                    # several files in quick succession — bucketing keeps the
                    # timeline at "one glyph per burst of activity" instead
                    # of one per single-file edit. Worktree is part of the key
                    # so a worktree checkout's activity never collapses into
                    # its parent project's main-checkout bucket.
                    bucket = (ts or "")[:16]  # YYYY-MM-DDTHH:MM
                    key = (session_id, worktree, bucket)
                    ev = events.get(key)
                    if ev is None:
                        ev = {
                            "id": f"claude_code:{session_id}:{worktree or 'main'}:{bucket}",
                            "kind": "session_diff",
                            "date": ts, "project": project, "project_label": project,
                            "worktree": worktree, "title": "", "author": "Claude Code",
                            "session_id": session_id, "sha": None,
                            "insertions": 0, "deletions": 0, "files": [], "files_count": 0,
                        }
                        events[key] = ev
                    ev["insertions"] += ins
                    ev["deletions"] += dele
                    fname = file_path.rsplit("/", 1)[-1]
                    if fname not in ev["files"] and len(ev["files"]) < 12:
                        ev["files"].append(fname)
                    ev["files_count"] += 1
    except OSError:
        return []
    out = list(events.values())
    for ev in out:
        n = ev["files_count"]
        ev["title"] = f"{n} file{'s' if n != 1 else ''} edited"
    return out


def collect_commit_diffs() -> dict:
    root = Path(os.getenv("CLAUDE_TRANSCRIPTS_DIR", "~/.claude/projects")).expanduser()
    events: list[dict] = []
    sessions_scanned = 0
    if root.is_dir():
        deadline = time.monotonic() + BUILD_DEADLINE_S
        files = sorted(root.rglob("*.jsonl"), key=lambda p: -p.stat().st_mtime
                      if p.exists() else 0)[:MAX_FILES_SCANNED]
        for f in files:
            if time.monotonic() > deadline or len(events) >= MAX_EVENTS_TOTAL:
                break
            events.extend(_events_from_transcript(f))
            sessions_scanned += 1
    events.sort(key=lambda e: e["date"] or "", reverse=True)
    truncated = len(events) > MAX_EVENTS_TOTAL
    if truncated:
        events = events[:MAX_EVENTS_TOTAL]
    return {
        "source": {"id": SOURCE_ID, "label": SOURCE_LABEL},
        "events": events,
        "sessions_scanned": sessions_scanned,
        "truncated": truncated,
    }
