"""
Claude Code usage — parser + dashboard aggregator + sync aggregator.

Loaded by ``services.cowork_agent.usage_loader.load_usage_module()`` when
``AGENT_NAME=claude_code``. Same five-function contract as openclaw.

Source: ``~/.claude/projects/<encoded>/*.jsonl`` — every Anthropic transcript
Claude Code writes. Discovery walks the projects tree directly (NOT filtered
through xo-project ``sessionslist.json`` indices), so the UI shows the full
Claude Code usage the way the ``claude /usage`` CLI does.

Token shape uses Anthropic raw fields: ``input_tokens``, ``output_tokens``,
``cache_read_input_tokens``, ``cache_creation_input_tokens``. Cost is always
0.0 — Anthropic JSONL has no billing field. Records are deduped by
``message.id`` (every streaming chunk shares one id); a token-tuple dedup
historically over-counted by ~75% when tool_result records were interleaved.

A ``type:"user"`` record only counts as a user message if it carries actual
user text — pure tool_result records are protocol noise, not turns.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────


def _projects_root() -> Path:
    return Path(os.getenv("CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects")))


def _resolve_tz(tz: str) -> tzinfo:
    if tz == "utc":
        return timezone.utc
    return datetime.now().astimezone().tzinfo or timezone.utc


def _empty_tokens() -> dict:
    return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}


def _record_time(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_jsonl(path: str) -> list[dict]:
    """Tolerant JSONL reader — skips malformed lines instead of raising,
    so one bad record doesn't poison a whole session.
    """
    out: list[dict] = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _derive_title(records: list[dict]) -> str:
    """First non-trivial user text in the transcript becomes the session title."""
    for r in records:
        if r.get("type") != "user":
            continue
        msg = r.get("message", {}) or {}
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
            if text:
                return text[:80] + ("..." if len(text) > 80 else "")
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        return text[:80] + ("..." if len(text) > 80 else "")
    return "Untitled Session"


# ─────────────────────────────────────────────────────────────────────────────
# File discovery — every JSONL under ~/.claude/projects/<encoded>/
# ─────────────────────────────────────────────────────────────────────────────


def _discover() -> list[str]:
    """Return every ``.jsonl`` file under the projects root, one level deep.

    Matches the ``claude /usage`` enumeration: each project directory holds
    one JSONL per session. No filtering on xo-project membership — we want
    the full picture the UI's Settings → Usage tab needs.
    """
    root = _projects_root()
    if not root.exists() or not root.is_dir():
        return []
    out: list[str] = []
    for proj in sorted(root.iterdir()):
        if not proj.is_dir() or proj.name.startswith("."):
            continue
        for f in sorted(proj.iterdir()):
            if f.is_file() and f.name.endswith(".jsonl"):
                out.append(str(f))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Contract — file-shaped methods (/openclaw/usage/*)
# ─────────────────────────────────────────────────────────────────────────────


def get_session_files(*, agent_id: Optional[str] = None) -> list[str]:
    """Every Claude Code native JSONL on disk.

    ``agent_id`` is accepted for contract symmetry with openclaw but ignored —
    Claude Code is single-tenant per deployment.
    """
    return _discover()


def parse_file(
    path: str,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> tuple[dict, list]:
    """Parse a single Anthropic JSONL transcript.

    Returns ``(meta, entries)`` with entries shaped like openclaw's parser
    output (role=user|assistant, normalized usage keys). Dedups by
    ``message.id`` so streaming records of the same API call count once.
    """
    meta: dict = {"sessionId": None, "sessionFile": os.path.basename(path)}
    entries: list = []
    seen_message_ids: set[str] = set()
    last_user_ts: Optional[int] = None

    records = _read_jsonl(path)

    # Default session id = filename without `.jsonl` (= Anthropic native id).
    base = os.path.basename(path)
    if base.endswith(".jsonl"):
        meta["sessionId"] = base[: -len(".jsonl")]

    for record in records:
        rtype = record.get("type")
        msg = record.get("message", {}) or {}
        ts_str = record.get("timestamp")
        rt = _record_time(ts_str)
        ts_epoch_ms = int(rt.timestamp() * 1000) if rt else None

        if ts_epoch_ms is not None:
            if start_ms and ts_epoch_ms < start_ms:
                continue
            if end_ms and ts_epoch_ms > end_ms:
                continue

        # Prefer record/message-supplied sessionId when present.
        sid = record.get("sessionId") or msg.get("sessionId")
        if isinstance(sid, str) and sid:
            meta["sessionId"] = sid

        if rtype == "user":
            content = msg.get("content", "")
            has_text = (
                (isinstance(content, str) and content.strip()) or
                (isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "text"
                    for b in content
                ))
            )
            if has_text:
                last_user_ts = ts_epoch_ms
                entries.append({"role": "user", "timestamp": ts_epoch_ms})
            continue

        if rtype != "assistant":
            continue

        usage_data = msg.get("usage") or {}
        if not usage_data:
            continue

        msg_id = msg.get("id")
        if isinstance(msg_id, str) and msg_id:
            if msg_id in seen_message_ids:
                continue
            seen_message_ids.add(msg_id)

        inp = int(usage_data.get("input_tokens", 0) or 0)
        out = int(usage_data.get("output_tokens", 0) or 0)
        cache_r = int(usage_data.get("cache_read_input_tokens", 0) or 0)
        cache_w = int(usage_data.get("cache_creation_input_tokens", 0) or 0)

        duration_ms = (ts_epoch_ms - last_user_ts) if (last_user_ts and ts_epoch_ms) else None

        entries.append({
            "role": "assistant",
            # Use openclaw-style keys so build_summary can be shared.
            "usage": {
                "input": inp,
                "output": out,
                "cacheRead": cache_r,
                "cacheWrite": cache_w,
                "totalTokens": inp + out + cache_r + cache_w,
                "cost": {"total": 0.0, "input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0},
            },
            "provider": "anthropic",
            "model": msg.get("model") or "claude",
            "timestamp": ts_epoch_ms,
            "stopReason": msg.get("stop_reason"),
            "toolNames": [],
            "toolResultCounts": {"total": 0, "errors": 0},
            "durationMs": duration_ms,
        })

    return meta, entries


def build_summary(meta: dict, entries: list) -> dict:
    """Reuse openclaw's SessionCostSummary builder — entry shape is aligned.
    Cost fields will all be 0 (Anthropic JSONL has no billing).
    """
    from config.agents.openclaw.usage.usage import build_summary as _openclaw_build
    return _openclaw_build(meta, entries)


# ─────────────────────────────────────────────────────────────────────────────
# Contract — aggregate_for_dashboard (/api/usage)
# Mirrors openclaw's aggregator but over ~/.claude/projects/ files. Always
# returns total_cost=0.0 (no billing in Anthropic JSONL).
# ─────────────────────────────────────────────────────────────────────────────


def aggregate_for_dashboard(*, days: int = 30, tz: str = "local") -> dict:
    days = max(1, min(days, 365))
    z = _resolve_tz(tz)
    today = datetime.now(z).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=days - 1)
    cutoff_ms = int(cutoff.timestamp() * 1000)

    total_tokens = _empty_tokens()
    assistant_messages = 0
    user_messages = 0
    session_ids: set[str] = set()
    by_day: dict[str, dict] = {}
    by_model_key: dict[tuple[str, str], dict] = {}
    session_stats: dict[str, dict] = {}
    response_times: list[float] = []

    for path in _discover():
        # session_id = file basename without ".jsonl" (= Anthropic native id).
        base = os.path.basename(path)
        if not base.endswith(".jsonl"):
            continue
        session_id = base[: -len(".jsonl")]

        # Gateway-style empty-session parity: any file whose mtime falls in
        # the window contributes to total_sessions, even if it has zero
        # messages. (Aligns with the openclaw module's behavior — see
        # docs/openclaw-usage-architecture.md §9.)
        try:
            if os.path.getmtime(path) * 1000 >= cutoff_ms:
                session_ids.add(session_id)
        except OSError:
            pass

        records = _read_jsonl(path)
        if not records:
            continue

        session_entry = {
            "session_id": session_id,
            "title": _derive_title(records),
            "total_cost": 0.0,
            "total_tokens": 0,
            "message_count": 0,
            "time_created": None,
        }
        first_user_ts: Optional[str] = None
        last_user_time: Optional[datetime] = None
        seen_message_ids: set[str] = set()

        for record in records:
            rtype = record.get("type")
            msg = record.get("message", {}) or {}
            if not msg:
                continue
            ts = record.get("timestamp")
            rt = _record_time(ts)
            if rt is None or rt < cutoff:
                continue

            if rtype == "user":
                content = msg.get("content", "")
                has_text = (
                    (isinstance(content, str) and content.strip()) or
                    (isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "text"
                        for b in content
                    ))
                )
                if has_text:
                    user_messages += 1
                    session_ids.add(session_id)
                last_user_time = rt
                if first_user_ts is None:
                    first_user_ts = ts
                continue

            if rtype != "assistant":
                continue

            usage_data = msg.get("usage") or {}
            if not usage_data:
                continue

            msg_id = msg.get("id")
            if isinstance(msg_id, str) and msg_id:
                if msg_id in seen_message_ids:
                    continue
                seen_message_ids.add(msg_id)

            inp = int(usage_data.get("input_tokens", 0) or 0)
            out = int(usage_data.get("output_tokens", 0) or 0)
            cache_r = int(usage_data.get("cache_read_input_tokens", 0) or 0)
            cache_w = int(usage_data.get("cache_creation_input_tokens", 0) or 0)

            total_tokens["input"] += inp
            total_tokens["output"] += out
            total_tokens["cache_read"] += cache_r
            total_tokens["cache_write"] += cache_w
            assistant_messages += 1
            session_ids.add(session_id)

            if last_user_time is not None:
                delta = (rt - last_user_time).total_seconds()
                if 0 <= delta <= 600:
                    response_times.append(delta)
                last_user_time = None

            day_key = rt.date().isoformat()
            day = by_day.setdefault(day_key, {"date": day_key, "cost": 0.0, "tokens": 0, "messages": 0})
            day["tokens"] += inp + out
            day["messages"] += 1

            model_id = msg.get("model") or "claude"
            mk = (model_id, "anthropic")
            m = by_model_key.setdefault(mk, {
                "model_id": model_id, "provider_id": "anthropic",
                "total_cost": 0.0, "total_tokens": _empty_tokens(), "message_count": 0,
            })
            m["total_tokens"]["input"] += inp
            m["total_tokens"]["output"] += out
            m["total_tokens"]["cache_read"] += cache_r
            m["total_tokens"]["cache_write"] += cache_w
            m["message_count"] += 1

            session_entry["total_tokens"] += inp + out
            session_entry["message_count"] += 1

        if session_entry["message_count"] > 0:
            session_entry["time_created"] = first_user_ts or _iso_now()
            session_stats[session_id] = session_entry

    daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).date().isoformat()
        daily.append(by_day.get(d, {"date": d, "cost": 0.0, "tokens": 0, "messages": 0}))

    by_model = sorted(by_model_key.values(), key=lambda m: m["message_count"], reverse=True)
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
        "total_cost": 0.0,
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
# Contract — aggregate_for_sync
# Anthropic JSONL has no cost, so the swarm gets cost=0 rows. Useful for
# token/message accounting even without billing.
# ─────────────────────────────────────────────────────────────────────────────


def _date_from_ms(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def aggregate_for_sync(*, since_date: Optional[str] = None) -> dict:
    """Per-date totals shaped like openclaw's sync payload. ``total_cost`` is
    always 0.0 for claude_code."""
    files = _discover()
    if not files:
        return {"__session_dates__": {}}

    from collections import defaultdict
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
    session_dates: dict[str, set] = defaultdict(set)
    parse_errors: list[str] = []

    for sf_idx, path in enumerate(files):
        try:
            _, entries = parse_file(path)
        except Exception as e:
            parse_errors.append(f"{os.path.basename(path)}: {e}")
            continue

        if since_date:
            entries = [
                e for e in entries
                if e.get("timestamp") and _date_from_ms(e["timestamp"]) >= since_date
            ]
        for e in entries:
            ts = e.get("timestamp")
            if not ts:
                continue
            date_str = _date_from_ms(ts)
            d = days[date_str]
            session_dates[date_str].add(sf_idx)

            if e.get("role") == "user":
                d["total_messages"] += 1
                continue

            usage = e.get("usage") or {}
            inp = usage.get("input", 0) or 0
            out = usage.get("output", 0) or 0
            cr = usage.get("cacheRead", 0) or 0
            cw = usage.get("cacheWrite", 0) or 0
            tok = usage.get("totalTokens", 0) or (inp + out + cr + cw)

            d["total_input_tokens"] += inp
            d["total_output_tokens"] += out
            d["total_cache_read_tokens"] += cr
            d["total_cache_write_tokens"] += cw
            d["total_tokens"] += tok
            d["total_messages"] += 1

            mkey = f"anthropic|{e.get('model', 'claude')}"
            mm = d["_model_map"].setdefault(mkey, {
                "provider": "anthropic",
                "model": e.get("model", "claude"),
                "calls": 0, "tokens": 0, "cost": 0.0,
            })
            mm["calls"] += 1
            mm["tokens"] += tok

    result: dict = {}
    for date_str, d in days.items():
        result[date_str] = {
            "report_date": date_str,
            "total_input_tokens": d["total_input_tokens"],
            "total_output_tokens": d["total_output_tokens"],
            "total_cache_read_tokens": d["total_cache_read_tokens"],
            "total_cache_write_tokens": d["total_cache_write_tokens"],
            "total_tokens": d["total_tokens"],
            "total_cost": 0.0,
            "input_cost": 0.0,
            "output_cost": 0.0,
            "cache_read_cost": 0.0,
            "cache_write_cost": 0.0,
            "total_messages": d["total_messages"],
            "total_sessions": 0,  # filled by orchestrator
            "total_tool_calls": 0,
            "model_usage": list(d["_model_map"].values()),
            "tool_usage": [],
        }
    result["__session_dates__"] = {k: len(v) for k, v in session_dates.items()}
    if parse_errors:
        result["__parse_errors__"] = parse_errors
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Contract — canonical view methods (post-restructure surface).
# Routers under /api/usage/* and /openclaw/usage/* both dispatch into these
# via load_usage_module(). All claude_code-specific view assembly lives here.
# ─────────────────────────────────────────────────────────────────────────────


def _window_to_ms(window: dict | None) -> tuple[Optional[int], Optional[int], int]:
    """Resolve a unified window dict to (start_ms, end_ms, range_days)."""
    if not window:
        return None, None, 0
    if "start" in window or "end" in window:
        s = window.get("start"); e = window.get("end")
        start_ms = (
            int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
            if s else None
        )
        end_ms = (
            int(datetime.strptime(e, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) + 86_400_000
            if e else None
        )
        if start_ms and end_ms:
            range_days = max(1, (end_ms - start_ms) // 86_400_000)
        else:
            range_days = 0
        return start_ms, end_ms, int(range_days)
    if "days" in window:
        days = int(window["days"])
        tz = window.get("tz", "local")
        z = timezone.utc if tz == "utc" else (datetime.now().astimezone().tzinfo or timezone.utc)
        today_start = datetime.now(z).replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int((today_start - timedelta(days=days - 1)).timestamp() * 1000)
        end_ms = int((today_start + timedelta(days=1)).timestamp() * 1000) - 1
        return start_ms, end_ms, days
    return None, None, 0


def _collect_entries(start_ms, end_ms):
    """Iterate every JSONL via parse_file() and flatten entries."""
    all_entries: list = []
    for sf in get_session_files():
        try:
            _, entries = parse_file(sf, start_ms=start_ms, end_ms=end_ms)
        except Exception:
            continue
        all_entries.extend(entries)
    return all_entries


# ── dashboard ────────────────────────────────────────────────────────────────


def dashboard(*, window: dict) -> dict:
    """UsageStats — claude_code's /api/usage payload. Wraps aggregate_for_dashboard."""
    if "days" in window:
        return aggregate_for_dashboard(days=int(window["days"]), tz=window.get("tz", "local"))
    _, _, derived_days = _window_to_ms(window)
    return aggregate_for_dashboard(days=derived_days or 30, tz="local")


# ── analytics ────────────────────────────────────────────────────────────────


def analytics(*, window: dict) -> dict:
    """Time-series dashboard payload: stat cards + per-day cost/tokens +
    per-day messages + per-day performance + per-tool counts + per-model totals.
    Same shape as openclaw's analytics output.
    """
    from collections import defaultdict

    start_ms, end_ms, range_days = _window_to_ms(window)
    range_days = range_days or 5

    all_entries = _collect_entries(start_ms, end_ms)

    empty_response = {
        "stats": {"totalCost": 0, "totalTokens": 0, "totalMessages": 0, "avgLatencyMs": 0},
        "costAndTokens": [],
        "messages": [],
        "performance": [],
        "toolUsage": {"totalCalls": 0, "uniqueTools": 0, "tools": []},
        "modelUsage": [],
    }
    if not all_entries:
        return empty_response

    def _date_from_ms(ms):
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    total_cost = 0.0
    total_tokens = 0
    total_messages = 0
    latencies: list[int] = []
    daily_cost: dict[str, dict] = defaultdict(lambda: {"date": "", "tokens": 0, "cost": 0.0})
    daily_msgs: dict[str, dict] = defaultdict(
        lambda: {"date": "", "total": 0, "user": 0, "assistant": 0, "toolCalls": 0}
    )
    daily_perf: dict[str, list] = defaultdict(list)
    tool_counter: dict[str, int] = defaultdict(int)
    total_tool_calls = 0
    model_map: dict[str, dict] = {}

    for entry in all_entries:
        role = entry.get("role", "assistant")
        ts = entry.get("timestamp")

        if role == "user":
            total_messages += 1
            if ts:
                d = _date_from_ms(ts)
                dm = daily_msgs[d]
                dm["date"] = d; dm["user"] += 1; dm["total"] += 1
            continue

        usage = entry["usage"]
        cost_val = usage.get("cost", {}).get("total", 0) or 0
        tok = usage.get("totalTokens", 0) or 0
        total_cost += cost_val
        total_tokens += tok
        total_messages += 1

        dur = entry.get("durationMs")
        if dur and dur > 0:
            latencies.append(dur)

        if ts:
            d = _date_from_ms(ts)
            dc = daily_cost[d]
            dc["date"] = d; dc["tokens"] += tok; dc["cost"] += cost_val
            dm = daily_msgs[d]
            dm["date"] = d; dm["assistant"] += 1; dm["total"] += 1
            dm["toolCalls"] += len(entry.get("toolNames", []))
            if dur and dur > 0:
                daily_perf[d].append(dur)

        for tn in entry.get("toolNames", []):
            tool_counter[tn] += 1
            total_tool_calls += 1

        mkey = f"{entry.get('provider', '')}|{entry.get('model', '')}"
        if mkey not in model_map:
            model_map[mkey] = {
                "model": entry.get("model", ""), "provider": entry.get("provider", ""),
                "calls": 0, "tokens": 0, "cost": 0.0,
            }
        mm = model_map[mkey]
        mm["calls"] += 1; mm["tokens"] += tok; mm["cost"] += cost_val

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
                "assistant": dm["assistant"], "toolCalls": dm["toolCalls"],
            })
        else:
            messages_list.append({"date": d, "total": 0, "user": 0, "assistant": 0, "toolCalls": 0})
        vals = daily_perf.get(d, [])
        if vals:
            vs = sorted(vals)
            p95_idx = max(0, int(len(vs) * 0.95) - 1)
            performance_list.append({
                "date": d, "avgMs": round(sum(vs) / len(vs)),
                "p95Ms": vs[p95_idx], "minMs": vs[0], "maxMs": vs[-1],
            })
        else:
            performance_list.append({"date": d, "avgMs": 0, "p95Ms": 0, "minMs": 0, "maxMs": 0})

    avg_latency = round(sum(latencies) / len(latencies)) if latencies else 0

    return {
        "stats": {
            "totalCost": round(total_cost, 6), "totalTokens": total_tokens,
            "totalMessages": total_messages, "avgLatencyMs": avg_latency,
        },
        "costAndTokens": cost_and_tokens,
        "messages": messages_list,
        "performance": performance_list,
        "toolUsage": {
            "totalCalls": total_tool_calls, "uniqueTools": len(tool_counter),
            "tools": sorted([{"name": k, "count": v} for k, v in tool_counter.items()],
                            key=lambda t: -t["count"]),
        },
        "modelUsage": sorted([
            {"model": m["model"], "provider": m["provider"], "calls": m["calls"],
             "tokens": m["tokens"], "cost": round(m["cost"], 6)}
            for m in model_map.values()
        ], key=lambda m: -m["cost"]),
    }


def summary(*, window: dict) -> dict:
    """Aggregated SessionCostSummary across every claude_code JSONL."""
    start_ms, end_ms, _ = _window_to_ms(window)
    files = get_session_files()
    if not files:
        return {"error": "No session files found"}

    all_entries: list = []
    session_summaries: list = []
    for sf in files:
        meta, entries = parse_file(sf, start_ms=start_ms, end_ms=end_ms)
        if entries:
            session_summaries.append(build_summary(meta, entries))
            all_entries.extend(entries)

    if not all_entries:
        return {"error": "No usage data found in the given range"}

    combined = build_summary(
        {"sessionId": "all", "sessionFile": f"{len(files)} files"},
        all_entries,
    )
    combined["sessionCount"] = len(session_summaries)
    combined["sessions"] = session_summaries
    return combined


def summary_card(*, window: dict) -> dict:
    from collections import defaultdict
    days = int(window.get("days", 5)) if "days" in window else 5
    start_ms, end_ms, _ = _window_to_ms(window)

    all_entries = _collect_entries(start_ms, end_ms)
    if not all_entries:
        return {"days": days, "totalCost": 0, "totalMessages": 0, "totalTokens": 0, "dailyCost": []}

    def _date_from_ms(ms):
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    daily: dict[str, dict] = defaultdict(lambda: {"date": "", "cost": 0.0, "tokens": 0, "messages": 0})
    total_cost = 0.0
    total_tokens = 0
    total_messages = 0

    for entry in all_entries:
        if entry.get("role") == "user":
            continue
        usage = entry["usage"]
        cost_val = usage.get("cost", {}).get("total", 0) or 0
        tok = usage.get("totalTokens", 0) or 0
        total_cost += cost_val
        total_tokens += tok
        total_messages += 1

        ts = entry.get("timestamp")
        if ts:
            d = _date_from_ms(ts)
            row = daily[d]
            row["date"] = d
            row["cost"] += cost_val
            row["tokens"] += tok
            row["messages"] += 1

    today_local = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_list = []
    for i in range(days):
        d = (today_local - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        if d in daily:
            row = daily[d]
            daily_list.append({
                "date": row["date"], "cost": round(row["cost"], 6),
                "tokens": row["tokens"], "messages": row["messages"],
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


def list_sessions() -> dict:
    """One row per claude_code JSONL. messageCount assistant-only."""
    sessions = []
    for sf in get_session_files():
        meta, entries = parse_file(sf, start_ms=None, end_ms=None)
        if not meta:
            continue
        assistant_entries = [e for e in entries if e.get("role") != "user"]
        total_cost = sum(
            (e["usage"].get("cost", {}).get("total", 0) or 0) for e in assistant_entries
        )
        total_tokens = sum(
            (e["usage"].get("totalTokens", 0) or 0) for e in assistant_entries
        )
        timestamps = [e["timestamp"] for e in entries if e.get("timestamp")]

        sessions.append({
            "sessionId": meta.get("sessionId"),
            "sessionFile": meta.get("sessionFile"),
            "messageCount": len(assistant_entries),
            "totalTokens": total_tokens,
            "totalCost": round(total_cost, 6),
            "firstActivity": min(timestamps) if timestamps else None,
            "lastActivity": max(timestamps) if timestamps else None,
        })

    return {"count": len(sessions), "sessions": sessions}


def get_session(session_id: str, *, window: dict | None = None) -> dict | None:
    """Detailed SessionCostSummary for one claude_code session."""
    start_ms, end_ms, _ = _window_to_ms(window or {})

    for sf in get_session_files():
        if session_id in os.path.basename(sf):
            meta, entries = parse_file(sf, start_ms=start_ms, end_ms=end_ms)
            if not entries:
                return {"error": "No usage data found for this session in the given range"}
            return build_summary(meta, entries)

    return None


# Canonical sync surface alias. Legacy aggregate_for_sync stays as the
# function definition above; Phase 5 swaps which side is the alias.
sync_payload = aggregate_for_sync
