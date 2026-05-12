"""Map between Claude Code's encoded-cwd jsonl directories and our
project ids.

Claude Code writes each session log to
``~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`` where
``<encoded-cwd>`` is the absolute working directory with ``/``
replaced by ``-`` (the leading slash becomes an empty segment, so
the result starts with ``-``).

The encoding is **lossy** when a directory name contains a hyphen —
``xo-projects/blackhole`` encodes to ``-home-…-xo-projects-blackhole``
which a naive decode would split as ``xo/projects/blackhole``.

This module avoids the lossy reverse by:

* :func:`project_id_for_encoded_cwd` — *longest-match against actual
  project directories under* ``xo_projects_root()``. Resolves the
  ambiguity in the common case but is still a best-effort discovery
  helper.

* :func:`project_id_for_cwd` — authoritative lookup using a literal
  ``cwd`` string from an event payload. Use this whenever the source
  has read an event; the encoded directory is only used for
  discovery / listing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from services.cowork_agent.project_layout import xo_projects_root


def _encode_path(abs_path: str) -> str:
    """Forward direction: ``/foo/bar`` → ``-foo-bar``. Lossless.

    Used for forward comparisons; we never decode an encoded string
    blindly because the reverse is lossy (see module docstring).
    """
    return abs_path.replace("/", "-")


def project_id_for_cwd(cwd: str) -> Optional[str]:
    """Authoritative lookup. ``cwd`` is the literal working directory
    a Claude session reports in its event payload (each event has a
    ``cwd`` field). Returns the top-level project id under
    ``xo_projects_root()``, or ``None`` if the cwd is outside the
    workspace.
    """
    if not cwd:
        return None
    p = Path(cwd)
    try:
        p = p.resolve()
    except OSError:
        return None
    root = xo_projects_root()
    if not p.is_relative_to(root):
        return None
    rel = p.relative_to(root)
    if not rel.parts:
        return None
    return rel.parts[0]


def project_id_for_encoded_cwd(encoded: str) -> Optional[str]:
    """Best-effort: encoded dir name → project id, by **longest match
    against existing project directories**.

    Strategy:

    1. Verify the encoding starts with the encoded form of
       ``xo_projects_root()`` followed by ``-`` (otherwise the session
       isn't inside the workspace).
    2. Strip that prefix; the remainder is ``<project_id>`` optionally
       followed by ``-<subdir-encoded>``.
    3. Match the remainder against each existing top-level project
       directory, preferring the **longest** match. Longest-match
       handles the case where projects ``foo`` and ``foo-bar`` both
       exist and the session is in ``foo-bar``.

    Returns ``None`` if no project matches (unknown session, or
    project deleted between encoding and lookup).
    """
    if not encoded or not encoded.startswith("-"):
        return None
    root = xo_projects_root()
    root_encoded = _encode_path(str(root))  # e.g. ``-home-coder-xo-projects``
    prefix = root_encoded + "-"
    if not encoded.startswith(prefix):
        return None
    remainder = encoded[len(prefix):]
    if not remainder:
        return None

    # Sort actual children by name length, longest first, to handle
    # the ``foo`` vs ``foo-bar`` ambiguity correctly.
    try:
        candidates = sorted(
            (c.name for c in root.iterdir()
             if c.is_dir() and not c.name.startswith(".")),
            key=len,
            reverse=True,
        )
    except OSError:
        return None

    for name in candidates:
        if remainder == name or remainder.startswith(name + "-"):
            return name
    return None


def project_id_for_jsonl(jsonl_path: Path) -> Optional[str]:
    """Discovery helper for a ``~/.claude/projects/<enc>/<sid>.jsonl``
    path. Returns the project id, or ``None`` if the jsonl isn't
    rooted in our workspace.

    The watcher uses this once on first sight; subsequent events
    inside the jsonl confirm via the authoritative
    :func:`project_id_for_cwd` (each event carries ``cwd``).
    """
    if jsonl_path.suffix != ".jsonl":
        return None
    parent = jsonl_path.parent
    if not parent.name:
        return None
    return project_id_for_encoded_cwd(parent.name)


def encoded_cwd_for_project(project_id: str) -> str:
    """Round-trip helper: project id → ``<encoded-cwd>`` directory
    name under ``~/.claude/projects/``. Lossless (forward direction).
    """
    return _encode_path(str((xo_projects_root() / project_id).resolve()))
