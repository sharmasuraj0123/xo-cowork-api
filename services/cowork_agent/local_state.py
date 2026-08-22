"""Canonical machine-local state roots for Quirq.

``~/.quirq/`` belongs to the local Quirq installation. It is separate
from portable project metadata under ``<project>/.xo/`` and from each
agent runtime's native storage.

``~/.xo-cowork/`` is retained only as a read-only migration source.
Callers must write new state beneath :func:`quirq_state_dir`.
"""

from __future__ import annotations

import os
from pathlib import Path


def quirq_state_dir() -> Path:
    """Return the canonical machine-local Quirq state directory."""
    configured = (os.getenv("QUIRQ_STATE_ROOT", "") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".quirq"


def legacy_state_dir() -> Path:
    """Return the former state root, used only for one-time migration."""
    return Path.home() / ".xo-cowork"
