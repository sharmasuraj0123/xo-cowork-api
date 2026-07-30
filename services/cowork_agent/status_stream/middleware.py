"""
Invalidate a status source when a request changes what it reports.

A status change the user *causes* arrives through this API, so there is no
reason to discover it by polling. After a successful non-GET request, any
source whose ``owns`` prefix matches the path is marked stale and re-probed
immediately — which is what lets the background intervals stay slow (60s for
HTTPS probes) without the UI feeling slow.

Why the source declares the prefix instead of each route calling
``invalidate()``: the connector and auth routes stay completely unaware that a
status stream exists. Adding a connector means registering a source, not
remembering to add an invalidation call in four handlers — and a forgotten call
is a silent bug (status just lags by a minute, with nothing to trace).

**This is a raw ASGI middleware, not a ``BaseHTTPMiddleware`` subclass, and
that is deliberate.** ``BaseHTTPMiddleware`` wraps the response in an
anyio task and is well known to interfere with long-lived streaming responses.
This app already streams SSE from ``/api/chat/stream/{id}`` and now from
``/api/status/stream``, so installing a ``BaseHTTPMiddleware`` app-wide risks
buffering or breaking chat streaming — a serious regression far outside the
scope of this feature. A raw ASGI wrapper only observes the response-start
message and never touches the body, so streaming is unaffected.

Only 2xx/3xx responses invalidate: a rejected or failed connect attempt did not
change anything, and re-probing on it would spend an HTTPS call to learn that.
"""

from __future__ import annotations

import logging

from .collector import invalidate
from .registry import sources_for_path

log = logging.getLogger(__name__)

#: Methods that cannot change connection state.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class StatusInvalidationMiddleware:
    """Re-probe the owning status source after a successful mutating request."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("method") in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        targets = sources_for_path(scope.get("path") or "")
        if not targets:
            await self.app(scope, receive, send)
            return

        status_code: int | None = None

        async def _send(message) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = message.get("status")
            await send(message)

        await self.app(scope, receive, _send)

        if status_code is not None and 200 <= status_code < 400:
            for source in targets:
                log.debug("invalidating status source %r after %s", source.name, scope.get("path"))
                invalidate(source.name)


__all__ = ["StatusInvalidationMiddleware"]
