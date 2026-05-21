"""
OpenClaw usage — parser, dashboard aggregator, and sync aggregator.

Loaded by ``services.cowork_agent.usage_loader.load_usage_module()`` when
``AGENT_NAME=openclaw``. Contract (also satisfied by claude_code/hermes):

    get_session_files(*, agent_id=None) -> list[str]
    parse_file(path, *, start_ms, end_ms) -> tuple[meta, entries]
    build_summary(meta, entries) -> dict
    aggregate_for_dashboard(*, days, tz) -> dict       # UsageStats shape
    aggregate_for_sync(*, since_date=None) -> dict[date_str, day_dict]

This module is a relocation of three previously separate sources, with no
counting changes:
- the parser/builder from services/cowork_agent/adapters/openclaw/usage.py
- the openclaw branch of routers/cowork_agent/usage.py (dashboard)
- _aggregate_by_date from services/usage_sync.py (sync)

Plus the Phase-B window-math helper (_gateway_window_ms) so dashboard and
``/openclaw/usage/*`` use the same gateway-equal window for ``days=N``.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Filename filter + tool extraction + parser
# Mirrors openclaw `dist/artifacts-B81-HgBC.js` and `dist/chat-envelope-D39qAHGK.js`.
# ─────────────────────────────────────────────────────────────────────────────

_TOOL_CALL_BLOCK_TYPES = frozenset({"tool_use", "toolcall", "tool_call"})
_TOOL_RESULT_BLOCK_TYPES = frozenset({"tool_result", "tool_result_error"})
_ERROR_STOP_REASONS = frozenset({"error", "aborted", "timeout"})

_ARCHIVE_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:\.\d{3})?Z$")
_SESSIONS_JSON_BAK_RE = re.compile(r"^sessions\.json\.bak\.\d+$")


def _is_usage_counted_session_filename(name: str) -> bool:
    if name == "sessions.json" or _SESSIONS_JSON_BAK_RE.match(name):
        return False
    for tail in (".jsonl.reset.", ".jsonl.deleted.", ".jsonl.bak."):
        idx = name.find(tail)
        if idx > 0:
            ts_part = name[idx + len(tail):]
            if _ARCHIVE_TS_RE.match(ts_part):
                return tail != ".jsonl.bak."
            return False
    return name.endswith(".jsonl")


def _extract_tool_counts(message: dict) -> tuple[list[str], dict]:
    """Mirror gateway extractToolCallNames + countToolResults."""
    names: list[str] = []
    seen: set[str] = set()
    results = {"total": 0, "errors": 0}

    direct = message.get("toolName") or message.get("tool_name")
    if isinstance(direct, str) and direct and direct not in seen:
        seen.add(direct)
        names.append(direct)

    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if not isinstance(btype, str):
                continue
            btype_lc = btype.lower()
            if btype_lc in _TOOL_CALL_BLOCK_TYPES:
                name = block.get("name")
                if isinstance(name, str) and name and name not in seen:
                    seen.add(name)
                    names.append(name)
            elif btype_lc in _TOOL_RESULT_BLOCK_TYPES:
                results["total"] += 1
                if block.get("is_error") is True:
                    results["errors"] += 1

    return names, results


def _agents_dir() -> str:
    return os.getenv("OPENCLAW_AGENTS_DIR", os.path.expanduser("~/.openclaw/agents"))


def _list_agent_ids() -> list[str]:
    base = _agents_dir()
    try:
        return sorted(
            name for name in os.listdir(base)
            if os.path.isdir(os.path.join(base, name))
        )
    except OSError:
        return []


def _discover_one_agent(agent_id: str) -> list[str]:
    sessions_dir = os.path.join(_agents_dir(), agent_id, "sessions")
    try:
        names = os.listdir(sessions_dir)
    except OSError:
        return []
    return sorted(
        os.path.join(sessions_dir, n)
        for n in names
        if _is_usage_counted_session_filename(n)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Window helper (gateway parity)
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_tz(tz: str) -> tzinfo:
    """tz='local' → host local TZ (matches gateway mode=gateway).
    tz='utc' → UTC. Anything else falls back to local."""
    if tz == "utc":
        return timezone.utc
    return datetime.now().astimezone().tzinfo or timezone.utc


def _gateway_window_ms(days: int, tz: str = "local") -> tuple[int, int]:
    """Mirror openclaw parseDateRange(days=N, mode=gateway/utc): today + (N-1)
    prior in the chosen timezone, end snapped to end-of-day-1ms.
    """
    z = _resolve_tz(tz)
    today_start = datetime.now(z).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int((today_start - timedelta(days=days - 1)).timestamp() * 1000)
    end_ms = int((today_start + timedelta(days=1)).timestamp() * 1000) - 1
    return start_ms, end_ms


def _date_from_ms(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────────────
# Contract — file-shaped methods (/openclaw/usage/*)
# ─────────────────────────────────────────────────────────────────────────────


def get_session_files(*, agent_id: Optional[str] = None) -> list[str]:
    """All session transcripts the gateway counts toward usage.

    ``agent_id=None`` iterates every agent subdir under OPENCLAW_AGENTS_DIR
    (matches gateway sessions.usage "all agents" view). Pass an explicit id
    to scope to a single openclaw agent (main, researcher, …).
    """
    if agent_id:
        return _discover_one_agent(agent_id)
    files: list[str] = []
    for aid in _list_agent_ids():
        files.extend(_discover_one_agent(aid))
    return sorted(files)


def parse_file(
    path: str,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> tuple[dict, list]:
    """Parse a single openclaw session JSONL.

    Returns (session_meta, entries) where each entry carries
    ``role="user"|"assistant"``. User entries are minimal; assistant entries
    carry usage/cost/model/stopReason/toolNames/toolResultCounts/durationMs.
    Emitting both roles keeps parity with gateway messageCounts.
    """
    session_meta: dict = {}
    entries: list = []

    with open(path, "r") as f:
        last_user_ts: Optional[float] = None

        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            rtype = record.get("type")
            if rtype == "session":
                session_meta = {
                    "sessionId": record.get("id"),
                    "sessionFile": os.path.basename(path),
                    "startTimestamp": record.get("timestamp"),
                }
                continue
            if rtype != "message":
                continue

            msg = record.get("message", {})
            role = msg.get("role")
            ts_str = record.get("timestamp") or msg.get("timestamp")

            ts_epoch_ms: Optional[int] = None
            if isinstance(ts_str, str):
                try:
                    ts_epoch_ms = int(
                        datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() * 1000
                    )
                except Exception:
                    pass
            elif isinstance(ts_str, (int, float)):
                ts_epoch_ms = int(ts_str) if ts_str > 1e12 else int(ts_str * 1000)

            if ts_epoch_ms:
                if start_ms and ts_epoch_ms < start_ms:
                    continue
                if end_ms and ts_epoch_ms > end_ms:
                    continue

            if role == "user":
                last_user_ts = ts_epoch_ms
                entries.append({"role": "user", "timestamp": ts_epoch_ms})
                continue

            if role != "assistant":
                continue

            usage = msg.get("usage")
            if not usage:
                continue

            tool_names, tool_result_counts = _extract_tool_counts(msg)
            duration_ms = msg.get("durationMs")
            if not (isinstance(duration_ms, (int, float)) and duration_ms > 0):
                duration_ms = (ts_epoch_ms - last_user_ts) if (last_user_ts and ts_epoch_ms) else None

            entries.append({
                "role": "assistant",
                "usage": usage,
                "provider": msg.get("provider"),
                "model": msg.get("model"),
                "timestamp": ts_epoch_ms,
                "stopReason": msg.get("stopReason"),
                "toolNames": tool_names,
                "toolResultCounts": tool_result_counts,
                "durationMs": duration_ms,
            })

    return session_meta, entries


def build_summary(session_meta: dict, entries: list) -> dict:
    """SessionCostSummary-compatible dict matching openclaw Export JSON."""
    total_input = total_output = total_cache_read = total_cache_write = total_tokens = 0
    total_cost = input_cost = output_cost = cache_read_cost = cache_write_cost = 0.0
    missing_cost = 0

    daily_usage: dict = defaultdict(lambda: {"date": "", "tokens": 0, "cost": 0.0})
    daily_messages: dict = defaultdict(
        lambda: {"date": "", "total": 0, "user": 0, "assistant": 0, "toolCalls": 0,
                 "toolResults": 0, "errors": 0}
    )
    daily_latency_buckets: dict = defaultdict(list)
    daily_model_usage: dict = defaultdict(
        lambda: {"date": "", "provider": "", "model": "", "tokens": 0, "cost": 0.0, "count": 0}
    )

    tool_counter: dict = defaultdict(int)
    total_tool_calls = 0
    model_usage_map: dict = {}
    latencies: list = []
    first_activity: Optional[int] = None
    last_activity: Optional[int] = None
    activity_dates: set = set()

    total_user_msgs = 0
    total_assistant_msgs = 0
    total_tool_results = 0
    total_errors = 0

    for entry in entries:
        role = entry.get("role", "assistant")
        ts = entry.get("timestamp")

        if role == "user":
            total_user_msgs += 1
            if ts:
                date_str = _date_from_ms(ts)
                activity_dates.add(date_str)
                if first_activity is None or ts < first_activity:
                    first_activity = ts
                if last_activity is None or ts > last_activity:
                    last_activity = ts
                dm = daily_messages[date_str]
                dm["date"] = date_str
                dm["user"] += 1
                dm["total"] += 1
            continue

        total_assistant_msgs += 1
        usage = entry["usage"]
        cost_obj = usage.get("cost", {})

        inp = usage.get("input", 0)
        out = usage.get("output", 0)
        cr = usage.get("cacheRead", 0)
        cw = usage.get("cacheWrite", 0)
        tok = usage.get("totalTokens", 0) or (inp + out + cr + cw)

        total_input += inp
        total_output += out
        total_cache_read += cr
        total_cache_write += cw
        total_tokens += tok

        if cost_obj:
            total_cost += cost_obj.get("total", 0) or 0
            input_cost += cost_obj.get("input", 0) or 0
            output_cost += cost_obj.get("output", 0) or 0
            cache_read_cost += cost_obj.get("cacheRead", 0) or 0
            cache_write_cost += cost_obj.get("cacheWrite", 0) or 0
        else:
            missing_cost += 1

        for tn in entry.get("toolNames", []):
            tool_counter[tn] += 1
            total_tool_calls += 1

        tr = entry.get("toolResultCounts") or {}
        total_tool_results += int(tr.get("total", 0) or 0)
        msg_errors = int(tr.get("errors", 0) or 0)
        if entry.get("stopReason") in _ERROR_STOP_REASONS:
            msg_errors += 1
        total_errors += msg_errors

        if ts:
            date_str = _date_from_ms(ts)
            activity_dates.add(date_str)
            if first_activity is None or ts < first_activity:
                first_activity = ts
            if last_activity is None or ts > last_activity:
                last_activity = ts

            d = daily_usage[date_str]
            d["date"] = date_str
            d["tokens"] += tok
            d["cost"] += cost_obj.get("total", 0) or 0

            dm = daily_messages[date_str]
            dm["date"] = date_str
            dm["assistant"] += 1
            dm["total"] += 1
            dm["toolCalls"] += len(entry.get("toolNames", []))
            dm["toolResults"] += int(tr.get("total", 0) or 0)
            dm["errors"] += msg_errors

            dur = entry.get("durationMs")
            if dur and dur > 0:
                daily_latency_buckets[date_str].append(dur)
                latencies.append(dur)

            model_key = f"{date_str}|{entry.get('provider', '')}|{entry.get('model', '')}"
            dmu = daily_model_usage[model_key]
            dmu["date"] = date_str
            dmu["provider"] = entry.get("provider", "")
            dmu["model"] = entry.get("model", "")
            dmu["tokens"] += tok
            dmu["cost"] += cost_obj.get("total", 0) or 0
            dmu["count"] += 1

        mkey = f"{entry.get('provider', '')}|{entry.get('model', '')}"
        if mkey not in model_usage_map:
            model_usage_map[mkey] = {
                "provider": entry.get("provider"),
                "model": entry.get("model"),
                "count": 0,
                "totals": {
                    "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                    "totalTokens": 0, "totalCost": 0, "inputCost": 0, "outputCost": 0,
                    "cacheReadCost": 0, "cacheWriteCost": 0, "missingCostEntries": 0,
                },
            }
        mu = model_usage_map[mkey]
        mu["count"] += 1
        mt = mu["totals"]
        mt["input"] += inp
        mt["output"] += out
        mt["cacheRead"] += cr
        mt["cacheWrite"] += cw
        mt["totalTokens"] += tok
        mt["totalCost"] += cost_obj.get("total", 0) or 0
        mt["inputCost"] += cost_obj.get("input", 0) or 0
        mt["outputCost"] += cost_obj.get("output", 0) or 0
        mt["cacheReadCost"] += cost_obj.get("cacheRead", 0) or 0
        mt["cacheWriteCost"] += cost_obj.get("cacheWrite", 0) or 0

    def _latency_stats(vals: list) -> dict:
        if not vals:
            return {"count": 0, "avgMs": 0, "p95Ms": 0, "minMs": 0, "maxMs": 0}
        s = sorted(vals)
        p95_idx = max(0, int(len(s) * 0.95) - 1)
        return {
            "count": len(s),
            "avgMs": round(sum(s) / len(s)),
            "p95Ms": s[p95_idx],
            "minMs": s[0],
            "maxMs": s[-1],
        }

    daily_latency_list = []
    for date_str in sorted(daily_latency_buckets.keys()):
        stats = _latency_stats(daily_latency_buckets[date_str])
        stats["date"] = date_str
        daily_latency_list.append(stats)

    return {
        "sessionId": session_meta.get("sessionId"),
        "sessionFile": session_meta.get("sessionFile"),
        "firstActivity": first_activity,
        "lastActivity": last_activity,
        "durationMs": (last_activity - first_activity) if first_activity and last_activity else None,
        "activityDates": sorted(activity_dates),
        "input": total_input,
        "output": total_output,
        "cacheRead": total_cache_read,
        "cacheWrite": total_cache_write,
        "totalTokens": total_tokens,
        "totalCost": round(total_cost, 6),
        "inputCost": round(input_cost, 6),
        "outputCost": round(output_cost, 6),
        "cacheReadCost": round(cache_read_cost, 6),
        "cacheWriteCost": round(cache_write_cost, 6),
        "missingCostEntries": missing_cost,
        "dailyBreakdown": sorted(daily_usage.values(), key=lambda d: d["date"]),
        "dailyMessageCounts": sorted(daily_messages.values(), key=lambda d: d["date"]),
        "dailyLatency": daily_latency_list,
        "dailyModelUsage": sorted(daily_model_usage.values(), key=lambda d: d["date"]),
        "messageCounts": {
            "total": total_user_msgs + total_assistant_msgs,
            "user": total_user_msgs,
            "assistant": total_assistant_msgs,
            "toolCalls": total_tool_calls,
            "toolResults": total_tool_results,
            "errors": total_errors,
        },
        "toolUsage": {
            "totalCalls": total_tool_calls,
            "uniqueTools": len(tool_counter),
            "tools": sorted(
                [{"name": k, "count": v} for k, v in tool_counter.items()],
                key=lambda t: -t["count"],
            ),
        },
        "modelUsage": list(model_usage_map.values()),
        "latency": _latency_stats(latencies),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Contract — aggregate_for_dashboard (/api/usage)
# Returns the UsageStats shape consumed by the frontend Settings → Usage tab.
# Relocated from routers/cowork_agent/usage.py:openclaw branch.
# ─────────────────────────────────────────────────────────────────────────────


def _empty_tokens() -> dict:
    return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_time(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def aggregate_for_dashboard(*, days: int = 30, tz: str = "local") -> dict:
    """The body of /api/usage minus HTTP plumbing.

    Aggregates across every agent under OPENCLAW_AGENTS_DIR (the all-agents
    view that matches the gateway's sessions.usage). Day buckets honor ``tz``
    (default ``"local"`` for gateway parity); ``"utc"`` is also accepted.
    """
    days = max(1, min(days, 365))
    z = _resolve_tz(tz)
    today = datetime.now(z).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=days - 1)

    total_tokens = _empty_tokens()
    total_cost = 0.0
    assistant_messages = 0
    user_messages = 0
    session_ids: set[str] = set()

    by_day: dict[str, dict] = {}
    by_model_key: dict[tuple[str, str], dict] = {}
    session_stats: dict[str, dict] = {}
    response_times: list[float] = []

    cutoff_ms = int(cutoff.timestamp() * 1000)

    for sf in get_session_files():
        # session_id = base part of filename (handles .reset/.deleted archives)
        name = os.path.basename(sf)
        session_id: Optional[str] = None
        for marker in (".jsonl.reset.", ".jsonl.deleted."):
            idx = name.find(marker)
            if idx > 0:
                session_id = name[:idx]
                break
        if session_id is None:
            if name.endswith(".jsonl") and ".checkpoint." not in name:
                session_id = name[:-len(".jsonl")]
        if session_id is None:
            continue

        # Gateway parity: empty trajectory shells (e.g. .trajectory.jsonl.deleted.<ISO>
        # files with only session.started/session.ended events) still count as a
        # discovered session if mtime is within the window. They contribute 0 to
        # tokens/cost/messages but +1 to total_sessions. Without this we report
        # 27 sessions where the gateway reports 29 on the same data.
        try:
            if os.path.getmtime(sf) * 1000 >= cutoff_ms:
                session_ids.add(session_id)
        except OSError:
            pass

        try:
            with open(sf, "r") as f:
                records = [json.loads(line) for line in f if line.strip()]
        except Exception:
            continue

        session_title: Optional[str] = None
        first_user_ts: Optional[str] = None
        session_entry = {
            "session_id": session_id,
            "title": "Untitled Session",
            "total_cost": 0.0,
            "total_tokens": 0,
            "message_count": 0,
            "time_created": None,
        }
        last_user_time: Optional[datetime] = None

        for record in records:
            if record.get("type") != "message":
                continue
            msg = record.get("message", {})
            role = msg.get("role")
            rt = _record_time(record.get("timestamp"))
            if rt is None or rt < cutoff:
                continue

            if role == "user":
                user_messages += 1
                session_ids.add(session_id)
                last_user_time = rt
                if session_title is None:
                    for block in msg.get("content", []):
                        if block.get("type") == "text":
                            text = block["text"].strip()
                            if text and not text.startswith("Read HEARTBEAT.md"):
                                session_title = text[:80] + ("..." if len(text) > 80 else "")
                                break
                if first_user_ts is None:
                    first_user_ts = record.get("timestamp")
                continue

            if role != "assistant":
                continue

            usage_data = msg.get("usage") or {}
            if not usage_data:
                continue

            inp = int(usage_data.get("input", 0) or 0)
            out = int(usage_data.get("output", 0) or 0)
            cache_r = int(usage_data.get("cacheRead", 0) or 0)
            cache_w = int(usage_data.get("cacheWrite", 0) or 0)
            cost_raw = usage_data.get("cost", 0)
            cost_val = float(cost_raw.get("total") or 0) if isinstance(cost_raw, dict) else float(cost_raw or 0)

            total_tokens["input"] += inp
            total_tokens["output"] += out
            total_tokens["cache_read"] += cache_r
            total_tokens["cache_write"] += cache_w
            total_cost += cost_val
            assistant_messages += 1
            session_ids.add(session_id)

            if last_user_time is not None:
                delta = (rt - last_user_time).total_seconds()
                if 0 <= delta <= 600:
                    response_times.append(delta)
                last_user_time = None

            day_key = rt.date().isoformat()
            day = by_day.setdefault(day_key, {"date": day_key, "cost": 0.0, "tokens": 0, "messages": 0})
            day["cost"] += cost_val
            day["tokens"] += inp + out
            day["messages"] += 1

            model_id = msg.get("model") or "unknown"
            provider_id = msg.get("provider") or ""
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
            m["message_count"] += 1

            session_entry["total_cost"] += cost_val
            session_entry["total_tokens"] += inp + out
            session_entry["message_count"] += 1

        if session_entry["message_count"] > 0:
            if session_title:
                session_entry["title"] = session_title
            session_entry["time_created"] = first_user_ts or _iso_now()
            session_stats[session_id] = session_entry

    daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).date().isoformat()
        daily.append(by_day.get(d, {"date": d, "cost": 0.0, "tokens": 0, "messages": 0}))

    by_model = sorted(by_model_key.values(), key=lambda m: m["total_cost"], reverse=True)
    by_session = sorted(session_stats.values(), key=lambda s: s["total_tokens"], reverse=True)[:10]

    if response_times:
        sorted_rt = sorted(response_times)
        n = len(sorted_rt)
        rt_stats = {
            "avg": sum(sorted_rt) / n,
            "median": sorted_rt[n // 2],
            "p95": sorted_rt[min(n - 1, int(n * 0.95))],
            "min": sorted_rt[0],
            "max": sorted_rt[-1],
            "count": n,
        }
    else:
        rt_stats = {"avg": 0, "median": 0, "p95": 0, "min": 0, "max": 0, "count": 0}

    total_sessions = len(session_ids)
    flat_tokens = total_tokens["input"] + total_tokens["output"] + total_tokens["reasoning"]
    avg_tokens_per_session = flat_tokens / total_sessions if total_sessions else 0

    return {
        "total_cost": round(total_cost, 6),
        "total_tokens": total_tokens,
        "total_sessions": total_sessions,
        "total_messages": assistant_messages + user_messages,
        "avg_tokens_per_session": round(avg_tokens_per_session, 2),
        "avg_response_time": round(rt_stats["avg"], 3),
        "by_model": by_model,
        "by_session": by_session,
        "daily": daily,
        "response_time": {
            "avg": round(rt_stats["avg"], 3),
            "median": round(rt_stats["median"], 3),
            "p95": round(rt_stats["p95"], 3),
            "min": round(rt_stats["min"], 3),
            "max": round(rt_stats["max"], 3),
            "count": rt_stats["count"],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Contract — aggregate_for_sync (daily swarm push)
# Relocated from services/usage_sync.py:_aggregate_by_date + the session-file
# enumeration loop. The router/sync still owns watermark I/O and the HTTP POST;
# this just builds the per-date dict.
# ─────────────────────────────────────────────────────────────────────────────


def aggregate_for_sync(*, since_date: Optional[str] = None) -> dict:
    """Return ``{report_date: per-day dict}`` matching the swarm /usage/report
    payload shape (minus workspace identifiers, which the orchestrator adds).

    Also returns the per-date set of contributing session file indices via the
    sentinel key ``"__session_dates__"`` so the orchestrator can fill
    ``total_sessions`` without re-walking the parser.
    """
    session_files = get_session_files()
    if not session_files:
        return {"__session_dates__": {}}

    all_entries: list = []
    session_dates: dict[str, set] = defaultdict(set)
    parse_errors: list[str] = []

    for sf_idx, sf in enumerate(session_files):
        try:
            _, entries = parse_file(sf)
        except Exception as e:
            parse_errors.append(f"{os.path.basename(sf)}: {e}")
            continue

        if since_date:
            entries = [
                e for e in entries
                if e.get("timestamp") and _date_from_ms(e["timestamp"]) >= since_date
            ]

        for e in entries:
            if e.get("timestamp"):
                session_dates[_date_from_ms(e["timestamp"])].add(sf_idx)

        all_entries.extend(entries)

    if not all_entries:
        return {
            "__session_dates__": {},
            "__parse_errors__": parse_errors,
        }

    days = defaultdict(lambda: {
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

    for entry in all_entries:
        ts = entry.get("timestamp")
        if not ts:
            continue

        date_str = _date_from_ms(ts)
        d = days[date_str]

        # User records still count toward total_messages (matches gateway
        # messageCounts.total = user + assistant) but carry no usage payload.
        if entry.get("role") == "user":
            d["total_messages"] += 1
            continue

        usage = entry.get("usage") or {}
        cost_obj = usage.get("cost", {}) or {}

        inp = usage.get("input", 0) or 0
        out = usage.get("output", 0) or 0
        cr = usage.get("cacheRead", 0) or 0
        cw = usage.get("cacheWrite", 0) or 0
        tok = usage.get("totalTokens", 0) or (inp + out + cr + cw)
        c_total = cost_obj.get("total", 0) or 0

        d["total_input_tokens"] += inp
        d["total_output_tokens"] += out
        d["total_cache_read_tokens"] += cr
        d["total_cache_write_tokens"] += cw
        d["total_tokens"] += tok
        d["total_cost"] += c_total
        d["input_cost"] += cost_obj.get("input", 0) or 0
        d["output_cost"] += cost_obj.get("output", 0) or 0
        d["cache_read_cost"] += cost_obj.get("cacheRead", 0) or 0
        d["cache_write_cost"] += cost_obj.get("cacheWrite", 0) or 0
        d["total_messages"] += 1

        for tn in entry.get("toolNames", []):
            d["_tool_counter"][tn] += 1
            d["total_tool_calls"] += 1

        mkey = f"{entry.get('provider', '')}|{entry.get('model', '')}"
        if mkey not in d["_model_map"]:
            d["_model_map"][mkey] = {
                "provider": entry.get("provider", ""),
                "model": entry.get("model", ""),
                "calls": 0,
                "tokens": 0,
                "cost": 0.0,
            }
        mm = d["_model_map"][mkey]
        mm["calls"] += 1
        mm["tokens"] += tok
        mm["cost"] += c_total

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
            "input_cost": round(d["input_cost"], 8),
            "output_cost": round(d["output_cost"], 8),
            "cache_read_cost": round(d["cache_read_cost"], 8),
            "cache_write_cost": round(d["cache_write_cost"], 8),
            "total_messages": d["total_messages"],
            "total_sessions": 0,  # filled by orchestrator using __session_dates__
            "total_tool_calls": d["total_tool_calls"],
            "model_usage": list(d["_model_map"].values()),
            "tool_usage": [
                {"name": k, "count": v}
                for k, v in sorted(d["_tool_counter"].items(), key=lambda x: -x[1])
            ],
        }
    result["__session_dates__"] = {k: len(v) for k, v in session_dates.items()}
    if parse_errors:
        result["__parse_errors__"] = parse_errors
    return result
