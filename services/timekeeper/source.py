"""Recursive inotify watcher. Walks the root once at startup, watches every
directory not on the prune list, and dynamically adds/removes watches as
subdirs are created/deleted/renamed at runtime.

Runs in a background thread, pushes normalised event dicts into a queue
the asyncio drain task consumes. ``inotify_simple`` is synchronous, so a
thread is the simplest bridge.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import queue as queue_mod
import threading
from pathlib import Path
from typing import Optional

from inotify_simple import INotify, flags

logger = logging.getLogger("timekeeper.source")

# What we ask the kernel to deliver. Modification-only — we drop the
# noisy IN_OPEN / IN_ACCESS / IN_CLOSE_NOWRITE family on purpose.
WATCH_MASK = (
    flags.CREATE | flags.DELETE | flags.MODIFY
    | flags.MOVED_FROM | flags.MOVED_TO
    | flags.CLOSE_WRITE | flags.ATTRIB
    | flags.DELETE_SELF | flags.MOVE_SELF
    | flags.EXCL_UNLINK
)

# Priority order matters: a single event can carry multiple bits (e.g.
# CLOSE_WRITE often co-occurs with MODIFY). Pick the most specific op.
_OP_PRIORITY: tuple[tuple[int, str], ...] = (
    (flags.MOVED_FROM, "moved_from"),
    (flags.MOVED_TO,   "moved_to"),
    (flags.CREATE,     "create"),
    (flags.DELETE,     "delete"),
    (flags.CLOSE_WRITE,"close_write"),
    (flags.MODIFY,     "modify"),
    (flags.ATTRIB,     "attrib"),
)


def _op_from_mask(mask: int) -> str:
    for bit, name in _OP_PRIORITY:
        if mask & bit:
            return name
    return f"mask_{mask:#x}"


def _now_iso() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class InotifySource:
    def __init__(
        self,
        root: Path,
        out_queue: "queue_mod.Queue[dict]",
        ignore_prefixes: tuple[str, ...],
        ignore_substrings: tuple[str, ...],
        prune_dirs: frozenset[str],
    ) -> None:
        self.root = root
        self.out_queue = out_queue
        self.ignore_prefixes = ignore_prefixes
        self.ignore_substrings = ignore_substrings
        self.prune_dirs = prune_dirs
        self.inotify = INotify()
        self.wd_to_path: dict[int, Path] = {}
        self.dropped = 0
        self._stop = threading.Event()

    # ── Filtering ───────────────────────────────────────────────────────

    def _path_ok(self, p: Path) -> bool:
        s = str(p)
        for prefix in self.ignore_prefixes:
            if s.startswith(prefix):
                return False
        for sub in self.ignore_substrings:
            if sub in s:
                return False
        return True

    # ── Watch management ────────────────────────────────────────────────

    def _add_watch(self, p: Path) -> None:
        if not self._path_ok(p):
            return
        try:
            wd = self.inotify.add_watch(str(p), WATCH_MASK)
        except OSError as e:
            # ENOSPC = inotify watch limit; EACCES/ENOENT = transient. Log
            # at debug so we don't spam.
            logger.debug("add_watch %s failed: %s", p, e)
            return
        self.wd_to_path[wd] = p

    def _walk(self) -> None:
        n = 0
        # followlinks=False — symlink loops would explode the watch count.
        for dirpath, dirnames, _ in os.walk(self.root, followlinks=False):
            p = Path(dirpath)
            if not self._path_ok(p):
                dirnames.clear()
                continue
            # Prune by basename so we don't even stat into noisy subtrees.
            dirnames[:] = [d for d in dirnames if d not in self.prune_dirs]
            self._add_watch(p)
            n += 1
            if n % 5000 == 0:
                logger.info("inotify: %d directories watched so far", n)
        logger.info("inotify: ready, %d directories under %s", n, self.root)

    # ── Run / stop ──────────────────────────────────────────────────────

    def run(self) -> None:
        # print(flush=True), not logger: see daemon.run's note. Also, daemon
        # threads swallow exceptions silently — the except block is the only
        # way a crash in here becomes visible.
        try:
            self._walk()
            print(
                f"[timekeeper] watching {len(self.wd_to_path)} directories "
                f"under {self.root}",
                flush=True,
            )
            while not self._stop.is_set():
                events = self.inotify.read(timeout=500, read_delay=10)
                for ev in events:
                    self._handle(ev)
        except BaseException as exc:
            print(f"[timekeeper] reader thread CRASHED: {exc!r}", flush=True)
            import traceback
            traceback.print_exc()
            raise

    def stop(self) -> None:
        self._stop.set()

    # ── Hot path ────────────────────────────────────────────────────────

    def _handle(self, ev) -> None:  # ev is inotify_simple.Event
        base = self.wd_to_path.get(ev.wd)
        if base is None:
            return
        full = base / ev.name if ev.name else base

        # Dir lifecycle first — these must run regardless of filter result
        # so our wd map stays consistent.
        is_dir = bool(ev.mask & flags.ISDIR)
        if is_dir and (ev.mask & (flags.CREATE | flags.MOVED_TO)):
            if ev.name and ev.name not in self.prune_dirs:
                self._add_watch_recursive_with_synthetics(full)
        if ev.mask & (flags.IGNORED | flags.DELETE_SELF | flags.MOVE_SELF):
            self.wd_to_path.pop(ev.wd, None)
            # Don't emit synthetic events for the self-deletion of a watched
            # dir; the parent already got DELETE/MOVED_FROM with ISDIR.
            return

        if not self._path_ok(full):
            return

        self._emit({
            "ts": _now_iso(),
            "op": _op_from_mask(ev.mask),
            "path": str(full),
            "is_dir": is_dir,
        })

    # ── Race handling ───────────────────────────────────────────────────
    # When a new subdir is created at runtime, inotify delivers the CREATE
    # for the dir itself, but any files written into it *before* we add the
    # watch are lost. Fix: scan the dir on add, synthesise create events
    # for whatever's there, and recurse into nested dirs. Rare false dupes
    # are acceptable.

    def _add_watch_recursive_with_synthetics(self, p: Path) -> None:
        if not self._path_ok(p):
            return
        self._add_watch(p)
        try:
            entries = list(os.scandir(p))
        except OSError:
            return
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            child = Path(entry.path)
            if not self._path_ok(child):
                continue
            self._emit({
                "ts": _now_iso(),
                "op": "create",
                "path": str(child),
                "is_dir": is_dir,
            })
            if is_dir and entry.name not in self.prune_dirs:
                self._add_watch_recursive_with_synthetics(child)

    def _emit(self, event: dict) -> None:
        try:
            self.out_queue.put_nowait(event)
        except queue_mod.Full:
            self.dropped += 1
