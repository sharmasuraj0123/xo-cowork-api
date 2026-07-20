"""Code diffs derived from Codex rollouts, for the Sessions-space Timeline.

Unlike Claude Code (a dedicated Edit/Write tool with before/after content
right in the transcript), Codex's file edits go through a generic shell-exec
tool running the `apply_patch` CLI convention — a "*** Begin Patch ... ***
End Patch" block embedded in the shell command text, not a structured field.
This is a best-effort parser for that convention: it degrades to zero events
cleanly (not an error) for any session where no such block appears, which in
practice is most sessions in an environment whose Codex usage runs through
custom multi-agent orchestration tools (wait/spawn_agent/send_message) rather
than direct file edits. See claude_code/commit_diffs.py for the reliable
sibling implementation.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

SOURCE_ID = "codex"
SOURCE_LABEL = "Codex"

MAX_FILES_SCANNED = 400
MAX_EVENTS_PER_FILE = 2000
MAX_EVENTS_TOTAL = 4000
BUILD_DEADLINE_S = 20.0
_SHELL_TOOLS = {"exec_command", "shell", "local_shell_call"}

_PATCH_BLOCK_RE = re.compile(r"\*\*\* Begin Patch(.*?)\*\*\* End Patch", re.S)
_FILE_HEADER_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)


def _codex_home() -> Path:
    return Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser()


def _diff_from_patch_text(text: str) -> list[dict]:
    """[(file, insertions, deletions)] for every "*** Update/Add/Delete File"
    section in one apply_patch block. Lines starting with a single + or -
    (not +++/---, which this format doesn't use) are counted directly."""
    out = []
    headers = list(_FILE_HEADER_RE.finditer(text))
    for i, h in enumerate(headers):
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        section = text[start:end]
        ins = sum(1 for ln in section.splitlines()
                  if ln.startswith("+") and not ln.startswith("+++"))
        dele = sum(1 for ln in section.splitlines()
                   if ln.startswith("-") and not ln.startswith("---"))
        if ins or dele:
            out.append({"file": h.group(1).strip(), "insertions": ins, "deletions": dele})
    return out


def _extract_command_text(args: str) -> str:
    """exec_command's `arguments` is a JSON string; the shell command itself
    may be a single string or an argv array under a few possible keys."""
    try:
        parsed = json.loads(args)
    except (ValueError, TypeError):
        return args
    if not isinstance(parsed, dict):
        return args
    for key in ("command", "cmd", "input", "script"):
        v = parsed.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return " ".join(str(x) for x in v)
    return json.dumps(parsed)


def _events_from_rollout(path: Path) -> list[dict]:
    session_id = path.stem
    events: dict[str, dict] = {}
    read = 0
    try:
        with open(path, encoding="utf-8", errors="ignore") as fp:
            for line in fp:
                if read >= MAX_EVENTS_PER_FILE:
                    break
                if "Begin Patch" not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "response_item":
                    continue
                p = d.get("payload") or {}
                if p.get("type") not in ("function_call", "local_shell_call"):
                    continue
                text = _extract_command_text(str(p.get("arguments") or p.get("command") or ""))
                for m in _PATCH_BLOCK_RE.finditer(text):
                    diffs = _diff_from_patch_text(m.group(1))
                    if not diffs:
                        continue
                    read += 1
                    bucket_key = f"{session_id}:{p.get('id') or read}"
                    ev = events.setdefault(bucket_key, {
                        "id": f"codex:{session_id}:{p.get('id') or read}",
                        "kind": "session_diff",
                        "date": None, "project": "", "project_label": "",
                        "worktree": None, "title": "", "author": "Codex",
                        "session_id": session_id, "sha": None,
                        "insertions": 0, "deletions": 0, "files": [], "files_count": 0,
                    })
                    for fd in diffs:
                        ev["insertions"] += fd["insertions"]
                        ev["deletions"] += fd["deletions"]
                        fname = fd["file"].rsplit("/", 1)[-1]
                        if fname not in ev["files"] and len(ev["files"]) < 12:
                            ev["files"].append(fname)
                        ev["files_count"] += 1
    except OSError:
        return []
    out = []
    for ev in events.values():
        n = ev["files_count"]
        ev["title"] = f"{n} file{'s' if n != 1 else ''} edited"
        out.append(ev)
    return out


def _session_timestamp(path: Path) -> str | None:
    """First session_meta line carries the ISO start time; used to stamp
    events, since apply_patch blocks have no timestamp of their own."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fp:
            for i, line in enumerate(fp):
                if i > 5:
                    break
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "session_meta":
                    return (d.get("payload") or {}).get("timestamp")
    except OSError:
        pass
    return None


def collect_commit_diffs() -> dict:
    home = _codex_home()
    sessions_dir = home / "sessions"
    events: list[dict] = []
    sessions_scanned = 0
    if sessions_dir.is_dir():
        deadline = time.monotonic() + BUILD_DEADLINE_S
        files = sorted(sessions_dir.rglob("*.jsonl"),
                       key=lambda p: -p.stat().st_mtime if p.exists() else 0)[:MAX_FILES_SCANNED]
        for f in files:
            if time.monotonic() > deadline or len(events) >= MAX_EVENTS_TOTAL:
                break
            sessions_scanned += 1
            found = _events_from_rollout(f)
            if not found:
                continue
            ts = _session_timestamp(f)
            for ev in found:
                ev["date"] = ts
            events.extend(found)
    events = [e for e in events if e["date"]]
    events.sort(key=lambda e: e["date"], reverse=True)
    truncated = len(events) > MAX_EVENTS_TOTAL
    if truncated:
        events = events[:MAX_EVENTS_TOTAL]
    return {
        "source": {"id": SOURCE_ID, "label": SOURCE_LABEL},
        "events": events,
        "sessions_scanned": sessions_scanned,
        "truncated": truncated,
    }
