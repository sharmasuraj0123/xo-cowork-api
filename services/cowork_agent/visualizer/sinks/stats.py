"""``stats.json`` sink — rolling 7d/30d + by_runtime + by_session.

Per the stats schema (``services/cowork_agent/project_template/.xo/
schema/stats.schema.json``):

* ``rolling.7d`` / ``rolling.30d`` — totals over the trailing window
  with ``{tokens, by_model, files_edited, sessions, active_minutes}``.
* ``by_session`` — per native session id, ``{tokens, files,
  duration_ms}``.
* ``by_runtime`` — totals bucketed by runtime (claude_code / openclaw).

The sink rebuilds from durable state stored INSIDE ``stats.json``
itself: a private ``_session_totals`` dict that's filtered on read
into the public ``by_session`` view. That way restart-after-crash
recovers state from disk; we don't need a sidecar.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    FileTouched,
    MessageObserved,
    SessionFirstSeen,
    UsageObserved,
)
from services.cowork_agent.visualizer.reader import read_json


_STATS_FILE = Path("stats.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_to_dt(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _empty_window() -> dict:
    return {
        "tokens":         {"input": 0, "output": 0},
        "by_model":       {},
        "files_edited":   0,
        "sessions":       0,
        "active_minutes": 0,
    }


def _empty_session_totals() -> dict:
    return {
        "tokens":          {"input": 0, "output": 0},
        "files":           [],
        "duration_ms":     0,
        "first_ts":        None,
        "last_ts":         None,
        "runtime":         "",
        "by_model_tokens": {},   # {model: {input, output}}
    }


def apply(xo_dir: Path, events: Iterable[Event]) -> bool:
    """Apply events to the project's stats file. Returns ``True`` if
    the file changed.
    """
    events = list(events)
    if not events:
        return False

    path = xo_dir / _STATS_FILE
    current = read_json(path) or {}
    private = dict(current.get("_session_totals") or {})

    changed = False

    for ev in events:
        nsid = ev.native_session_id
        if not nsid:
            continue
        st = private.setdefault(nsid, _empty_session_totals())
        if not st["runtime"]:
            st["runtime"] = ev.runtime

        # Stamp first/last seen
        if isinstance(ev, SessionFirstSeen) or st["first_ts"] is None:
            st["first_ts"] = st["first_ts"] or ev.ts
        st["last_ts"] = ev.ts

        if isinstance(ev, MessageObserved):
            changed = True

        elif isinstance(ev, UsageObserved):
            st["tokens"]["input"]  = int(st["tokens"]["input"])  + ev.input_tokens
            st["tokens"]["output"] = int(st["tokens"]["output"]) + ev.output_tokens
            model = ev.model or ""
            if model:
                m = st["by_model_tokens"].setdefault(model, {"input": 0, "output": 0})
                m["input"]  += ev.input_tokens
                m["output"] += ev.output_tokens
            changed = True

        elif isinstance(ev, FileTouched):
            if ev.relative_path and ev.relative_path not in st["files"]:
                st["files"].append(ev.relative_path)
            changed = True

    if not changed:
        return False

    # Compute duration_ms per session
    for st in private.values():
        d1 = _iso_to_dt(st.get("first_ts") or "")
        d2 = _iso_to_dt(st.get("last_ts") or "")
        if d1 and d2 and d2 >= d1:
            st["duration_ms"] = int((d2 - d1).total_seconds() * 1000)

    # ── Build the public stats shape ────────────────────────────────────
    by_session = {
        sid: {
            "tokens":      dict(s["tokens"]),
            "files":       list(s["files"]),
            "duration_ms": int(s["duration_ms"]),
        }
        for sid, s in private.items()
    }

    by_runtime: dict[str, dict] = {}
    for s in private.values():
        rt = s["runtime"] or "unknown"
        bucket = by_runtime.setdefault(rt, _empty_window())
        bucket["tokens"]["input"]  += s["tokens"]["input"]
        bucket["tokens"]["output"] += s["tokens"]["output"]
        bucket["files_edited"] += len(s["files"])
        bucket["sessions"] += 1
        bucket["active_minutes"] += s["duration_ms"] // 60_000
        for model, mt in s["by_model_tokens"].items():
            bm = bucket["by_model"].setdefault(model, {"input": 0, "output": 0})
            bm["input"]  += mt["input"]
            bm["output"] += mt["output"]

    # ── Rolling 7d / 30d windows ────────────────────────────────────────
    # Each session contributes to a window if its `last_ts` is inside
    # the window. Cheap and exact enough for v1 (per-event bucketing
    # is Phase 3 polish).
    now = datetime.now(timezone.utc)
    rolling = {"7d": _empty_window(), "30d": _empty_window()}

    for s in private.values():
        last = _iso_to_dt(s.get("last_ts") or "")
        if last is None:
            continue
        age = (now - last).total_seconds()
        for window, window_seconds in (("7d", 7 * 86400), ("30d", 30 * 86400)):
            if age <= window_seconds:
                w = rolling[window]
                w["tokens"]["input"]  += s["tokens"]["input"]
                w["tokens"]["output"] += s["tokens"]["output"]
                w["files_edited"] += len(s["files"])
                w["sessions"] += 1
                w["active_minutes"] += s["duration_ms"] // 60_000
                for model, mt in s["by_model_tokens"].items():
                    bm = w["by_model"].setdefault(model, {"input": 0, "output": 0})
                    bm["input"]  += mt["input"]
                    bm["output"] += mt["output"]

    payload = {
        "schema": 1,
        "updated_at": _now_iso(),
        "rolling": rolling,
        "by_session": by_session,
        "by_runtime": by_runtime,
        "_session_totals": private,  # private — stripped on serialise by BFF route allowlist
    }
    write_json_atomic(path, payload)
    return True
