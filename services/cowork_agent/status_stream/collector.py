"""
The shared status collector behind `GET /api/status/stream`.

One background task probes every registered :class:`StatusSource` and keeps an
in-memory snapshot; SSE connections read that snapshot instead of probing for
themselves. The cost of watching the workspace is therefore **O(1) in connected
clients** — ten open browser tabs cost exactly what one costs. A per-connection
polling loop would multiply every outbound GitHub call and every ``rclone``
subprocess by the number of viewers, which is what makes the naive version
unusable rather than merely wasteful.

Two independent cadences
------------------------
*Push* cadence is uniform: each connection is sent a full snapshot every
``push_interval`` seconds, so the frontend can replace its state wholesale and
never merge. *Probe* cadence is per-source, because probe costs differ by
orders of magnitude — an env-var read is free, ``claude auth status`` is a
subprocess, GitHub token validation is an HTTPS round-trip against a
5000/hour rate limit. Pushing every 5s while probing GitHub every 60s is the
whole point of separating them.

Slow polling is only acceptable because of the other half: a status change the
*user* causes arrives through this API, so :func:`invalidate` re-probes that
source at once. Polling is left to catch what we do not control — a token
expiring, someone running a CLI logout in a terminal — which is rare and
tolerates being noticed a minute late.

Failure policy
--------------
A status feed's worst failure is silence that looks like health, so:

* a probe that raises or times out **keeps the last known good value** rather
  than reporting a disconnect (a transient blip must not flip the UI), and its
  interval is still honoured so a permanently broken probe cannot become a
  retry storm;
* a source that has *never* succeeded contributes **no key at all**, because
  inventing ``connected: false`` when the truth is "cannot tell" manufactures
  exactly the wrong-status bug this feed exists to eliminate;
* the loop catches everything and continues — one bad source must never be
  able to freeze every client's view;
* every probe runs under a timeout inside ``gather``, so one hung ``rclone``
  or blackholed socket cannot stall the other categories.

The collector is process-local, like ``engine/chat_state.py`` and
``skill_catalog.py``: N uvicorn workers means N collectors and N× probe cost.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from . import registry
from .registry import StatusSource

log = logging.getLogger(__name__)

#: Seconds between snapshot pushes to each connected client.
DEFAULT_PUSH_INTERVAL = 5.0

#: Hard ceiling on a single probe. Without it, one hung subprocess or dead
#: socket would stall the whole pass and every category with it.
DEFAULT_PROBE_TIMEOUT = 10.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StatusCollector:
    """Probes registered sources on a schedule and fans snapshots out.

    ``clock`` (monotonic, for scheduling) and ``now`` (wall clock, for the
    payload's ``ts``) are injectable so tests can drive time forward without
    sleeping. ``sources`` defaults to the live registry but can be supplied
    directly, which is what lets tests exercise the scheduling logic with
    trivial fake probes — the suite's network and subprocess guards would
    otherwise make this untestable.
    """

    def __init__(
        self,
        *,
        sources: Iterable[StatusSource] | None = None,
        clock: Callable[[], float] | None = None,
        now: Callable[[], datetime] = _utc_now,
        push_interval: float = DEFAULT_PUSH_INTERVAL,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    ) -> None:
        self._explicit_sources = tuple(sources) if sources is not None else None
        self._clock = clock or time.monotonic
        self._now = now
        self.push_interval = push_interval
        self.probe_timeout = probe_timeout

        self._values: dict[str, dict[str, Any]] = {}
        self._last_probed: dict[str, float] = {}
        self._dirty: set[str] = set()
        self._subscribers: set[asyncio.Queue] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ── sources ──────────────────────────────────────────────────────────────

    def _sources(self) -> tuple[StatusSource, ...]:
        if self._explicit_sources is not None:
            return self._explicit_sources
        return registry.iter_sources()

    def _categories(self) -> tuple[str, ...]:
        if self._explicit_sources is not None:
            seen: dict[str, None] = {}
            for source in self._explicit_sources:
                seen.setdefault(source.category, None)
            return tuple(seen)
        return registry.categories()

    # ── snapshot ─────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Assemble the current full snapshot.

        Every registered category is always present (empty when it has no data),
        so the wire shape does not change shape underneath the frontend just
        because a probe has not landed yet.
        """
        payload: dict[str, Any] = {
            "ts": self._now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        for category in self._categories():
            payload[category] = {}
        for source in self._sources():
            values = self._values.get(source.name)
            if not values:
                continue
            payload.setdefault(source.category, {}).update(values)
        return payload

    # ── subscriptions ────────────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        """Register a subscriber queue.

        ``maxsize=1`` with newest-wins replacement in :meth:`_broadcast` means a
        slow client coalesces to the latest snapshot rather than building a
        backlog. With full-snapshot semantics an undelivered snapshot is already
        obsolete, so dropping it loses nothing — and it guarantees one stalled
        reader can never block the collector or the other subscribers.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Drop a subscriber. Must run in a ``finally`` on the streaming path —
        a leaked queue here is the classic SSE memory leak, where every browser
        refresh adds a permanent subscriber."""
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _broadcast(self) -> None:
        if not self._subscribers:
            return
        payload = self.snapshot()
        for queue in list(self._subscribers):
            # Newest wins: clear a stale pending item, then enqueue.
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover - raced another drain
                pass

    # ── invalidation ─────────────────────────────────────────────────────────

    def invalidate(self, name: str) -> None:
        """Mark a source stale and wake the loop to re-probe it now."""
        self._dirty.add(name)
        self._wake.set()

    # ── probing ──────────────────────────────────────────────────────────────

    def _is_due(self, source: StatusSource, when: float) -> bool:
        if source.name in self._dirty:
            return True
        last = self._last_probed.get(source.name)
        if last is None:
            return True
        return (when - last) >= source.interval

    async def _probe_one(self, source: StatusSource) -> None:
        try:
            values = await asyncio.wait_for(source.probe(), timeout=self.probe_timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning(
                "status probe %r timed out after %.0fs; keeping last known value",
                source.name, self.probe_timeout,
            )
        except Exception as exc:
            # Deliberately not re-raised: a failing source keeps its previous
            # value and the other sources are unaffected.
            log.warning(
                "status probe %r failed (%s); keeping last known value",
                source.name, exc,
            )
        else:
            if isinstance(values, dict):
                self._values[source.name] = {
                    key: value for key, value in values.items() if isinstance(value, dict)
                }
            else:
                log.warning(
                    "status probe %r returned %s, expected a mapping; ignoring",
                    source.name, type(values).__name__,
                )
        finally:
            # Stamped on failure too, so a permanently broken probe respects its
            # interval instead of being retried on every pass.
            self._last_probed[source.name] = self._clock()
            self._dirty.discard(source.name)

    async def refresh(self) -> None:
        """Probe every source that is due (or dirty), concurrently."""
        when = self._clock()
        due = [source for source in self._sources() if self._is_due(source, when)]
        if not due:
            return
        await asyncio.gather(
            *(self._probe_one(source) for source in due),
            return_exceptions=True,
        )

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """The collector loop. Never returns except via cancellation."""
        log.info(
            "status collector started (%d sources, push every %.0fs)",
            len(self._sources()), self.push_interval,
        )
        while True:
            # Cleared *before* the pass, not after: an invalidate() arriving
            # while refresh() is in flight must survive into the next wait, or
            # its wake-up is swallowed and the "immediate" re-probe it asked for
            # is delayed by a full push interval. The dirty set is the source of
            # truth either way — the event is only the wake-up hint.
            self._wake.clear()
            try:
                await self.refresh()
                self._broadcast()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The loop outliving its errors is the point: if it dies, every
                # client silently keeps a stale snapshot forever.
                log.exception("status collector pass failed (continuing): %s", exc)

            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.push_interval)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

    def start(self) -> asyncio.Task:
        """Start the loop if it is not already running."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - defensive
            pass
        self._task = None


# ── process-wide singleton ───────────────────────────────────────────────────

_collector: StatusCollector | None = None


def get_collector() -> StatusCollector:
    """The process-wide collector, created on first use.

    Lazy creation means the SSE route still serves (an empty snapshot) if the
    loop was never started — degrading to "no data yet" instead of a 500.
    """
    global _collector
    if _collector is None:
        _collector = StatusCollector()
    return _collector


def start_collector() -> asyncio.Task:
    """Register the default sources and start the loop. Idempotent."""
    from . import sources

    sources.register_default_sources()
    return get_collector().start()


async def stop_collector() -> None:
    global _collector
    if _collector is not None:
        await _collector.stop()


def invalidate(name: str) -> None:
    """Mark a source stale. No-op when no collector exists yet."""
    if _collector is not None:
        _collector.invalidate(name)


def reset_collector() -> None:
    """Drop the singleton. For tests only."""
    global _collector
    _collector = None


__all__ = [
    "DEFAULT_PROBE_TIMEOUT",
    "DEFAULT_PUSH_INTERVAL",
    "StatusCollector",
    "get_collector",
    "invalidate",
    "reset_collector",
    "start_collector",
    "stop_collector",
]
