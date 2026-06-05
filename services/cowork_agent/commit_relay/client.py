"""
commit_relay/client.py — push a commit hash event directly to a peer cowork-api.

Auth: each channel has a shared per-channel secret (peer_push_secret) exchanged
during pairing. The peer's /push endpoint validates it via constant-time compare.
"""

from __future__ import annotations

import httpx

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


async def push_commit_to_peer(
    peer_url: str,
    channel_id: str,
    push_secret: str,
    payload: dict,
) -> dict:
    """POST {peer_url}/api/relay/channels/{channel_id}/push"""
    url = f"{peer_url.rstrip('/')}/api/relay/channels/{channel_id}/push"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {push_secret}"},
        )
        r.raise_for_status()
        return r.json()
