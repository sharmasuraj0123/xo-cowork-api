"""
Workspace status SSE feed.

    GET /api/status/stream   ->   event: status  (full snapshot, every ~5s)

Replaces frontend polling of ``/providers/status`` and the per-connector
``/api/connectors/*/status`` endpoints. Those endpoints are unchanged and still
work — this is additive, and ``models`` is sourced from the same
``providers_status`` capability, so the two can never disagree.

Every message is a **complete** snapshot, so the client assigns rather than
merges:

    event: status
    data: {"ts":"2026-07-30T11:04:22Z",
           "models":{"claude_code":{"connected":true},"codex":{"connected":false}},
           "data":{"github":{"status":"connected"},"vercel":{"status":"needs_auth"},
                   "gdrive":{"status":"connected"},"onedrive":{"status":"needs_auth"}}}

Consequences worth knowing on the client side:

* Repeating an unchanged snapshot every tick means the payload doubles as the
  keepalive, so there is no separate ``heartbeat`` event to handle (unlike
  ``/api/chat/stream/{id}``, which can be silent for minutes and needs one).
* A category is always present, but a key inside it may be **absent** when that
  source has never been probed successfully. Absent means "not known yet", not
  "disconnected" — render it as unknown, not as an error.

The handler holds no state and does no probing: it subscribes to the shared
collector, which is what keeps cost flat as viewers multiply.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from services.cowork_agent.status_stream import get_collector

log = logging.getLogger(__name__)

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Without this, nginx buffers the response and the client sees nothing until
    # the buffer fills — which looks exactly like a broken endpoint.
    "X-Accel-Buffering": "no",
}


def _frame(payload: dict) -> str:
    return f"event: status\ndata: {json.dumps(payload)}\n\n"


@router.get("/api/status/stream")
async def status_stream(request: Request) -> StreamingResponse:
    """Stream the workspace status snapshot until the client disconnects."""
    collector = get_collector()

    async def generator():
        queue = collector.subscribe()
        try:
            # Paint immediately from cache rather than making a new tab wait a
            # full tick for its first frame.
            yield _frame(collector.snapshot())

            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(
                        queue.get(), timeout=collector.push_interval
                    )
                except asyncio.TimeoutError:
                    # No broadcast arrived (e.g. the collector loop is not
                    # running). Send the cached snapshot anyway so the stream
                    # stays alive and proxies do not time it out.
                    payload = collector.snapshot()
                yield _frame(payload)
        except asyncio.CancelledError:
            raise
        finally:
            # Non-negotiable: a subscriber left behind on every disconnect is
            # an unbounded leak, and browsers disconnect constantly.
            collector.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
