"""Append events to ``timekeeper/current.jsonl``; rotate daily, gzip rotated
files, prune older than ``RETENTION_DAYS``.

The writer is *not* fsync-per-event — we fsync only on rotation and on
shutdown. At machine scope, per-event fsync would dominate CPU.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import os
import shutil
from pathlib import Path

from services.timekeeper import config

logger = logging.getLogger(__name__)


class JsonlWriter:
    def __init__(self, out_dir: Path = config.OUTPUT_DIR) -> None:
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.current = self.out_dir / "current.jsonl"
        self._fh = None
        self._day: dt.date | None = None
        self._open_for_today()

    # ── Lifecycle ───────────────────────────────────────────────────────

    def _open_for_today(self) -> None:
        today = dt.datetime.now(dt.timezone.utc).date()
        # If current.jsonl exists from a previous day, rotate it before
        # appending today's events to a fresh file.
        if self.current.exists():
            mtime_day = dt.datetime.fromtimestamp(
                self.current.stat().st_mtime, dt.timezone.utc
            ).date()
            if mtime_day < today:
                self._rotate(mtime_day)
        self._fh = open(self.current, "ab", buffering=0)
        self._day = today

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError:
                pass
            self._fh.close()
            self._fh = None

    # ── Hot path ────────────────────────────────────────────────────────

    def write_batch(self, events: list[dict]) -> None:
        if not events:
            return
        today = dt.datetime.now(dt.timezone.utc).date()
        if self._day is not None and today != self._day:
            self._rotate(self._day)
            self._fh = open(self.current, "ab", buffering=0)
            self._day = today
        assert self._fh is not None
        buf = b"".join(
            (json.dumps(ev, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
            for ev in events
        )
        self._fh.write(buf)

    # ── Rotation + retention ────────────────────────────────────────────

    def _rotate(self, for_day: dt.date) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError:
                pass
            self._fh.close()
            self._fh = None
        if not self.current.exists() or self.current.stat().st_size == 0:
            # Empty file — just remove, don't gzip an empty archive.
            self.current.unlink(missing_ok=True)
        else:
            target = self.out_dir / f"{for_day.isoformat()}.jsonl.gz"
            tmp = target.with_suffix(".gz.tmp")
            with open(self.current, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, length=1 << 20)
            os.replace(tmp, target)
            self.current.unlink(missing_ok=True)
            logger.info("timekeeper: rotated %s (%d bytes gz)", target.name, target.stat().st_size)
        self._prune()

    def _prune(self) -> None:
        if config.RETENTION_DAYS <= 0:
            return
        cutoff = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=config.RETENTION_DAYS)
        for f in self.out_dir.glob("*.jsonl.gz"):
            stem = f.name.removesuffix(".jsonl.gz")
            try:
                day = dt.date.fromisoformat(stem)
            except ValueError:
                continue
            if day < cutoff:
                f.unlink(missing_ok=True)
                logger.info("timekeeper: pruned %s", f.name)
