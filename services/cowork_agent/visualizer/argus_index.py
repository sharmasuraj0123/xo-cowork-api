"""Argus graph builder — maps an Argus telemetry DB to the space.json shape.

Pure reader: opens the Claude Code session-telemetry SQLite DB **read-only**
and returns the graph document the Space UI consumes. Writes nothing.
Served by routers/space.py (GET /space/data/argus.json).

Mapping: root=ARGUS, hub=project (sessions.project_path), group=month bucket,
leaf=session (disc) / subagent run (diamond + "spawned by" tie to its parent).
Leaf date = started_at date — what the UI timeline scrubs.

Failure policy: fail fast when data would be wrong (missing DB file, missing
sessions table); degrade when merely incomplete (missing tool_calls/alerts/
app_meta → that feature is absent, the graph still builds).
"""

from __future__ import annotations

import calendar
import math
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

# Same muted palette as space_index (copied, not imported — reaching into a
# sibling module's private name would couple us to its internals).
_PALETTE = [
    "#a2b56b", "#7fb3c8", "#c8a06b", "#b58a9e",
    "#8fbf9f", "#c4bd72", "#9a93d0", "#c88585",
]

# Hard bounds — each constant is an assumption made explicit:
MAX_SESSIONS_PER_PROJECT = 60  # wider hub fans are unreadable; top-by-tokens carries the story
MAX_TOTAL_LEAVES = 1500        # same browser render bound as space_index
MAX_TIES = 300                 # spawn ties only (one per kept subagent)
_TOP_TOOLS_PER_SESSION = 5     # hover blurb must stay one card tall
_MAX_ROOT_ALERTS = 3           # root blurb is a sentence, not a pager
_BUSY_TIMEOUT_MS = 2000        # Argus writes while we read; brief waits ok, stalls become a 503
_EXPECTED_SCHEMA = 6           # argus.db app_meta.schema_version this was written against


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(
            f"Argus DB not found at {db_path} (set ARGUS_DB to change)")
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.execute(f"pragma busy_timeout={_BUSY_TIMEOUT_MS}")
    return con


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}K"
    return str(n)


def _fmt_duration(sec) -> str:
    if not sec:
        return "0m"
    minutes = int(sec) // 60
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "unknown"


def _month_label(day: str) -> str:  # "2026-06-11" -> "June 2026"
    return f"{calendar.month_name[int(day[5:7])]} {day[:4]}"


def _load_sessions(con: sqlite3.Connection) -> list[dict]:
    try:
        rows = con.execute(
            "select id, project_path, started_at, duration_sec,"
            " total_fresh_input_tokens, total_output_tokens,"
            " total_cache_read_tokens, total_cache_write_tokens,"
            " total_cost_usd, primary_model, turn_count"
            " from sessions where started_at is not null"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise ValueError(f"not an Argus DB (sessions unreadable: {exc})") from exc
    out = []
    for (sid, project, started, dur, fresh, outt, cr, cw,
         cost, model, turns) in rows:
        fresh, outt, cr, cw = (fresh or 0), (outt or 0), (cr or 0), (cw or 0)
        out.append({
            "id": sid,
            "project": (project or "").strip(),
            "date": started[:10],
            "time": started[11:16],
            "duration": dur,
            "fresh": fresh, "out": outt, "cache_r": cr, "cache_w": cw,
            "tokens": fresh + outt + cr + cw,
            "cost": cost or 0.0,
            "model": model or "",
            "turns": turns or 0,
            # Subagent runs are session rows namespaced under their parent:
            # "claude_code:<uuid>/agent-<hash>".
            "parent_id": sid.split("/", 1)[0] if "/" in sid else None,
        })
    return out


def _tool_stats(con: sqlite3.Connection) -> dict[str, list[tuple[str, int, int]]]:
    """session_id -> [(tool, calls, errors)] sorted by calls desc.
    Missing table (older Argus) -> {}: tool stats degrade, graph still builds."""
    try:
        rows = con.execute(
            "select session_id, tool_name, count(*), sum(coalesce(is_error,0))"
            " from tool_calls group by session_id, tool_name").fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, list[tuple[str, int, int]]] = {}
    for sid, tool, n, errs in rows:
        out.setdefault(sid, []).append((tool or "?", n, errs or 0))
    for sid in out:
        out[sid].sort(key=lambda t: -t[1])
    return out


def _open_alerts(con: sqlite3.Connection) -> list[tuple[str, str]]:
    try:
        return con.execute(
            "select severity, title from alerts where resolved_at is null"
            " order by last_seen_at desc").fetchall()
    except sqlite3.OperationalError:
        return []


def _check_schema(con: sqlite3.Connection) -> None:
    try:
        row = con.execute(
            "select value from app_meta where key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return
    if row and str(row[0]) != str(_EXPECTED_SCHEMA):
        print(f"argus_index: schema_version {row[0]} != expected "
              f"{_EXPECTED_SCHEMA}; building anyway (fields may be missing)")


def _session_blurb(s: dict, tools: list[tuple[str, int, int]]) -> str:
    parts = [
        f"{_fmt_tokens(s['tokens'])} tokens",
        f"~${s['cost']:.2f}",
        f"{s['turns']} turns",
        _fmt_duration(s["duration"]),
        s["model"] or "unknown model",
        (f"fresh {_fmt_tokens(s['fresh'])} / out {_fmt_tokens(s['out'])}"
         f" / cache r {_fmt_tokens(s['cache_r'])} w {_fmt_tokens(s['cache_w'])}"),
    ]
    if tools:
        parts.append("tools: " + ", ".join(
            f"{t} ×{n}" + (f" ({e} err)" if e else "")
            for t, n, e in tools[:_TOP_TOOLS_PER_SESSION]))
    return " · ".join(parts)


def _model_tag(model: str) -> str:
    short = model.removeprefix("claude-").upper()
    return short or "SESSION"


def build_argus_data(db_path: Path) -> dict:
    con = _connect_ro(db_path)
    try:
        _check_schema(con)
        sessions = _load_sessions(con)
        tool_stats = _tool_stats(con)
        open_alerts = _open_alerts(con)
    finally:
        con.close()

    parents = [s for s in sessions if not s["parent_id"] and s["tokens"] > 0]
    subs_by_parent: dict[str, list[dict]] = {}
    for s in sessions:
        if s["parent_id"] and s["tokens"] > 0:
            subs_by_parent.setdefault(s["parent_id"], []).append(s)

    by_project: dict[str, list[dict]] = {}
    for s in parents:
        by_project.setdefault(s["project"] or "(unknown)", []).append(s)

    categories: dict = {}
    hub_angles: dict = {}
    hubs: list[dict] = []
    groups: list[dict] = []
    leaves: list[dict] = []
    ties: list[dict] = []
    milestones: list[dict] = []

    n = max(len(by_project), 1)
    for i, ppath in enumerate(sorted(by_project)):
        plist = by_project[ppath]
        display = ppath.rstrip("/").rsplit("/", 1)[-1] or "(unknown)"
        cat = f"p_{_slug(ppath)}"
        plist.sort(key=lambda s: -s["tokens"])
        kept = plist[:MAX_SESSIONS_PER_PROJECT]
        overflow = len(plist) - len(kept)

        categories[cat] = {"name": display, "color": _PALETTE[i % len(_PALETTE)]}
        hub_angles[cat] = -math.pi / 2 + i * 2 * math.pi / n
        ptok = sum(s["tokens"] for s in plist)
        pcost = sum(s["cost"] for s in plist)
        blurb = (f"{len(plist)} sessions · {_fmt_tokens(ptok)} tokens"
                 f" · ~${pcost:.2f}")
        if overflow:
            blurb += f" · showing top {len(kept)} by tokens (+{overflow} more)"
        hubs.append({"id": cat, "cat": cat, "label": display, "blurb": blurb})

        month_gids: dict[str, str] = {}
        for s in kept:
            month = s["date"][:7]
            gid = month_gids.get(month)
            if gid is None:
                gid = f"g_{_slug(ppath)}_{month}"
                month_gids[month] = gid
                groups.append({"id": gid, "cat": cat,
                               "label": _month_label(s["date"]),
                               "blurb": f"Sessions started in {_month_label(s['date'])}."})
            leaves.append({
                "id": s["id"], "group": gid, "shape": "disc",
                "tag": _model_tag(s["model"]),
                "label": f"{s['date']} {s['time']}",
                "date": s["date"],
                "blurb": _session_blurb(s, tool_stats.get(s["id"], [])),
                "path": f"{display}/{s['id']}",
            })
            for sub in subs_by_parent.get(s["id"], []):
                short = sub["id"].rsplit("/", 1)[-1]
                leaves.append({
                    "id": sub["id"], "group": gid, "shape": "diamond",
                    "tag": "AGENT",
                    "label": short[:14],
                    "date": sub["date"],
                    "blurb": _session_blurb(sub, tool_stats.get(sub["id"], [])),
                    "path": f"{display}/{short}",
                })
                ties.append({"s": sub["id"], "t": s["id"], "label": "spawned by"})

        first = min(kept, key=lambda s: s["date"], default=None)
        if first:
            milestones.append({"d": first["date"],
                               "t": f"{display} first session"})

    if len(leaves) > MAX_TOTAL_LEAVES:
        dropped = len(leaves) - MAX_TOTAL_LEAVES
        leaves.sort(key=lambda leaf: leaf["date"], reverse=True)
        leaves = leaves[:MAX_TOTAL_LEAVES]
        kept_groups = {leaf["group"] for leaf in leaves}
        groups = [g for g in groups if g["id"] in kept_groups]
        print(f"argus_index: dropped {dropped} oldest leaves (cap {MAX_TOTAL_LEAVES})")

    # Ties last, against the final leaf set — a tie referencing a dropped
    # node hard-crashes the UI (byId.get(...).adj).
    leaf_ids = {leaf["id"] for leaf in leaves}
    ties = [t for t in ties if t["s"] in leaf_ids and t["t"] in leaf_ids]
    ties = ties[:MAX_TIES]

    if parents:
        biggest = max(parents, key=lambda s: s["tokens"])
        if biggest["id"] in leaf_ids:
            milestones.append({
                "d": biggest["date"],
                "t": f"largest session · {_fmt_tokens(biggest['tokens'])} tokens"})

    today = date.today()
    if leaves:
        dates = sorted(leaf["date"] for leaf in leaves)
        start = (date.fromisoformat(dates[0]) - timedelta(days=7)).isoformat()
        end = (date.fromisoformat(dates[-1]) + timedelta(days=7)).isoformat()
    else:
        start = (today - timedelta(days=7)).isoformat()
        end = (today + timedelta(days=7)).isoformat()

    total_tok = sum(s["tokens"] for s in parents)
    total_cost = sum(s["cost"] for s in parents)
    root_blurb = (f"{len(parents)} sessions · {_fmt_tokens(total_tok)} tokens"
                  f" · ~${total_cost:.2f} · {len(by_project)} projects")
    if open_alerts:
        root_blurb += " · ⚠ " + "; ".join(
            f"[{sev}] {title}" for sev, title in open_alerts[:_MAX_ROOT_ALERTS])

    return {
        "meta": {
            "title": "ARGUS",
            "tagline": "claude code session telemetry",
            "mappedOn": today.strftime("%d %B %Y"),
            "workspace": str(db_path),
        },
        "categories": categories,
        "hubAngles": hub_angles,
        "timeline": {"start": start, "end": end},
        "root": {"id": "argus", "label": "ARGUS", "blurb": root_blurb},
        "hubs": hubs,
        "groups": groups,
        "leaves": leaves,
        "ties": ties,
        "milestones": milestones,
    }
