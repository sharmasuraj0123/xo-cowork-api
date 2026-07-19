"""Argus stats builder — pre-aggregated session telemetry for the Space UI.

Pure reader: opens the Argus session-telemetry SQLite DB **read-only** and
returns one JSON-able payload for GET /space/data/sessions.json. The Sessions
tab renders it directly: session list (subagents nested), per-day rollups for
client-side windowing (models, sessions, tools). Argus records the originating
agent on every session; that dimension is preserved throughout the payload so
several providers can be displayed and filtered together.

Failure policy: fail fast when data would be wrong (missing DB file, missing
sessions table); degrade when merely incomplete (missing turns/tool_calls/
app_meta → that key is empty, the rest builds). No alerts and no prompts —
those tables are deliberately never queried (user decision; prompts carry
raw typed text, which also keeps it out of the payload).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Bounds — each constant is an assumption made explicit:
MAX_SESSIONS = 500          # newest sessions shipped; the API runs in every
                            # user's workspace, so payload and table size must
                            # not grow without bound as history accumulates
MAX_TOOLS_PER_SESSION = 10  # per-session tool rollup rows (detail card height)
_BUSY_TIMEOUT_MS = 2000     # Argus writes while we read; brief waits ok
_EXPECTED_SCHEMA = 6        # argus.db app_meta.schema_version this targets

_TURN_TOK = ("coalesce(t.fresh_input_tokens,0)+coalesce(t.output_tokens,0)"
             "+coalesce(t.cache_read_tokens,0)+coalesce(t.cache_write_tokens,0)")


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(
            f"Argus DB not found at {db_path} (set ARGUS_DB to change)")
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.execute(f"pragma busy_timeout={_BUSY_TIMEOUT_MS}")
    return con


def _schema_version(con: sqlite3.Connection) -> str | None:
    try:
        row = con.execute(
            "select value from app_meta where key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return None
    v = row[0] if row else None
    if v is not None and str(v) != str(_EXPECTED_SCHEMA):
        print(f"argus_index: schema_version {v} != expected {_EXPECTED_SCHEMA};"
              " building anyway (fields may be missing)")
    return str(v) if v is not None else None


def _load_sessions(con: sqlite3.Connection) -> list[dict]:
    try:
        rows = con.execute(
            "select id, agent, project_path, started_at, ended_at, duration_sec,"
            " total_fresh_input_tokens, total_output_tokens,"
            " total_cache_read_tokens, total_cache_write_tokens,"
            " total_cost_usd, primary_model, turn_count, agent_version,"
            " pricing_table_version"
            " from sessions where started_at is not null"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise ValueError(f"not an Argus DB (sessions unreadable: {exc})") from exc
    out = []
    for (sid, agent, project, started, ended, dur, fresh, outp, cr, cw,
         cost, model, turns, aver, pver) in rows:
        fresh, outp, cr, cw = (fresh or 0), (outp or 0), (cr or 0), (cw or 0)
        tokens = fresh + outp + cr + cw
        # ARGUS "_is_meaningful": at least one of turns/cost/tokens non-zero.
        if not ((turns or 0) or tokens or (cost or 0)):
            continue
        out.append({
            "id": sid, "agent": agent or "unknown", "project_path": project or "",
            "started_at": started, "ended_at": ended, "duration_sec": dur,
            "fresh": fresh, "output": outp, "cache_read": cr,
            "cache_write": cw, "tokens": tokens, "cost": cost or 0.0,
            "model": model or "", "turns": turns or 0,
            "agent_version": aver or "", "pricing_version": pver,
        })
    return out


def _daily_rollup(con: sqlite3.Connection, expr: str, key: str) -> list[dict]:
    """Per-day turns rollup grouped by ``expr`` (model or session_id)."""
    try:
        rows = con.execute(
            f"select substr(t.timestamp,1,10), coalesce(s.agent,'unknown'),"
            f" {expr}, sum({_TURN_TOK}), sum(coalesce(t.cost_usd,0))"
            " from turns t left join sessions s on s.id=t.session_id"
            " where t.timestamp is not null group by 1, 2, 3").fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"day": d, "agent": a, key: k, "tokens": t or 0,
             "cost": c or 0.0, "cost_known": True}
            for d, a, k, t, c in rows]


def _tools_by_session(con: sqlite3.Connection) -> dict[str, list[dict]]:
    try:
        rows = con.execute(
            "select session_id, tool_name, count(*),"
            " sum(coalesce(is_error,0)) from tool_calls"
            " group by session_id, tool_name").fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, list[dict]] = {}
    for sid, name, calls, errors in rows:
        out.setdefault(sid, []).append(
            {"name": name or "?", "calls": calls, "errors": errors or 0})
    for sid in out:
        out[sid].sort(key=lambda t: -t["calls"])
    return out


def _daily_tools(con: sqlite3.Connection) -> list[dict]:
    try:
        rows = con.execute(
            "select substr(t.timestamp,1,10), coalesce(s.agent,'unknown'),"
            " t.tool_name, count(*), sum(coalesce(t.is_error,0))"
            " from tool_calls t left join sessions s on s.id=t.session_id"
            " where t.timestamp is not null group by 1, 2, 3").fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"day": d, "agent": a, "name": n or "?", "calls": c,
             "errors": e or 0}
            for d, a, n, c, e in rows]


def build_argus_stats(db_path: Path, *, agent: str | None = None) -> dict:
    """Build Argus telemetry, optionally restricted to one runtime agent."""
    con = _connect_ro(db_path)
    try:
        schema_version = _schema_version(con)
        raw = _load_sessions(con)
        daily_models = _daily_rollup(
            con, "coalesce(t.model,'unknown')", "model")
        daily_raw = _daily_rollup(con, "t.session_id", "session_id")
        tools_by_session = _tools_by_session(con)
        daily_tools = _daily_tools(con)
    finally:
        con.close()

    if agent is not None:
        raw = [row for row in raw if row["agent"] == agent]
        daily_models = [row for row in daily_models if row["agent"] == agent]
        daily_raw = [row for row in daily_raw if row["agent"] == agent]
        daily_tools = [row for row in daily_tools if row["agent"] == agent]
        allowed_session_ids = {row["id"] for row in raw}
        tools_by_session = {
            session_id: rows
            for session_id, rows in tools_by_session.items()
            if session_id in allowed_session_ids
        }

    # Subagent runs are session rows namespaced "parent/agent-…": their turn
    # burn is attributed to the parent in daily_sessions, and their totals
    # nest under the parent in the sessions list.
    merged: dict[tuple[str, str, str], dict] = {}
    for r in daily_raw:
        parent = r["session_id"].split("/", 1)[0]
        cur = merged.setdefault(
            (r["day"], r["agent"], parent),
            {"day": r["day"], "agent": r["agent"],
             "session_id": parent,
             "session_key": f'{r["agent"]}:{parent}',
             "tokens": 0, "cost": 0.0, "cost_known": True})
        cur["tokens"] += r["tokens"]
        cur["cost"] += r["cost"]

    parents = [s for s in raw if "/" not in s["id"]]
    parents.sort(key=lambda s: s["started_at"], reverse=True)
    subs: dict[str, list[dict]] = {}
    for s in raw:
        if "/" in s["id"]:
            subs.setdefault(s["id"].split("/", 1)[0], []).append(s)

    # Ship only the newest MAX_SESSIONS; totals below stay all-time truth.
    # daily_sessions is filtered to the kept ids so every id the UI meets in
    # a rollup resolves in its session lookup (no dangling references).
    kept = parents[:MAX_SESSIONS]
    kept_ids = {s["id"] for s in kept}
    daily_sessions = sorted(
        (r for r in merged.values() if r["session_id"] in kept_ids),
        key=lambda r: (r["day"], r["agent"], r["session_id"]))

    sessions = []
    for s in kept:
        sub_rows = subs.get(s["id"], [])
        total_tokens = s["tokens"]
        # Argus parent counters already include nested agent runs. Expose both
        # scopes so every telemetry provider can give the UI the same meaning:
        # ``total_tokens`` is tree-inclusive; ``own_tokens`` excludes the
        # separately listed subagent rows.
        own_tokens = max(
            0, total_tokens - sum(row["tokens"] for row in sub_rows)
        )
        sessions.append({
            "id": s["id"],
            "key": f'{s["agent"]}:{s["id"]}',
            "agent": s["agent"],
            "project": (s["project_path"].rstrip("/").rsplit("/", 1)[-1]
                        if s["project_path"] else "(unknown)"),
            "project_path": s["project_path"],
            "started_at": s["started_at"], "ended_at": s["ended_at"],
            "duration_sec": s["duration_sec"],
            "model": s["model"], "agent_version": s["agent_version"],
            "turns": s["turns"],
            "fresh": s["fresh"], "output": s["output"],
            "cache_read": s["cache_read"], "cache_write": s["cache_write"],
            "tokens": total_tokens, "own_tokens": own_tokens,
            "total_tokens": total_tokens, "unclassified": 0,
            "breakdown_known": True,
            "cost": s["cost"], "cost_known": True,
            "tools": tools_by_session.get(s["id"], [])[:MAX_TOOLS_PER_SESSION],
            "subagents": [{"id": x["id"], "tokens": x["tokens"],
                           "own_tokens": x["tokens"],
                           "total_tokens": x["tokens"],
                           "cost": x["cost"], "cost_known": True,
                           "turns": x["turns"]}
                          for x in sub_rows],
        })

    projects = {s["project_path"] or "(unknown)" for s in parents}
    sessions_by_agent: dict[str, int] = {}
    for session in parents:
        agent = session["agent"]
        sessions_by_agent[agent] = sessions_by_agent.get(agent, 0) + 1
    pricing = next((s["pricing_version"] for s in parents
                    if s["pricing_version"]), None)
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat()
            .replace("+00:00", "Z"),
            "db_path": str(db_path),
            "schema_version": schema_version,
            "pricing_version": pricing,
        },
        "totals": {
            "sessions": len(parents),
            "tokens": sum(s["tokens"] for s in parents),
            "cost_usd": sum(s["cost"] for s in parents),
            "projects": len(projects),
            "sessions_by_agent": sessions_by_agent,
        },
        # Internal provider metadata: unlike ``sessions``, this remains all-time
        # truth when the displayed list is capped at MAX_SESSIONS.
        "project_keys": sorted(projects),
        "daily_models": daily_models,
        "daily_sessions": daily_sessions,
        "sessions": sessions,
        "daily_tools": daily_tools,
    }
