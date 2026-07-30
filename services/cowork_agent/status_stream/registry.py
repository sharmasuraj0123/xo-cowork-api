"""
Registry of workspace-status sources for the `/api/status/stream` SSE feed.

A :class:`StatusSource` is one independently-refreshable contributor to the
streamed snapshot. Adding a new thing to the stream means registering one
source — the collector and the SSE route never enumerate categories or names,
so neither has to change. This mirrors how ``adapters/`` are auto-discovered
rather than listed in a dict: the extension point is registration, not an
edit to core code.

**A probe returns a mapping, not a single value.** ``probe()`` resolves to
``{key: payload}`` and every key is merged into the source's ``category``.
That one rule covers both shapes we need:

    github    -> {"github": {"status": "connected"}}          # 1 key
    providers -> {"claude_code": {"connected": True},         # N keys
                  "codex": {"connected": False}}

so a source backed by one upstream call that reports on many things does not
need a special case.

``owns`` is the URL prefix whose mutating requests imply this source is now
stale. The ASGI middleware in ``middleware.py`` matches request paths against
it and invalidates the source, which is what makes a user-driven change (a
connect, a disconnect) show up immediately instead of on the next slow tick.
Declaring it here keeps the knowledge in one place — the connector routes stay
unaware that a status stream exists at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

#: A probe resolves to ``{key: payload}``, merged into the source's category.
ProbeFn = Callable[[], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class StatusSource:
    """One refreshable contributor to the streamed status snapshot.

    ``category`` is the top-level payload key (``"models"``, ``"data"``).
    ``name`` is the source's own id — used for invalidation, scheduling and
    logging. It is *not* necessarily a key in the payload; the keys come from
    whatever ``probe()`` returns.

    ``interval`` is the minimum seconds between background probes. Set it from
    what the probe actually costs: free (env var / file stat) sources can run
    every tick, subprocess and network sources should not.
    """

    category: str
    name: str
    interval: float
    probe: ProbeFn
    owns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Accept a bare string for the common single-prefix case without making
        # every call site remember a trailing comma.
        if isinstance(self.owns, str):
            object.__setattr__(self, "owns", (self.owns,))
        else:
            object.__setattr__(self, "owns", tuple(self.owns or ()))


_SOURCES: dict[str, StatusSource] = {}


def register(source: StatusSource, *, replace: bool = False) -> None:
    """Add ``source`` to the registry.

    Raises ``ValueError`` on a duplicate ``name`` unless ``replace`` is set.
    A duplicate is almost always a copy-paste bug, and silently overwriting it
    would make one source invisible with no error to trace, so it is loud by
    default.
    """
    if not source.category or not source.name:
        raise ValueError("StatusSource needs a non-empty category and name")
    if source.interval <= 0:
        raise ValueError(f"StatusSource {source.name!r}: interval must be > 0")
    if not replace and source.name in _SOURCES:
        raise ValueError(f"status source {source.name!r} is already registered")
    _SOURCES[source.name] = source


def iter_sources() -> tuple[StatusSource, ...]:
    """Every registered source, in registration order."""
    return tuple(_SOURCES.values())


def get(name: str) -> StatusSource | None:
    return _SOURCES.get(name)


def categories() -> tuple[str, ...]:
    """Distinct categories, in first-registration order.

    The collector uses this so every registered category is always present in
    the payload — an empty ``{}`` rather than an absent key — which keeps the
    wire shape stable even when a whole category has no data yet.
    """
    seen: dict[str, None] = {}
    for source in _SOURCES.values():
        seen.setdefault(source.category, None)
    return tuple(seen)


def sources_for_path(path: str) -> tuple[StatusSource, ...]:
    """Sources one of whose ``owns`` prefixes contains ``path``.

    Used by the invalidation middleware. Matching is a plain prefix test, so
    ``/api/connectors/github`` covers ``/connect``, ``/disconnect``,
    ``/reconnect`` and anything added later under it.
    """
    if not path:
        return ()
    return tuple(
        source
        for source in _SOURCES.values()
        if any(path.startswith(prefix) for prefix in source.owns)
    )


def clear() -> None:
    """Drop every registered source. For tests only."""
    _SOURCES.clear()


__all__ = [
    "ProbeFn",
    "StatusSource",
    "categories",
    "clear",
    "get",
    "iter_sources",
    "register",
    "sources_for_path",
]
