"""
xo-cowork machine-local UI/installation state.

Stored at ~/.xo-cowork/state.json (separate from ~/.openclaw/, which is
openclaw's own data). This is for state that:

- belongs to xo-cowork (the product), not to openclaw
- needs to persist across browsers/incognito/devtools-clear, so cannot
  live in localStorage
- is per-machine, not per-tenant (single-tenant assumption — revisit if
  xo-cowork-api ever serves multiple users from one process)

Schema is a flat dict; callers patch it via `update_state`. First fields:

    {
      "onboarding_completed": bool,
      "onboarding_completed_at": "<iso-8601>"
    }
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".xo-cowork"
STATE_FILE = STATE_DIR / "state.json"


def _read() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_write(payload: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_state() -> dict[str, Any]:
    return _read()


def update_state(patch: dict[str, Any]) -> dict[str, Any]:
    current = _read()
    current.update(patch)
    _atomic_write(current)
    return current
