"""
commit_relay/scanner.py — background task that detects new git commits in
watched repos and pushes commit hash events directly to the paired peer cowork-api.

Commit detection uses raw .git file reads (no GitPython dependency):
  .git/HEAD → ref: refs/heads/main  (or a bare SHA for detached HEAD)
  .git/refs/heads/<branch>          → 40-char SHA
  .git/packed-refs                  → fallback when ref is packed

Git metadata (branch, author, message) is read via a single
asyncio.create_subprocess_exec call to `git log -1`.
"""

from __future__ import annotations

import asyncio
import datetime
import os
from pathlib import Path

from services.cowork_agent.commit_relay import config as relay_config
from services.cowork_agent.commit_relay import client as relay_client

POLL_INTERVAL_S = 5.0


def _read_head_commit(repo_path: str) -> str | None:
    """Return the current HEAD commit hash, or None if not a git repo."""
    git_dir = Path(repo_path) / ".git"
    head_file = git_dir / "HEAD"
    if not head_file.is_file():
        return None
    try:
        head = head_file.read_text(encoding="utf-8").strip()
    except Exception:
        return None

    if head.startswith("ref: "):
        ref = head[5:]  # e.g. refs/heads/main
        ref_file = git_dir / ref
        if ref_file.is_file():
            try:
                return ref_file.read_text(encoding="utf-8").strip() or None
            except Exception:
                pass
        # Fallback: scan packed-refs
        packed = git_dir / "packed-refs"
        if packed.is_file():
            try:
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0]
            except Exception:
                pass
        return None

    # Detached HEAD — the content is the hash itself
    if len(head) == 40 and all(c in "0123456789abcdefABCDEF" for c in head):
        return head
    return None


async def _read_git_metadata(repo_path: str, commit_hash: str) -> dict:
    """Run `git log -1` to get branch, author, message for a commit."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "log", "-1",
            "--format=%H%n%D%n%an%n%ae%n%s",
            commit_hash,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        lines = stdout.decode("utf-8", errors="replace").strip().splitlines()
    except Exception:
        lines = []

    branch = ""
    if len(lines) > 1 and lines[1]:
        for part in lines[1].split(","):
            p = part.strip()
            if p.startswith("HEAD -> "):
                branch = p[8:].strip()
                break
        if not branch:
            for part in lines[1].split(","):
                p = part.strip()
                if p and not p.startswith("HEAD"):
                    branch = p.split("/")[-1]
                    break

    return {
        "commit_hash": lines[0] if lines else commit_hash,
        "branch": branch or "unknown",
        "author_name": lines[2] if len(lines) > 2 else None,
        "author_email": lines[3] if len(lines) > 3 else None,
        "commit_message": lines[4] if len(lines) > 4 else None,
        "committed_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


class CommitScanner:
    """Polls watched git repos every POLL_INTERVAL_S, pushes new commits to peer."""

    def __init__(self) -> None:
        self._last_seen: dict[str, str] = {}  # repo_path → commit_hash

    def _scan_sync(self) -> list[tuple[str, str, str]]:
        """Synchronous scan. Returns list of (channel_id, repo_path, new_hash)."""
        changed = []
        entries = relay_config.load_relay_config()
        for entry in entries:
            channel_id = entry.get("channel_id", "")
            # Skip channels with no peer URL configured yet
            if not entry.get("peer_cowork_url"):
                continue
            watched = entry.get("watched_repos", [])
            for repo_path in watched:
                try:
                    current = _read_head_commit(repo_path)
                except Exception:
                    current = None
                if not current:
                    continue
                prev = self._last_seen.get(repo_path)
                if prev != current:
                    changed.append((channel_id, repo_path, current))
        return changed

    async def _push(self, channel_id: str, repo_path: str, commit_hash: str) -> None:
        workspace_id = os.getenv("CODER_WORKSPACE_ID", "unknown")
        peer_url = relay_config.get_peer_url(channel_id)
        peer_push_secret = relay_config.get_peer_push_secret(channel_id)

        if not peer_url or not peer_push_secret:
            print(f"[commit_scanner] no peer URL/secret for channel {channel_id[:8]}, skipping")
            return

        try:
            entry = relay_config.get_channel(channel_id) or {}
            project_id = entry.get("project_id", "")
            meta = await _read_git_metadata(repo_path, commit_hash)
            payload = {
                "sender_workspace_id": workspace_id,
                "project_id": project_id,
                "repo_path": repo_path,
                **meta,
            }
            await relay_client.push_commit_to_peer(peer_url, channel_id, peer_push_secret, payload)
            self._last_seen[repo_path] = commit_hash
            short = commit_hash[:7]
            print(f"[commit_scanner] pushed {short} ({repo_path}) → {peer_url[:40]}")
        except Exception as exc:
            print(f"[commit_scanner] push failed for {repo_path}: {exc}")

    async def run(self) -> None:
        while True:
            try:
                changed = await asyncio.to_thread(self._scan_sync)
                for channel_id, repo_path, commit_hash in changed:
                    await self._push(channel_id, repo_path, commit_hash)
            except Exception as exc:
                print(f"[commit_scanner] tick error (non-fatal): {exc}")
            await asyncio.sleep(POLL_INTERVAL_S)


async def start_commit_scanner() -> None:
    """Entry point called from FastAPI lifespan."""
    await asyncio.sleep(3)  # brief startup delay
    scanner = CommitScanner()
    await scanner.run()
