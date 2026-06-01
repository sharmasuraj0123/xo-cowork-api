"""Timekeeper daemon — recursive inotify watcher on a configured root
(default: the xo-projects root), filters events, batch-writes JSONL to
``timekeeper/``.

Runs as the invoking user. No root or special capabilities needed.

Entry: ``python -m services.timekeeper`` (see ``__main__.py``).
"""

from __future__ import annotations

import asyncio
import logging
import queue as queue_mod
import signal
import sys
import threading

from services.timekeeper import config
from services.timekeeper.filters import keep
from services.timekeeper.source import InotifySource
from services.timekeeper.writer import JsonlWriter

logger = logging.getLogger("timekeeper")


class Daemon:
    def __init__(self) -> None:
        # Plain queue.Queue (thread-safe) bridges the sync inotify reader
        # thread and the asyncio drain task.
        self.queue: "queue_mod.Queue[dict]" = queue_mod.Queue(maxsize=config.QUEUE_MAX)
        self.writer = JsonlWriter()
        self.source = InotifySource(
            root=config.WATCH_ROOT,
            out_queue=self.queue,
            ignore_prefixes=config.IGNORE_PATH_PREFIXES,
            ignore_substrings=config.IGNORE_PATH_SUBSTRINGS,
            prune_dirs=config.PRUNE_DIRS,
        )
        self._stop = asyncio.Event()
        self._reader_thread: threading.Thread | None = None

    # ── Drain consumer ──────────────────────────────────────────────────

    async def _drain(self) -> None:
        import time as _time
        batch: list[dict] = []
        last_flush = _time.monotonic()
        while not self._stop.is_set() or not self.queue.empty():
            try:
                ev = await asyncio.to_thread(
                    self.queue.get, True, config.FLUSH_INTERVAL_S
                )
                if keep(ev):
                    batch.append(ev)
            except queue_mod.Empty:
                pass
            now = _time.monotonic()
            time_due = (now - last_flush) >= config.FLUSH_INTERVAL_S
            if batch and (len(batch) >= config.FLUSH_BATCH_LINES or self.queue.empty() or time_due):
                dropped = self.source.dropped
                if dropped:
                    self.source.dropped = 0
                    batch.append({"op": "overflow", "dropped": dropped})
                try:
                    self.writer.write_batch(batch)
                except Exception:
                    logger.exception("writer batch failed")
                batch = []
                last_flush = now

    # ── Entry ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        # print(flush=True) rather than logging: uvicorn doesn't attach a
        # handler to the root logger, so logger.info would be swallowed in
        # the embedded (FastAPI lifespan) case. These few lines are the
        # operator's only window into whether the watcher actually came up.
        if not config.WATCH_ROOT.is_dir():
            print(
                f"[timekeeper] WATCH_ROOT does not exist: {config.WATCH_ROOT} "
                f"— nothing will be captured (check TIMEKEEPER_WATCH_ROOT)",
                flush=True,
            )
        print(
            f"[timekeeper] starting: root={config.WATCH_ROOT} "
            f"out={self.writer.out_dir} retention={config.RETENTION_DAYS}d",
            flush=True,
        )
        self._reader_thread = threading.Thread(
            target=self.source.run, name="inotify-reader", daemon=True,
        )
        self._reader_thread.start()
        try:
            await self._drain()
        except asyncio.CancelledError:
            self._stop.set()
            self.source.stop()
            self.writer.close()
            raise
        # Natural exit (stop event set by signal handler in standalone mode).
        self.source.stop()
        self.writer.close()
        print("[timekeeper] stopped", flush=True)


# ── Public entry-point used by server.py lifespan ───────────────────────────

_daemon: Daemon | None = None


async def start_timekeeper() -> None:
    """Construct (once) and run the daemon. Spawn as an asyncio task
    from FastAPI's lifespan; cancel on shutdown.
    """
    global _daemon
    if _daemon is None:
        _daemon = Daemon()
    await _daemon.run()


# ── Standalone CLI entry ────────────────────────────────────────────────────


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    daemon = Daemon()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, daemon._stop.set)
    await daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
