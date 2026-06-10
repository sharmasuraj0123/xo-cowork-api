"""Report detected commits to swarm's POST /commits. Auth via get_auth_token (lazy
import so tests don't load routers.auth). Never raises; a relay outage must not break
the watcher loop."""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


def _base_url() -> str:
    return os.getenv("CHAT_API_BASE_URL", "https://api-swarm-beta.xo.builders").rstrip("/")


async def report_commits(project_id: str, hashes: list[str], token: str | None = None) -> bool:
    """POST {project_id, commits} to swarm. True on 2xx (and on empty input)."""
    if not hashes:
        return True
    if token is None:
        from routers.auth import get_auth_token
        token = get_auth_token()
    if not token:
        log.warning("commit_relay: no swarm auth token; skipping report")
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_base_url()}/commits",
                json={"project_id": project_id, "commits": hashes},
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:
        log.warning("commit_relay: report failed: %s", exc)
        return False
    if resp.status_code >= 400:
        log.warning("commit_relay: swarm returned %s", resp.status_code)
        return False
    return True
