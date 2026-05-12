"""Atomic file writes for the watcher's sinks.

Every ``.xo/`` file the watcher rewrites goes through
:func:`write_json_atomic`. The BFF reader either sees the previous
revision or the new one — never a partial / torn write — because
``os.replace`` is atomic on the same filesystem.

Why a dedicated helper rather than each sink doing its own ``write +
rename``: one place to enforce ``ensure_ascii=False`` (we want UTF-8
on disk), ``sort_keys=False`` (preserve template ordering for diff
readability), and the trailing newline. Also a single test target.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, data: Any) -> None:
    """Atomically write ``data`` as JSON to ``path``.

    Parent directories are created on demand (the watcher's
    workspace-tier sink writes the first-ever workspace ``.xo/``
    directory this way). Writes a sibling ``<path>.tmp`` file and
    ``os.replace`` it over the target — atomic on POSIX as long as
    both paths live on the same filesystem (true for everything under
    ``~/xo-projects/`` in practice).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def append_jsonl(path: Path, lines: list[dict]) -> None:
    """Append one or more JSON objects as JSONL lines, then ``fsync``.

    Used by the timeline sink. Append is non-atomic at the *line*
    level (each ``write`` syscall is atomic for sub-PIPE_BUF buffers
    on Linux, but multi-event batches may interleave with concurrent
    writers). The timeline file has exactly one writer — the
    watcher — so interleaving cannot happen. ``fsync`` after the batch
    ensures events survive a server crash.
    """
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(payload)
        fp.flush()
        os.fsync(fp.fileno())
