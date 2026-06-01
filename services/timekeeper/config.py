"""Timekeeper config — watch root, output dir, retention, filter lists.

Override via env vars; no YAML loader yet.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# What to watch. Recursive from this directory down.
WATCH_ROOT = Path(os.environ.get("TIMEKEEPER_WATCH_ROOT") or os.path.expanduser("~"))

# Where to put the JSONL.
OUTPUT_DIR = Path(os.environ.get("TIMEKEEPER_OUTPUT_DIR") or (REPO_ROOT / "timekeeper"))

RETENTION_DAYS = int(os.environ.get("TIMEKEEPER_RETENTION_DAYS", "14"))

FLUSH_INTERVAL_S = 0.2
FLUSH_BATCH_LINES = 100
QUEUE_MAX = 10_000

# Paths to drop. Matched as prefix against the absolute event path.
IGNORE_PATH_PREFIXES: tuple[str, ...] = (
    "/proc/", "/sys/", "/dev/", "/run/",
    "/var/log/", "/var/cache/", "/var/lib/dpkg/", "/var/lib/apt/",
    "/tmp/", "/var/tmp/",
    str(OUTPUT_DIR) + "/",  # never log our own writes
)

# Substrings that, if anywhere in the path, drop the event AND prune the
# directory from the walk at startup (so we never spend a watch on them).
IGNORE_PATH_SUBSTRINGS: tuple[str, ...] = (
    "/.cache/", "/.mozilla/", "/.config/google-chrome/", "/.config/chromium/",
    "/__pycache__/", "/node_modules/", "/.git/",
    "/.venv/", "/venv/", "/.mypy_cache/", "/.pytest_cache/",
    "/.xo/",
)

# Directory basenames that prune the walk wherever they appear.
PRUNE_DIRS: frozenset[str] = frozenset({
    ".cache", ".mozilla", ".config",  # browser caches live under .config too — prune wholesale
    "__pycache__", "node_modules", ".git",
    ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".xo",
})
