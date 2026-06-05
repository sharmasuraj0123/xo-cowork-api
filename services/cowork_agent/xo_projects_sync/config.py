"""
Runtime + .env config for xo-projects-sync.

Two responsibilities:

1. Read the live runtime config (`BACKUP_PASSWORD`, `GITHUB_PAT`) from
   `os.environ`. These are populated by dotenv at process start and may
   be live-updated by `upsert_env`.
2. Write new values to the project's `.env` file with surgical
   line-level upsert so existing lines, formatting, and comments are
   preserved. Also reflect writes into `os.environ` so the current
   process picks up new values without a restart.

The `.env` file lives at the repo root next to `server.py`. We refuse
to write paths that don't resolve under that root — defense in depth
against being tricked into editing arbitrary files.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_ENV_PATH = _REPO_ROOT / ".env"

# Env var names — single source of truth so the router and skill docs
# don't drift from the storage code.
ENV_PASSPHRASE = "BACKUP_PASSWORD"
ENV_GITHUB_PAT = "GITHUB_PAT"

# Per-project backup repos are named `<BACKUP_REPO_PREFIX><project_id>`
# on the user's GitHub account. The prefix is what lets us discover
# our repos via `GET /user/repos` without colliding with the user's
# unrelated repositories.
BACKUP_REPO_PREFIX = "xo-project-"

# How many timestamped snapshots to keep per project before pruning.
MAX_VERSIONS_PER_PROJECT = 10

# Chunk size for splitting encrypted blobs. GitHub's hard limit is 100 MB
# per file; we go a touch under so a manifest + part fits comfortably.
CHUNK_SIZE_BYTES = 95 * 1024 * 1024


@dataclass(frozen=True)
class SyncConfig:
    """Snapshot of the runtime config read fresh from os.environ.

    `configured` is true iff the passphrase is present. The repo name is
    derived per-project (see `repo_name_for`); there is no shared repo
    name to persist anymore. Token source ("connector" / "env" /
    "missing") is the responsibility of the github module — kept out of
    this snapshot so SyncConfig stays purely about persisted settings.
    """

    passphrase: str | None

    @property
    def configured(self) -> bool:
        return bool(self.passphrase)


def load_config() -> SyncConfig:
    """Read the current config from process env. Re-callable; no caching."""
    return SyncConfig(
        passphrase=(os.environ.get(ENV_PASSPHRASE) or "").strip() or None,
    )


def repo_name_for(project_id: str) -> str:
    """GitHub repo name for a given xo-project id."""
    return f"{BACKUP_REPO_PREFIX}{project_id}"


# Matches a KEY=... assignment at start of line (ignoring leading whitespace).
# Captures KEY in group 1. Quote handling is intentionally minimal — we
# only need to recognize the line, not parse the value.
_ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def upsert_env(updates: dict[str, str], *, env_path: Path | None = None) -> Path:
    """Add or replace `KEY=value` lines in the .env file.

    - Replaces the value in place if KEY already appears on a line.
    - Appends `KEY=value` at the end if not.
    - Preserves all other lines verbatim (comments, blanks, ordering).
    - Mirrors writes into `os.environ` so the current process sees them
      without a restart.

    Values are emitted as `KEY=value` without quoting. Callers should
    pre-strip leading/trailing whitespace from values. We don't quote
    automatically because dotenv treats unquoted values as plain strings
    and most of our values are simple repo names / hex secrets.
    """
    target = (env_path or _DEFAULT_ENV_PATH).resolve()
    if not str(target).startswith(str(_REPO_ROOT.resolve())):
        raise ValueError(
            f".env upsert refused: {target} is outside the repo root {_REPO_ROOT}"
        )

    # Reject newlines in values — they'd break the line-oriented file.
    for k, v in updates.items():
        if "\n" in v or "\r" in v:
            raise ValueError(f"env value for {k} contains newline; refusing to write")

    existing_lines: list[str] = []
    if target.exists():
        existing_lines = target.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)  # keys still to write
    out_lines: list[str] = []
    for line in existing_lines:
        m = _ASSIGNMENT_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out_lines.append(f"{key}={remaining.pop(key)}")
        else:
            out_lines.append(line)

    # Append anything not seen.
    if remaining:
        if out_lines and out_lines[-1] != "":
            out_lines.append("")  # blank separator before our new block
        for key, value in remaining.items():
            out_lines.append(f"{key}={value}")

    target.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    # Reflect into the running process. Done AFTER the file write so
    # there's no window where memory says yes but disk says no.
    for k, v in updates.items():
        os.environ[k] = v

    return target
