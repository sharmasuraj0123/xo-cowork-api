"""Relay → behind/ahead detector — **interface seam, not yet wired**.

This is the reserved seam for the commit-hash relay (doc 03 §2 of
xo-project-optimal/docs). The relay itself lives outside this repo and transfers
**only a commit hash** between machines; it drops that hash into a machine-local
file and the watcher derives the actionable ``behind/ahead`` state from it.

Contract (both files are runtime tier — ``~/.xo/<pid>/``, never synced):

    remote-head.json   ← WRITTEN BY THE EXTERNAL RELAY
    {
      "schema": 1,
      "branch": "main",
      "remote_head": "9f1c2a…",            # the hash a peer just pushed
      "remote_head_from": "tools@kosh.network",
      "received_at": "2026-06-18T11:48:20Z"
    }

    sync.json          ← WRITTEN BY detect() BELOW (what the UI/agent read)
    {
      "schema": 2,
      "local_head": "a31bb0…",
      "remote_head": "9f1c2a…",
      "behind": 3,            # commits on remote not in local
      "ahead": 1,            # local commits not pushed
      "diverged": true,      # behind>0 AND ahead>0 → rebase/merge needed
      "dirty": false,        # uncommitted local changes present
      "checked_at": "2026-06-18T11:48:21Z"
    }

**Status:** intentionally a no-op this iteration. The runtime layout already
reserves both filenames (``project_layout.runtime_dir(pid)``); when the relay is
adopted, implement ``detect()`` to:

  1. read ``runtime_dir(pid)/remote-head.json`` (skip if absent),
  2. resolve the project's local checkout path (registry → ``local_path``),
  3. shell ``git rev-parse HEAD`` and ``git rev-list --count`` in that path to
     compute behind/ahead/diverged/dirty against ``remote_head``,
  4. write ``runtime_dir(pid)/sync.json`` atomically, and
  5. emit a ``repo.behind`` / ``peer.commit`` timeline event.

Then call it from the watcher's per-project loop (after the runtime root is
resolved). The watcher only ever **detects** — it never commits or pushes; an
external git/relay layer owns writing the shared history.

This module names no agent.
"""

from __future__ import annotations

REMOTE_HEAD_FILE = "remote-head.json"
SYNC_FILE = "sync.json"


def detect(pid: str) -> None:  # pragma: no cover - seam, not yet implemented
    """Derive ``sync.json`` from the relay's ``remote-head.json``.

    Not implemented this iteration (see module docstring). Deliberately a
    no-op so the seam can be wired without a behavior change today.
    """
    return None
