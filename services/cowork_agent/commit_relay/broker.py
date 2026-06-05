"""
commit_relay/broker.py — in-memory fan-out broker for incoming commit hash events.

Messages are ephemeral: dropped if no SSE subscriber is connected.
Module-level singleton `broker` is shared by the push endpoint and SSE endpoint.
"""

from __future__ import annotations

import asyncio


class RelayBroker:
    """Fan-out broker. Each channel has a set of asyncio.Queue (one per SSE client)."""

    def __init__(self) -> None:
        self._channels: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, channel_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._channels.setdefault(channel_id, set()).add(q)
        return q

    def unsubscribe(self, channel_id: str, q: asyncio.Queue) -> None:
        subs = self._channels.get(channel_id)
        if subs:
            subs.discard(q)

    def publish(self, channel_id: str, payload: dict) -> int:
        """Deliver payload to all live subscribers. Returns count delivered."""
        subs = self._channels.get(channel_id, set())
        for q in list(subs):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass
        return len(subs)


broker = RelayBroker()
