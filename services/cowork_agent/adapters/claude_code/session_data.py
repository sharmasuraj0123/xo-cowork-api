"""Where the Claude Code runtime keeps session data, for the Overview tab.

Read-only capability: names the runtime's on-disk stores (transcripts,
in-repo worktrees) and cheap metadata about them. All bounds live here so
the aggregator can stay generic.
"""

from __future__ import annotations

import os
from pathlib import Path

from services.cowork_agent.project_layout import xo_projects_root

SOURCE_ID = "claude_code"
SOURCE_LABEL = "Claude Code"

_COUNT_CAP = 5000  # files counted per store before reporting "at least"


def _store_stats(base: Path, suffix: str | None = None) -> dict:
    files = 0
    size = 0
    newest = 0.0
    capped = False
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if suffix and not fn.endswith(suffix):
                continue
            try:
                st = os.stat(os.path.join(dirpath, fn))
            except OSError:
                continue
            files += 1
            size += st.st_size
            newest = max(newest, st.st_mtime)
            if files >= _COUNT_CAP:
                capped = True
                break
        if capped:
            break
    return {"files": files, "bytes": size, "newest_mtime": newest or None,
            "capped": capped}


def collect_session_data() -> dict:
    home = Path.home() / ".claude"
    transcripts = home / "projects"
    roots: list[dict] = []
    meta: dict = {}

    if transcripts.is_dir():
        roots.append({"label": "Transcripts", "path": str(transcripts), "depth": 2})
        stats = _store_stats(transcripts, ".jsonl")
        meta["transcripts"] = stats

    # In-repo worktrees: <project>/.claude/worktrees/<name>. Depth 1 on
    # purpose — a worktree is a full checkout (node_modules and all) and the
    # tree must not descend into it.
    worktree_count = 0
    try:
        for proj in sorted(xo_projects_root().iterdir()):
            if proj.name.startswith(".") or not proj.is_dir():
                continue
            wt = proj / ".claude" / "worktrees"
            if wt.is_dir():
                names = [d for d in wt.iterdir() if d.is_dir()]
                if names:
                    worktree_count += len(names)
                    roots.append({"label": f"Worktrees · {proj.name}",
                                  "path": str(wt), "depth": 1})
    except OSError:
        pass
    meta["worktrees"] = worktree_count

    # Telemetry DB (Argus) — size only; the Sessions dashboard reads content.
    db = Path(os.getenv("ARGUS_DB", "~/.argus/argus.db")).expanduser()
    if db.is_file():
        st = db.stat()
        meta["telemetry_db"] = {"path": str(db), "bytes": st.st_size,
                                "mtime": st.st_mtime}

    return {"source": {"id": SOURCE_ID, "label": SOURCE_LABEL},
            "roots": roots, "meta": meta}
