"""
Hermes usage — SQLite reader, dashboard aggregator, and sync aggregator.

Loaded by ``services.cowork_agent.usage_loader.load_usage_module()`` when
``AGENT_NAME=hermes``. Same five-function contract as openclaw/claude_code.

Source: per-profile SQLite databases:
  ~/.hermes/state.db                       (default profile)
  ~/.hermes/profiles/<name>/state.db       (named profiles)

The ``sessions`` table already carries rolled-up tokens, message counts, model,
billing provider, and start time — we read at session granularity rather than
walking every message row. Read-only SQLite access (``sqlite3.connect(...,
mode=ro)``); never writes.

**Pure relocation** of rohini-sp's upstream commit ``ae8e898`` (the
``_hermes_state_dbs()`` helper and the ``# ── Hermes sessions`` block in
routers/cowork_agent/usage.py). The SQL, the cutoff filter, the per-profile
enumeration order, and the choice to use SQL over ``hermes insights``
subprocess are all preserved. No new SQL, no schema migration.

The file-shaped contract methods (``get_session_files``, ``parse_file``,
``build_summary``) are no-ops here because hermes has no per-session JSONL
transcript. Calling ``/openclaw/usage/*`` against an ``AGENT_NAME=hermes``
deployment therefore returns empty — matching upstream which never added
hermes to those endpoints.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Optional

from services.cowork_agent.adapters.hermes.paths import HERMES_DIR


def _resolve_tz(tz: str) -> tzinfo:
    if tz == "utc":
        return timezone.utc
    return datetime.now().astimezone().tzinfo or timezone.utc


def _empty_tokens() -> dict:
    return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}


def _hermes_state_dbs() -> list[tuple[str, Path]]:
    """Yield (profile_name, state.db path) for every hermes profile with a db.

    Default profile lives at HERMES_DIR/state.db (not in profiles/). Named
    profiles live at HERMES_DIR/profiles/<name>/state.db. Missing files are
    silently skipped — a freshly-created profile has no state.db until the
    first chat lands.
    """
    out: list[tuple[str, Path]] = []
    default_db = HERMES_DIR / "state.db"
    if default_db.is_file():
        out.append(("default", default_db))
    profiles_root = HERMES_DIR / "profiles"
    if profiles_root.is_dir():
        for entry in sorted(profiles_root.iterdir()):
            if not entry.is_dir():
                continue
            db = entry / "state.db"
            if db.is_file():
                out.append((entry.name, db))
    return out


def _read_sessions(cutoff_epoch: float) -> list[tuple]:
    rows: list[tuple] = []
    for _profile, db_path in _hermes_state_dbs():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            continue
        try:
            rows.extend(
                conn.execute(
                    "SELECT id, source, model, started_at, title, "
                    "       message_count, input_tokens, output_tokens, "
                    "       cache_read_tokens, cache_write_tokens, "
                    "       reasoning_tokens, "
                    "       billing_provider, estimated_cost_usd, actual_cost_usd "
                    "FROM sessions WHERE started_at >= ?",
                    (cutoff_epoch,),
                ).fetchall()
            )
        except sqlite3.OperationalError:
            # Schema mismatch (older/newer hermes) — skip rather than 500.
            pass
        finally:
            conn.close()
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Contract — file-shaped methods (/openclaw/usage/*)
# Hermes has no per-session JSONL; return empty. Upstream never wired
# /openclaw/usage/* to hermes data, so this is a no-regression stub.
# ─────────────────────────────────────────────────────────────────────────────


def get_session_files(*, agent_id: Optional[str] = None) -> list[str]:
    return []


def parse_file(
    path: str,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> tuple[dict, list]:
    return {}, []


def build_summary(meta: dict, entries: list) -> dict:
    return {
        "sessionId": None, "sessionFile": None,
        "firstActivity": None, "lastActivity": None, "durationMs": None,
        "activityDates": [],
        "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
        "totalCost": 0.0, "inputCost": 0.0, "outputCost": 0.0,
        "cacheReadCost": 0.0, "cacheWriteCost": 0.0, "missingCostEntries": 0,
        "dailyBreakdown": [], "dailyMessageCounts": [], "dailyLatency": [],
        "dailyModelUsage": [],
        "messageCounts": {"total": 0, "user": 0, "assistant": 0,
                          "toolCalls": 0, "toolResults": 0, "errors": 0},
        "toolUsage": {"totalCalls": 0, "uniqueTools": 0, "tools": []},
        "modelUsage": [], "latency": {"count": 0, "avgMs": 0, "p95Ms": 0, "minMs": 0, "maxMs": 0},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Contract — aggregate_for_dashboard (/api/usage)
# Relocated from routers/cowork_agent/usage.py:358-455.
# ─────────────────────────────────────────────────────────────────────────────


def aggregate_for_dashboard(*, days: int = 30, tz: str = "local") -> dict:
    days = max(1, min(days, 365))
    z = _resolve_tz(tz)
    today = datetime.now(z).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=days - 1)
    cutoff_epoch = cutoff.timestamp()

    total_tokens = _empty_tokens()
    total_cost = 0.0
    assistant_messages = 0
    user_messages = 0
    session_ids: set[str] = set()
    by_day: dict[str, dict] = {}
    by_model_key: dict[tuple[str, str], dict] = {}
    session_stats: dict[str, dict] = {}

    for row in _read_sessions(cutoff_epoch):
        (sid, _source, model, started_at, title,
         msg_count, inp, out, cache_r, cache_w, reasoning,
         provider, est_cost, act_cost) = row

        inp = int(inp or 0)
        out = int(out or 0)
        cache_r = int(cache_r or 0)
        cache_w = int(cache_w or 0)
        reasoning = int(reasoning or 0)
        msg_count = int(msg_count or 0)
        # Prefer actual cost over estimate; both may be NULL for models
        # the hermes pricing lookup doesn't know yet.
        cost_val = float(act_cost if act_cost is not None else (est_cost or 0))

        total_tokens["input"] += inp
        total_tokens["output"] += out
        total_tokens["cache_read"] += cache_r
        total_tokens["cache_write"] += cache_w
        total_tokens["reasoning"] += reasoning
        total_cost += cost_val
        session_ids.add(sid)
        # We don't split user/assistant here — total_messages is just
        # assistant_messages + user_messages in the final response, so
        # parking the rollup in assistant_messages preserves the total.
        assistant_messages += msg_count

        rt = datetime.fromtimestamp(started_at, tz=timezone.utc)
        day_key = rt.date().isoformat()
        day = by_day.setdefault(
            day_key,
            {"date": day_key, "cost": 0.0, "tokens": 0, "messages": 0},
        )
        day["cost"] += cost_val
        day["tokens"] += inp + out
        day["messages"] += msg_count

        model_id = model or "unknown"
        provider_id = provider or "hermes"
        mk = (model_id, provider_id)
        m = by_model_key.setdefault(mk, {
            "model_id": model_id, "provider_id": provider_id,
            "total_cost": 0.0, "total_tokens": _empty_tokens(), "message_count": 0,
        })
        m["total_cost"] += cost_val
        m["total_tokens"]["input"] += inp
        m["total_tokens"]["output"] += out
        m["total_tokens"]["cache_read"] += cache_r
        m["total_tokens"]["cache_write"] += cache_w
        m["total_tokens"]["reasoning"] += reasoning
        m["message_count"] += msg_count

        if msg_count > 0:
            session_stats[sid] = {
                "session_id": sid,
                "title": (title or "Untitled Session")[:80],
                "total_cost": cost_val,
                "total_tokens": inp + out,
                "message_count": msg_count,
                "time_created": rt.isoformat(),
            }

    daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).date().isoformat()
        daily.append(by_day.get(d, {"date": d, "cost": 0.0, "tokens": 0, "messages": 0}))

    by_model = sorted(by_model_key.values(), key=lambda m: m["total_cost"], reverse=True)
    by_session = sorted(session_stats.values(), key=lambda s: s["total_tokens"], reverse=True)[:10]

    total_sessions = len(session_ids)
    flat_tokens = total_tokens["input"] + total_tokens["output"] + total_tokens["reasoning"]
    avg_tokens_per_session = flat_tokens / total_sessions if total_sessions else 0

    return {
        "total_cost": round(total_cost, 6),
        "total_tokens": total_tokens,
        "total_sessions": total_sessions,
        "total_messages": assistant_messages + user_messages,
        "avg_tokens_per_session": round(avg_tokens_per_session, 2),
        "avg_response_time": 0.0,
        "by_model": by_model,
        "by_session": by_session,
        "daily": daily,
        "response_time": {"avg": 0.0, "median": 0.0, "p95": 0.0, "min": 0.0, "max": 0.0, "count": 0},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Contract — aggregate_for_sync
# Upstream ae8e898 did not add hermes to usage_sync. Exposing this for contract
# symmetry; orchestrator can choose whether to ship these rows.
# ─────────────────────────────────────────────────────────────────────────────


def aggregate_for_sync(*, since_date: Optional[str] = None) -> dict:
    # Use a wide window so SQL is the only date filter we need.
    cutoff_epoch = (
        datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        if since_date else 0.0
    )
    rows = _read_sessions(cutoff_epoch)
    if not rows:
        return {"__session_dates__": {}}

    days: dict[str, dict] = defaultdict(lambda: {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
        "total_tokens": 0,
        "total_cost": 0.0,
        "input_cost": 0.0,
        "output_cost": 0.0,
        "cache_read_cost": 0.0,
        "cache_write_cost": 0.0,
        "total_messages": 0,
        "total_tool_calls": 0,
        "_model_map": {},
        "_tool_counter": defaultdict(int),
    })
    session_dates: dict[str, set] = defaultdict(set)

    for row in rows:
        (sid, _source, model, started_at, _title,
         msg_count, inp, out, cache_r, cache_w, _reasoning,
         provider, est_cost, act_cost) = row

        inp = int(inp or 0)
        out = int(out or 0)
        cache_r = int(cache_r or 0)
        cache_w = int(cache_w or 0)
        msg_count = int(msg_count or 0)
        cost_val = float(act_cost if act_cost is not None else (est_cost or 0))
        tok = inp + out + cache_r + cache_w

        date_str = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y-%m-%d")
        d = days[date_str]
        session_dates[date_str].add(sid)

        d["total_input_tokens"] += inp
        d["total_output_tokens"] += out
        d["total_cache_read_tokens"] += cache_r
        d["total_cache_write_tokens"] += cache_w
        d["total_tokens"] += tok
        d["total_cost"] += cost_val
        d["total_messages"] += msg_count

        mkey = f"{provider or 'hermes'}|{model or 'unknown'}"
        mm = d["_model_map"].setdefault(mkey, {
            "provider": provider or "hermes",
            "model": model or "unknown",
            "calls": 0, "tokens": 0, "cost": 0.0,
        })
        mm["calls"] += msg_count
        mm["tokens"] += tok
        mm["cost"] += cost_val

    result: dict = {}
    for date_str, d in days.items():
        result[date_str] = {
            "report_date": date_str,
            "total_input_tokens": d["total_input_tokens"],
            "total_output_tokens": d["total_output_tokens"],
            "total_cache_read_tokens": d["total_cache_read_tokens"],
            "total_cache_write_tokens": d["total_cache_write_tokens"],
            "total_tokens": d["total_tokens"],
            "total_cost": round(d["total_cost"], 8),
            "input_cost": 0.0,
            "output_cost": 0.0,
            "cache_read_cost": 0.0,
            "cache_write_cost": 0.0,
            "total_messages": d["total_messages"],
            "total_sessions": 0,
            "total_tool_calls": 0,
            "model_usage": list(d["_model_map"].values()),
            "tool_usage": [],
        }
    result["__session_dates__"] = {k: len(v) for k, v in session_dates.items()}
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Contract — canonical view methods (post-restructure surface).
# Hermes synthesizes views from session-row SQL — no per-message data, so
# tool/error counts and per-call latency are always zero.
# ─────────────────────────────────────────────────────────────────────────────


def _window_to_ms(window: dict | None) -> tuple[float, float, int]:
    """Resolve a unified window dict to (cutoff_epoch_secs, until_epoch_secs, range_days)."""
    if not window:
        return 0.0, float("inf"), 0
    if "start" in window or "end" in window:
        s = window.get("start"); e = window.get("end")
        cutoff = (
            datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            if s else 0.0
        )
        until = (
            datetime.strptime(e, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() + 86400
            if e else float("inf")
        )
        if s and e:
            range_days = max(1, int((until - cutoff) // 86400))
        else:
            range_days = 0
        return cutoff, until, range_days
    if "days" in window:
        days = int(window["days"])
        tz = window.get("tz", "local")
        z = _resolve_tz(tz)
        today_start = datetime.now(z).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = (today_start - timedelta(days=days - 1)).timestamp()
        until = (today_start + timedelta(days=1)).timestamp()
        return cutoff, until, days
    return 0.0, float("inf"), 0


def _rows_in_window(window: dict | None) -> list[tuple]:
    cutoff, until, _ = _window_to_ms(window)
    rows = _read_sessions(cutoff)
    if until != float("inf"):
        rows = [r for r in rows if r[3] <= until]  # r[3] = started_at
    return rows


# ── dashboard ────────────────────────────────────────────────────────────────


def dashboard(*, window: dict) -> dict:
    if "days" in window:
        return aggregate_for_dashboard(days=int(window["days"]), tz=window.get("tz", "local"))
    _, _, derived_days = _window_to_ms(window)
    return aggregate_for_dashboard(days=derived_days or 30, tz="local")


# ── analytics ────────────────────────────────────────────────────────────────


def analytics(*, window: dict) -> dict:
    """Time-series dashboard payload — synthesized from session-row SQL.
    Per-call latency / tool counts unavailable from hermes schema (always 0)."""
    from collections import defaultdict

    cutoff, until, range_days = _window_to_ms(window)
    range_days = range_days or 5
    rows = _rows_in_window(window)

    empty = {
        "stats": {"totalCost": 0, "totalTokens": 0, "totalMessages": 0, "avgLatencyMs": 0},
        "costAndTokens": [], "messages": [], "performance": [],
        "toolUsage": {"totalCalls": 0, "uniqueTools": 0, "tools": []},
        "modelUsage": [],
    }
    if not rows:
        return empty

    total_cost = 0.0
    total_tokens = 0
    total_messages = 0

    daily_cost: dict[str, dict] = defaultdict(lambda: {"date": "", "tokens": 0, "cost": 0.0})
    daily_msgs: dict[str, dict] = defaultdict(
        lambda: {"date": "", "total": 0, "user": 0, "assistant": 0, "toolCalls": 0}
    )
    model_map: dict[str, dict] = {}

    for row in rows:
        (_sid, _src, model, started_at, _title,
         msg_count, inp, out, cache_r, cache_w, reasoning,
         provider, est_cost, act_cost) = row
        inp = int(inp or 0); out = int(out or 0)
        cache_r = int(cache_r or 0); cache_w = int(cache_w or 0)
        reasoning = int(reasoning or 0); msg_count = int(msg_count or 0)
        cost_val = float(act_cost if act_cost is not None else (est_cost or 0))
        tok = inp + out + cache_r + cache_w + reasoning

        total_cost += cost_val
        total_tokens += tok
        total_messages += msg_count

        rt = datetime.fromtimestamp(started_at, tz=timezone.utc)
        d = rt.strftime("%Y-%m-%d")
        dc = daily_cost[d]
        dc["date"] = d; dc["tokens"] += tok; dc["cost"] += cost_val
        dm = daily_msgs[d]
        dm["date"] = d; dm["assistant"] += msg_count; dm["total"] += msg_count

        mkey = f"{provider or 'hermes'}|{model or 'unknown'}"
        if mkey not in model_map:
            model_map[mkey] = {
                "model": model or "unknown", "provider": provider or "hermes",
                "calls": 0, "tokens": 0, "cost": 0.0,
            }
        mm = model_map[mkey]
        mm["calls"] += msg_count
        mm["tokens"] += tok
        mm["cost"] += cost_val

    today_local = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    date_range = [
        (today_local - timedelta(days=range_days - 1 - i)).strftime("%Y-%m-%d")
        for i in range(range_days)
    ]
    cost_and_tokens, messages_list, performance_list = [], [], []
    for d in date_range:
        if d in daily_cost:
            dc = daily_cost[d]
            cost_and_tokens.append({"date": d, "tokens": dc["tokens"], "cost": round(dc["cost"], 6)})
        else:
            cost_and_tokens.append({"date": d, "tokens": 0, "cost": 0})
        if d in daily_msgs:
            dm = daily_msgs[d]
            messages_list.append({
                "date": d, "total": dm["total"], "user": dm["user"],
                "assistant": dm["assistant"], "toolCalls": 0,
            })
        else:
            messages_list.append({"date": d, "total": 0, "user": 0, "assistant": 0, "toolCalls": 0})
        performance_list.append({"date": d, "avgMs": 0, "p95Ms": 0, "minMs": 0, "maxMs": 0})

    return {
        "stats": {
            "totalCost": round(total_cost, 6), "totalTokens": total_tokens,
            "totalMessages": total_messages, "avgLatencyMs": 0,
        },
        "costAndTokens": cost_and_tokens,
        "messages": messages_list,
        "performance": performance_list,
        "toolUsage": {"totalCalls": 0, "uniqueTools": 0, "tools": []},
        "modelUsage": sorted(
            [{"model": m["model"], "provider": m["provider"], "calls": m["calls"],
              "tokens": m["tokens"], "cost": round(m["cost"], 6)}
             for m in model_map.values()],
            key=lambda m: -m["cost"],
        ),
    }


# ── summary ──────────────────────────────────────────────────────────────────


def _row_to_session_summary(row: tuple) -> dict:
    """Synthesize a SessionCostSummary from one hermes sessions-table row."""
    (sid, _src, model, started_at, title,
     msg_count, inp, out, cache_r, cache_w, reasoning,
     provider, est_cost, act_cost) = row
    inp = int(inp or 0); out = int(out or 0)
    cache_r = int(cache_r or 0); cache_w = int(cache_w or 0)
    reasoning = int(reasoning or 0); msg_count = int(msg_count or 0)
    cost_val = float(act_cost if act_cost is not None else (est_cost or 0))
    tok = inp + out + cache_r + cache_w + reasoning
    ts_ms = int(started_at * 1000)
    date_str = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y-%m-%d")
    return {
        "sessionId": sid,
        "sessionFile": None,
        "firstActivity": ts_ms,
        "lastActivity": ts_ms,
        "durationMs": 0,
        "activityDates": [date_str],
        "input": inp, "output": out, "cacheRead": cache_r, "cacheWrite": cache_w,
        "totalTokens": tok,
        "totalCost": round(cost_val, 6),
        "inputCost": 0.0, "outputCost": 0.0, "cacheReadCost": 0.0, "cacheWriteCost": 0.0,
        "missingCostEntries": 0,
        "dailyBreakdown": [{"date": date_str, "tokens": tok, "cost": round(cost_val, 6)}],
        "dailyMessageCounts": [{
            "date": date_str, "total": msg_count, "user": 0, "assistant": msg_count,
            "toolCalls": 0, "toolResults": 0, "errors": 0,
        }],
        "dailyLatency": [],
        "dailyModelUsage": [{
            "date": date_str, "provider": provider or "hermes",
            "model": model or "unknown", "tokens": tok,
            "cost": round(cost_val, 6), "count": msg_count,
        }],
        "messageCounts": {
            "total": msg_count, "user": 0, "assistant": msg_count,
            "toolCalls": 0, "toolResults": 0, "errors": 0,
        },
        "toolUsage": {"totalCalls": 0, "uniqueTools": 0, "tools": []},
        "modelUsage": [{
            "provider": provider or "hermes", "model": model or "unknown",
            "count": msg_count,
            "totals": {
                "input": inp, "output": out, "cacheRead": cache_r, "cacheWrite": cache_w,
                "totalTokens": tok, "totalCost": round(cost_val, 6),
                "inputCost": 0, "outputCost": 0, "cacheReadCost": 0, "cacheWriteCost": 0,
                "missingCostEntries": 0,
            },
        }],
        "latency": {"count": 0, "avgMs": 0, "p95Ms": 0, "minMs": 0, "maxMs": 0},
        "_title": title,
    }


def summary(*, window: dict) -> dict:
    rows = _rows_in_window(window)
    if not rows:
        return {"error": "No session data found in the given range"}

    session_summaries = [_row_to_session_summary(r) for r in rows]

    # Aggregate combined totals
    agg = {
        "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
        "totalCost": 0.0, "inputCost": 0.0, "outputCost": 0.0,
        "cacheReadCost": 0.0, "cacheWriteCost": 0.0, "missingCostEntries": 0,
    }
    total_msgs = 0
    for s in session_summaries:
        for k in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
            agg[k] += s[k]
        agg["totalCost"] += s["totalCost"]
        total_msgs += s["messageCounts"]["total"]

    daily_breakdown: dict[str, dict] = {}
    for s in session_summaries:
        for r in s["dailyBreakdown"]:
            row = daily_breakdown.setdefault(r["date"], {"date": r["date"], "tokens": 0, "cost": 0.0})
            row["tokens"] += r["tokens"]; row["cost"] += r["cost"]

    return {
        "sessionId": "all",
        "sessionFile": f"{len(rows)} hermes sessions",
        "firstActivity": min(s["firstActivity"] for s in session_summaries),
        "lastActivity": max(s["lastActivity"] for s in session_summaries),
        "durationMs": 0,
        "activityDates": sorted(set(d for s in session_summaries for d in s["activityDates"])),
        **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in agg.items()},
        "dailyBreakdown": sorted(daily_breakdown.values(), key=lambda d: d["date"]),
        "dailyMessageCounts": [],
        "dailyLatency": [],
        "dailyModelUsage": [],
        "messageCounts": {
            "total": total_msgs, "user": 0, "assistant": total_msgs,
            "toolCalls": 0, "toolResults": 0, "errors": 0,
        },
        "toolUsage": {"totalCalls": 0, "uniqueTools": 0, "tools": []},
        "modelUsage": [],
        "latency": {"count": 0, "avgMs": 0, "p95Ms": 0, "minMs": 0, "maxMs": 0},
        "sessionCount": len(session_summaries),
        "sessions": session_summaries,
    }


# ── summary_card ─────────────────────────────────────────────────────────────


def summary_card(*, window: dict) -> dict:
    from collections import defaultdict
    days = int(window.get("days", 5)) if "days" in window else 5
    rows = _rows_in_window(window)

    if not rows:
        return {"days": days, "totalCost": 0, "totalMessages": 0, "totalTokens": 0, "dailyCost": []}

    daily: dict[str, dict] = defaultdict(lambda: {"date": "", "cost": 0.0, "tokens": 0, "messages": 0})
    total_cost = 0.0; total_tokens = 0; total_messages = 0

    for row in rows:
        (_sid, _src, _model, started_at, _title,
         msg_count, inp, out, cache_r, cache_w, reasoning,
         _provider, est_cost, act_cost) = row
        tok = int(inp or 0) + int(out or 0) + int(cache_r or 0) + int(cache_w or 0) + int(reasoning or 0)
        msg_count = int(msg_count or 0)
        cost_val = float(act_cost if act_cost is not None else (est_cost or 0))
        total_cost += cost_val; total_tokens += tok; total_messages += msg_count

        d = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y-%m-%d")
        row_d = daily[d]
        row_d["date"] = d
        row_d["cost"] += cost_val
        row_d["tokens"] += tok
        row_d["messages"] += msg_count

    today_local = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_list = []
    for i in range(days):
        d = (today_local - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        if d in daily:
            row_d = daily[d]
            daily_list.append({
                "date": row_d["date"], "cost": round(row_d["cost"], 6),
                "tokens": row_d["tokens"], "messages": row_d["messages"],
            })
        else:
            daily_list.append({"date": d, "cost": 0, "tokens": 0, "messages": 0})

    return {
        "days": days,
        "totalCost": round(total_cost, 6),
        "totalMessages": total_messages,
        "totalTokens": total_tokens,
        "dailyCost": daily_list,
    }


# ── list_sessions / get_session ─────────────────────────────────────────────


def list_sessions() -> dict:
    rows = _read_sessions(0)  # all sessions, ever
    sessions = []
    for row in rows:
        (sid, _src, _model, started_at, _title,
         msg_count, inp, out, cache_r, cache_w, reasoning,
         _provider, est_cost, act_cost) = row
        tok = int(inp or 0) + int(out or 0) + int(cache_r or 0) + int(cache_w or 0) + int(reasoning or 0)
        cost_val = float(act_cost if act_cost is not None else (est_cost or 0))
        ts_ms = int(started_at * 1000)
        sessions.append({
            "sessionId": sid,
            "sessionFile": None,
            "messageCount": int(msg_count or 0),
            "totalTokens": tok,
            "totalCost": round(cost_val, 6),
            "firstActivity": ts_ms,
            "lastActivity": ts_ms,
        })
    return {"count": len(sessions), "sessions": sessions}


def get_session(session_id: str, *, window: dict | None = None) -> dict | None:
    rows = _read_sessions(0)
    for row in rows:
        if row[0] == session_id:
            return _row_to_session_summary(row)
    return None


# Canonical sync surface alias.
sync_payload = aggregate_for_sync
