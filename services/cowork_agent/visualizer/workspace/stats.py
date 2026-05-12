"""``~/xo-projects/.xo/stats.json`` — workspace stats = sum of every
project's ``stats.json``.

Same schema as per-project ``stats.json``. Recomputed from per-
project files each tick (no incremental state of its own).
"""

from __future__ import annotations

from datetime import datetime, timezone

from services.cowork_agent.project_layout import workspace_xo_dir, xo_dir
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.workspace_index import list_project_ids


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_window() -> dict:
    return {
        "tokens": {"input": 0, "output": 0},
        "by_model": {},
        "files_edited": 0,
        "sessions": 0,
        "active_minutes": 0,
    }


def _merge_window(into: dict, src: dict) -> None:
    """Sum one rolling-window dict into another in place."""
    src_tokens = src.get("tokens") or {}
    into["tokens"]["input"]  += int(src_tokens.get("input", 0) or 0)
    into["tokens"]["output"] += int(src_tokens.get("output", 0) or 0)
    into["files_edited"]   += int(src.get("files_edited", 0) or 0)
    into["sessions"]       += int(src.get("sessions", 0) or 0)
    into["active_minutes"] += int(src.get("active_minutes", 0) or 0)
    src_models = src.get("by_model") or {}
    if isinstance(src_models, dict):
        for model, mt in src_models.items():
            if not isinstance(mt, dict):
                continue
            bm = into["by_model"].setdefault(model, {"input": 0, "output": 0})
            bm["input"]  += int(mt.get("input", 0) or 0)
            bm["output"] += int(mt.get("output", 0) or 0)


def apply() -> bool:
    """Recompute and write workspace ``stats.json``. Returns ``True``."""
    rolling = {"7d": _empty_window(), "30d": _empty_window()}
    by_session: dict[str, dict] = {}
    by_runtime: dict[str, dict] = {}

    for pid in list_project_ids():
        st = read_json(xo_dir(pid) / "stats.json")
        if not isinstance(st, dict):
            continue
        r = st.get("rolling") or {}
        if isinstance(r.get("7d"), dict):
            _merge_window(rolling["7d"], r["7d"])
        if isinstance(r.get("30d"), dict):
            _merge_window(rolling["30d"], r["30d"])
        bs = st.get("by_session") or {}
        if isinstance(bs, dict):
            for sid, sdata in bs.items():
                if isinstance(sdata, dict):
                    by_session[sid] = sdata  # session-ids globally unique
        br = st.get("by_runtime") or {}
        if isinstance(br, dict):
            for rt, rdata in br.items():
                if not isinstance(rdata, dict):
                    continue
                bucket = by_runtime.setdefault(rt, _empty_window())
                _merge_window(bucket, rdata)

    payload = {
        "schema": 1,
        "updated_at": _now_iso(),
        "rolling": rolling,
        "by_session": by_session,
        "by_runtime": by_runtime,
    }
    write_json_atomic(workspace_xo_dir() / "stats.json", payload)
    return True
