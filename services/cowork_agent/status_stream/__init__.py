"""
Workspace status streaming: the machinery behind `GET /api/status/stream`.

The frontend used to learn connection state by polling ``/providers/status``,
``/channels/status`` and each ``/api/connectors/*/status`` endpoint. This
package pushes it instead, so a screen reflects the workspace without asking.

Three pieces, each with one job:

* ``registry`` — what can be streamed. Register a :class:`StatusSource` and it
  appears in the payload; nothing else needs editing.
* ``collector`` — one shared background task that probes sources on their own
  cadences and fans full snapshots out to subscribers. Probe cost is O(1) in
  connected clients.
* ``middleware`` — turns a successful mutating request into an immediate
  re-probe of the affected source, so background intervals can stay slow
  without the UI lagging behind the user's own actions.

``sources`` holds the v1 registrations (``models`` + ``data``).

The polling endpoints are untouched and still work; this is purely additive.
"""

from .collector import (
    DEFAULT_PROBE_TIMEOUT,
    DEFAULT_PUSH_INTERVAL,
    StatusCollector,
    get_collector,
    invalidate,
    reset_collector,
    start_collector,
    stop_collector,
)
from .middleware import StatusInvalidationMiddleware
from .registry import (
    StatusSource,
    categories,
    iter_sources,
    register,
    sources_for_path,
)

__all__ = [
    "DEFAULT_PROBE_TIMEOUT",
    "DEFAULT_PUSH_INTERVAL",
    "StatusCollector",
    "StatusInvalidationMiddleware",
    "StatusSource",
    "categories",
    "get_collector",
    "invalidate",
    "iter_sources",
    "register",
    "reset_collector",
    "sources_for_path",
    "start_collector",
    "stop_collector",
]
