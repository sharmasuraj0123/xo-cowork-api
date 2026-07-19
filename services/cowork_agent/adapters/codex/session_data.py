"""Where the Codex runtime keeps session data, for the Overview tab.

Read-only capability mirroring claude_code/session_data.py: rollout store
(year/month sharded) plus the state database, with cheap bounded stats.
"""

from __future__ import annotations

import os
from pathlib import Path

SOURCE_ID = "codex"
SOURCE_LABEL = "Codex"

_COUNT_CAP = 5000


def _codex_home() -> Path:
    return Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser()


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
    home = _codex_home()
    sessions = home / "sessions"
    roots: list[dict] = []
    meta: dict = {}

    if sessions.is_dir():
        roots.append({"label": "Rollouts", "path": str(sessions), "depth": 3})
        meta["rollouts"] = _store_stats(sessions, ".jsonl")

    dbs = sorted(home.glob("state_*.sqlite"),
                 key=lambda p: p.stat().st_mtime if p.exists() else 0)
    if dbs:
        st = dbs[-1].stat()
        meta["state_db"] = {"path": str(dbs[-1]), "bytes": st.st_size,
                            "mtime": st.st_mtime}

    return {"source": {"id": SOURCE_ID, "label": SOURCE_LABEL},
            "roots": roots, "meta": meta}
