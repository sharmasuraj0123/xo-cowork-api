"""POST change events to swarm's /signal/changes.

Auth reuses the existing cowork->swarm token resolution (get_auth_token),
imported lazily so tests need not load routers.auth.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


def _base_url() -> str:
    return os.getenv("CHAT_API_BASE_URL", "https://api-swarm-beta.xo.builders").rstrip("/")


async def report_change(project_id: str, commit_hash: str, token: str | None = None) -> bool:
    """POST the change to swarm. Returns True on a 2xx response, else False.

    `token` is resolved from get_auth_token() when not supplied (production
    path); tests pass it explicitly to avoid importing routers.auth.
    """
    if token is None:
        from routers.auth import get_auth_token
        token = get_auth_token()
    if not token:
        log.warning("commit_watcher: no swarm auth token; skipping report")
        return False

    url = f"{_base_url()}/signal/changes"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json={"project_id": project_id, "commit_hash": commit_hash},
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:
        log.warning("commit_watcher: report POST failed: %s", exc)
        return False

    if resp.status_code >= 400:
        log.warning("commit_watcher: swarm returned %s for %s", resp.status_code, project_id)
        return False
    return True
