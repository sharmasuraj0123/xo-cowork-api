"""
OpenClaw Usage API Router
Parses OpenClaw session JSONL files to expose usage/cost data for frontend dashboards.
Data format mirrors the OpenClaw Control UI "Export JSON" output.
"""

import json
import glob
import os
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict

from fastapi import APIRouter, Query

from services.cowork_agent.adapters.openclaw.usage import (
    discover_session_files,
    parse_session_file,
    build_session_cost_summary,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OPENCLAW_AGENTS_DIR = os.getenv(
    "OPENCLAW_AGENTS_DIR",
    os.path.expanduser("~/.openclaw/agents"),
)

router = APIRouter(prefix="/openclaw/usage", tags=["openclaw-usage"])


# ---------------------------------------------------------------------------
# Helpers – delegate to adapter module; keep private wrappers for backward compat
# ---------------------------------------------------------------------------


def _discover_session_files(agent_id: str = "main") -> list[str]:
    """Find all .jsonl session transcript files for an agent."""
    return discover_session_files(agent_id, OPENCLAW_AGENTS_DIR)


def _parse_session_file(
    path: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
):
    """Delegate to adapter module."""
    return parse_session_file(path, start_ms, end_ms)


def _date_from_ms(epoch_ms: int) -> str:
    """Convert epoch ms to YYYY-MM-DD string (UTC)."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _build_session_cost_summary(session_meta: dict, entries: list) -> dict:
    """Delegate to adapter module."""
    return build_session_cost_summary(session_meta, entries)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/analytics")
async def get_usage_analytics(
    agent_id: str = Query("main", description="Agent ID to query"),
    days: Optional[int] = Query(None, description="Limit to last N days"),
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
):
    """
    Full Usage Analytics dashboard endpoint.
    Powers: stat cards, Cost & Tokens tab, Messages tab, Performance tab,
    Tool Usage table, and Model Usage table.

    Response shape:
    {
      "stats": { "totalCost", "totalTokens", "totalMessages", "avgLatencyMs" },
      "costAndTokens": [{ "date", "tokens", "cost" }, ...],
      "messages": [{ "date", "total", "user", "assistant", "toolCalls" }, ...],
      "performance": [{ "date", "avgMs", "p95Ms", "minMs", "maxMs" }, ...],
      "toolUsage": { "totalCalls", "uniqueTools", "tools": [{ "name", "count" }] },
      "modelUsage": [{ "model", "provider", "calls", "tokens", "cost" }]
    }
    """
    from datetime import timedelta

    start_ms = None
    end_ms = None

    if start:
        start_ms = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    if end:
        end_ms = int(
            datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000
        ) + 86400_000
    if days and not start_ms:
        now = datetime.now(timezone.utc)
        start_ms = int((now - timedelta(days=days)).timestamp() * 1000)

    session_files = _discover_session_files(agent_id)
    if not session_files:
        return {
            "stats": {"totalCost": 0, "totalTokens": 0, "totalMessages": 0, "avgLatencyMs": 0},
            "costAndTokens": [],
            "messages": [],
            "performance": [],
            "toolUsage": {"totalCalls": 0, "uniqueTools": 0, "tools": []},
            "modelUsage": [],
        }

    all_entries = []
    for sf in session_files:
        _, entries = _parse_session_file(sf, start_ms, end_ms)
        all_entries.extend(entries)

    if not all_entries:
        return {
            "stats": {"totalCost": 0, "totalTokens": 0, "totalMessages": 0, "avgLatencyMs": 0},
            "costAndTokens": [],
            "messages": [],
            "performance": [],
            "toolUsage": {"totalCalls": 0, "uniqueTools": 0, "tools": []},
            "modelUsage": [],
        }

    # ---- Aggregate ----
    total_cost = 0.0
    total_tokens = 0
    total_messages = 0
    latencies: list[int] = []

    # Daily buckets
    daily_cost: dict[str, dict] = defaultdict(lambda: {"date": "", "tokens": 0, "cost": 0.0})
    daily_msgs: dict[str, dict] = defaultdict(
        lambda: {"date": "", "total": 0, "user": 0, "assistant": 0, "toolCalls": 0}
    )
    daily_perf: dict[str, list] = defaultdict(list)

    # Tool usage
    tool_counter: dict[str, int] = defaultdict(int)
    total_tool_calls = 0

    # Model usage
    model_map: dict[str, dict] = {}

    for entry in all_entries:
        usage = entry["usage"]
        cost_val = usage.get("cost", {}).get("total", 0) or 0
        tok = usage.get("totalTokens", 0) or 0

        total_cost += cost_val
        total_tokens += tok
        total_messages += 1

        dur = entry.get("durationMs")
        if dur and dur > 0:
            latencies.append(dur)

        ts = entry.get("timestamp")
        if ts:
            d = _date_from_ms(ts)

            dc = daily_cost[d]
            dc["date"] = d
            dc["tokens"] += tok
            dc["cost"] += cost_val

            dm = daily_msgs[d]
            dm["date"] = d
            dm["total"] += 2  # user + assistant pair
            dm["user"] += 1
            dm["assistant"] += 1
            dm["toolCalls"] += len(entry.get("toolNames", []))

            if dur and dur > 0:
                daily_perf[d].append(dur)

        # Tools
        for tn in entry.get("toolNames", []):
            tool_counter[tn] += 1
            total_tool_calls += 1

        # Models
        mkey = f"{entry.get('provider', '')}|{entry.get('model', '')}"
        if mkey not in model_map:
            model_map[mkey] = {
                "model": entry.get("model", ""),
                "provider": entry.get("provider", ""),
                "calls": 0,
                "tokens": 0,
                "cost": 0.0,
            }
        mm = model_map[mkey]
        mm["calls"] += 1
        mm["tokens"] += tok
        mm["cost"] += cost_val

    # ---- Build sorted daily arrays with zero-fill ----
    # Determine date range to fill
    range_days = days or 5  # default 5 days if not specified
    now = datetime.now(timezone.utc)
    date_range = []
    for i in range(range_days):
        date_range.append((now - timedelta(days=range_days - 1 - i)).strftime("%Y-%m-%d"))

    cost_and_tokens = []
    messages_list = []
    performance_list = []

    for d in date_range:
        # Cost & Tokens
        if d in daily_cost:
            dc = daily_cost[d]
            cost_and_tokens.append({"date": d, "tokens": dc["tokens"], "cost": round(dc["cost"], 6)})
        else:
            cost_and_tokens.append({"date": d, "tokens": 0, "cost": 0})

        # Messages
        if d in daily_msgs:
            dm = daily_msgs[d]
            messages_list.append({
                "date": d,
                "total": dm["total"],
                "user": dm["user"],
                "assistant": dm["assistant"],
                "toolCalls": dm["toolCalls"],
            })
        else:
            messages_list.append({"date": d, "total": 0, "user": 0, "assistant": 0, "toolCalls": 0})

        # Performance
        vals = daily_perf.get(d, [])
        if vals:
            vals_sorted = sorted(vals)
            p95_idx = max(0, int(len(vals_sorted) * 0.95) - 1)
            performance_list.append({
                "date": d,
                "avgMs": round(sum(vals_sorted) / len(vals_sorted)),
                "p95Ms": vals_sorted[p95_idx],
                "minMs": vals_sorted[0],
                "maxMs": vals_sorted[-1],
            })
        else:
            performance_list.append({"date": d, "avgMs": 0, "p95Ms": 0, "minMs": 0, "maxMs": 0})

    # Avg latency for top card
    avg_latency = round(sum(latencies) / len(latencies)) if latencies else 0

    return {
        "stats": {
            "totalCost": round(total_cost, 6),
            "totalTokens": total_tokens,
            "totalMessages": total_messages,
            "avgLatencyMs": avg_latency,
        },
        "costAndTokens": cost_and_tokens,
        "messages": messages_list,
        "performance": performance_list,
        "toolUsage": {
            "totalCalls": total_tool_calls,
            "uniqueTools": len(tool_counter),
            "tools": sorted(
                [{"name": k, "count": v} for k, v in tool_counter.items()],
                key=lambda t: -t["count"],
            ),
        },
        "modelUsage": sorted(
            [
                {
                    "model": m["model"],
                    "provider": m["provider"],
                    "calls": m["calls"],
                    "tokens": m["tokens"],
                    "cost": round(m["cost"], 6),
                }
                for m in model_map.values()
            ],
            key=lambda m: -m["cost"],
        ),
    }


@router.get("/summary/card")
async def get_usage_summary_card(
    agent_id: str = Query("main", description="Agent ID to query"),
    days: int = Query(5, description="Number of days to include (default 5)"),
):
    """
    Lightweight usage summary card — returns only what's needed for the
    "Usage Summary" widget: headline stats + daily cost bars.

    Response shape:
    {
      "days": 5,
      "totalCost": 33.78,
      "totalMessages": 353,
      "totalTokens": 20000000,
      "dailyCost": [
        {"date": "2026-03-13", "cost": 0.12, "tokens": 50000, "messages": 10},
        ...
      ]
    }
    """
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    start_ms = int((now - timedelta(days=days)).timestamp() * 1000)

    session_files = _discover_session_files(agent_id)
    if not session_files:
        return {"days": days, "totalCost": 0, "totalMessages": 0, "totalTokens": 0, "dailyCost": []}

    all_entries = []
    for sf in session_files:
        _, entries = _parse_session_file(sf, start_ms=start_ms)
        all_entries.extend(entries)

    if not all_entries:
        return {"days": days, "totalCost": 0, "totalMessages": 0, "totalTokens": 0, "dailyCost": []}

    # Bucket by date
    daily: dict[str, dict] = defaultdict(lambda: {"date": "", "cost": 0.0, "tokens": 0, "messages": 0})
    total_cost = 0.0
    total_tokens = 0
    total_messages = 0

    for entry in all_entries:
        cost_val = (entry["usage"].get("cost", {}).get("total", 0) or 0)
        tok = entry["usage"].get("totalTokens", 0) or 0
        total_cost += cost_val
        total_tokens += tok
        total_messages += 1  # each entry = 1 assistant response ≈ 1 exchange

        ts = entry.get("timestamp")
        if ts:
            date_str = _date_from_ms(ts)
            d = daily[date_str]
            d["date"] = date_str
            d["cost"] += cost_val
            d["tokens"] += tok
            d["messages"] += 1

    # Fill in missing dates with zeros so the chart has no gaps
    daily_list = []
    for i in range(days):
        date_str = (now - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        if date_str in daily:
            d = daily[date_str]
            daily_list.append({
                "date": d["date"],
                "cost": round(d["cost"], 6),
                "tokens": d["tokens"],
                "messages": d["messages"],
            })
        else:
            daily_list.append({"date": date_str, "cost": 0, "tokens": 0, "messages": 0})

    return {
        "days": days,
        "totalCost": round(total_cost, 6),
        "totalMessages": total_messages,
        "totalTokens": total_tokens,
        "dailyCost": daily_list,
    }


@router.get("/summary")
async def get_usage_summary(
    agent_id: str = Query("main", description="Agent ID to query"),
    days: Optional[int] = Query(None, description="Limit to last N days"),
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
):
    """
    Aggregated usage summary across all sessions.
    Returns the same schema as the OpenClaw Control UI "Export JSON" button.

    This is your main endpoint — it has everything:
    totals, daily breakdowns, message counts, tool usage, model usage, and latency.
    """
    start_ms = None
    end_ms = None

    if start:
        start_ms = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    if end:
        end_ms = int(
            datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000
        ) + 86400_000  # include the end date fully

    if days and not start_ms:
        now = datetime.now(timezone.utc)
        start_ms = int((now.timestamp() - days * 86400) * 1000)

    session_files = _discover_session_files(agent_id)
    if not session_files:
        return {"error": "No session files found", "agentId": agent_id}

    # Aggregate across all sessions
    all_entries = []
    session_summaries = []

    for sf in session_files:
        meta, entries = _parse_session_file(sf, start_ms, end_ms)
        if entries:
            summary = _build_session_cost_summary(meta, entries)
            session_summaries.append(summary)
            all_entries.extend(entries)

    if not all_entries:
        return {"error": "No usage data found in the given range", "agentId": agent_id}

    # Build a combined summary from all entries
    combined_meta = {
        "sessionId": "all",
        "sessionFile": f"{len(session_files)} files",
    }
    combined = _build_session_cost_summary(combined_meta, all_entries)
    combined["sessionCount"] = len(session_summaries)
    combined["sessions"] = session_summaries

    return combined


@router.get("/sessions")
async def get_session_list(
    agent_id: str = Query("main", description="Agent ID to query"),
):
    """
    List all discovered sessions with basic metadata.
    Use a session ID from this list to query /sessions/{session_id} for details.
    """
    session_files = _discover_session_files(agent_id)
    sessions = []

    for sf in session_files:
        meta, entries = _parse_session_file(sf)
        if not meta:
            continue

        total_cost = sum(
            (e["usage"].get("cost", {}).get("total", 0) or 0) for e in entries
        )
        total_tokens = sum(
            (e["usage"].get("totalTokens", 0) or 0) for e in entries
        )
        timestamps = [e["timestamp"] for e in entries if e.get("timestamp")]

        sessions.append(
            {
                "sessionId": meta.get("sessionId"),
                "sessionFile": meta.get("sessionFile"),
                "messageCount": len(entries),
                "totalTokens": total_tokens,
                "totalCost": round(total_cost, 6),
                "firstActivity": min(timestamps) if timestamps else None,
                "lastActivity": max(timestamps) if timestamps else None,
            }
        )

    return {"agentId": agent_id, "count": len(sessions), "sessions": sessions}


@router.get("/sessions/{session_id}")
async def get_session_usage(
    session_id: str,
    agent_id: str = Query("main", description="Agent ID to query"),
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
):
    """
    Detailed usage for a specific session.
    Returns the full SessionCostSummary matching the OpenClaw Export JSON format.
    """
    start_ms = None
    end_ms = None

    if start:
        start_ms = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    if end:
        end_ms = int(
            datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000
        ) + 86400_000

    session_files = _discover_session_files(agent_id)

    for sf in session_files:
        if session_id in os.path.basename(sf):
            meta, entries = _parse_session_file(sf, start_ms, end_ms)
            if not entries:
                return {"error": "No usage data found for this session in the given range"}
            return _build_session_cost_summary(meta, entries)

    return {"error": f"Session {session_id} not found", "agentId": agent_id}
