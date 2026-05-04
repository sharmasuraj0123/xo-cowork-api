"""
OpenClaw JSONL session parsing logic.

Moved from routers/openclaw_usage.py so it can be reused across adapters
and tested independently of the HTTP layer.
"""
from __future__ import annotations

import glob
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional


def discover_session_files(
    agent_id: str = "main",
    agents_dir: str | None = None,
) -> list[str]:
    """Find all .jsonl session transcript files for an agent."""
    base = agents_dir or os.getenv(
        "OPENCLAW_AGENTS_DIR",
        os.path.expanduser("~/.openclaw/agents"),
    )
    pattern = os.path.join(base, agent_id, "sessions", "*.jsonl")
    return sorted(glob.glob(pattern))


def parse_session_file(
    path: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> tuple[dict, list]:
    """
    Parse a single session JSONL file.
    Returns (session_meta, assistant_entries).
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
                continue

            if role != "assistant":
                continue

            usage = msg.get("usage")
            if not usage:
                continue

            tool_names = []
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        name = block.get("name")
                        if name:
                            tool_names.append(name)

            duration_ms: Optional[int] = None
            if last_user_ts and ts_epoch_ms:
                duration_ms = ts_epoch_ms - last_user_ts

            entries.append(
                {
                    "usage": usage,
                    "provider": msg.get("provider"),
                    "model": msg.get("model"),
                    "timestamp": ts_epoch_ms,
                    "stopReason": msg.get("stopReason"),
                    "toolNames": tool_names,
                    "durationMs": duration_ms,
                }
            )
            last_user_ts = None

    return session_meta, entries


def _date_from_ms(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def build_session_cost_summary(session_meta: dict, entries: list) -> dict:
    """
    Build a SessionCostSummary-compatible dict from parsed entries.
    Matches the OpenClaw Export JSON schema.
    """
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

    for entry in entries:
        usage = entry["usage"]
        cost_obj = usage.get("cost", {})
        ts = entry.get("timestamp")

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
            dm["total"] += 2
            dm["user"] += 1
            dm["assistant"] += 1
            dm["toolCalls"] += len(entry.get("toolNames", []))

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
            "total": sum(d["total"] for d in daily_messages.values()),
            "user": sum(d["user"] for d in daily_messages.values()),
            "assistant": sum(d["assistant"] for d in daily_messages.values()),
            "toolCalls": total_tool_calls,
            "toolResults": total_tool_calls,
            "errors": 0,
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
