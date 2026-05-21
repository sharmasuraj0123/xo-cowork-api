"""
OpenClaw-shaped Usage API router.

URL stays ``/openclaw/usage/*`` for backward compatibility (the cowork frontend
and any external pollers depend on it). The body, however, dispatches to the
**active agent's** usage module — resolved by AGENT_NAME via
``services.cowork_agent.usage_loader.load_usage_module()``. With
``AGENT_NAME=openclaw`` the response is what the OpenClaw Control UI "Export
JSON" emits; with another active agent it's that agent's equivalent (file-
shaped methods may return empty, e.g. for hermes — see each module).

No if/elif here. The five endpoints all call into the loader's contract.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from services.cowork_agent.usage_loader import load_usage_module

router = APIRouter(prefix="/openclaw/usage", tags=["openclaw-usage"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_or_501():
    try:
        return load_usage_module()
    except ModuleNotFoundError as e:
        raise HTTPException(
            status_code=501,
            detail=f"no usage module for active agent (tried config.agents.<name>.usage.usage): {e}",
        )


def _gateway_window_ms(days: int) -> tuple[int, int]:
    """Gateway parseDateRange(days=N, mode=gateway): today + (N-1) prior in
    host local TZ, end snapped to end-of-day-1ms.
    """
    local = datetime.now().astimezone()
    today_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int((today_start - timedelta(days=days - 1)).timestamp() * 1000)
    end_ms = int((today_start + timedelta(days=1)).timestamp() * 1000) - 1
    return start_ms, end_ms


def _explicit_window_ms(start: Optional[str], end: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """Explicit ``start=YYYY-MM-DD`` / ``end=YYYY-MM-DD`` in UTC, end snapped to
    end-of-day. Both bounds are independently optional — pass either / both.
    """
    start_ms = None
    end_ms = None
    if start:
        start_ms = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    if end:
        end_ms = int(
            datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000
        ) + 86_400_000
    return start_ms, end_ms


def _date_from_ms(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _collect_entries(mod, agent_id: Optional[str], start_ms: Optional[int], end_ms: Optional[int]):
    """Walk every counted session file via the active module's
    ``get_session_files`` + ``parse_file``. Returns a flat list of entries.
    """
    all_entries: list = []
    for sf in mod.get_session_files(agent_id=agent_id):
        try:
            _, entries = mod.parse_file(sf, start_ms=start_ms, end_ms=end_ms)
        except Exception:
            continue
        all_entries.extend(entries)
    return all_entries


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/analytics")
async def get_usage_analytics(
    agent_id: Optional[str] = Query(None, description="Sub-agent id to query; defaults to all agents"),
    days: Optional[int] = Query(None, description="Limit to last N days"),
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
):
    """
    Usage analytics dashboard payload: stat cards + per-day cost/tokens +
    per-day messages + per-day performance + per-tool counts + per-model totals.
    """
    from collections import defaultdict

    mod = _load_or_501()
    start_ms, end_ms = _explicit_window_ms(start, end)
    if days and not start_ms:
        start_ms, end_ms = _gateway_window_ms(days)

    all_entries = _collect_entries(mod, agent_id, start_ms, end_ms)

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
                dm["date"] = d
                dm["user"] += 1
                dm["total"] += 1
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
            dc["date"] = d
            dc["tokens"] += tok
            dc["cost"] += cost_val

            dm = daily_msgs[d]
            dm["date"] = d
            dm["assistant"] += 1
            dm["total"] += 1
            dm["toolCalls"] += len(entry.get("toolNames", []))

            if dur and dur > 0:
                daily_perf[d].append(dur)

        for tn in entry.get("toolNames", []):
            tool_counter[tn] += 1
            total_tool_calls += 1

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

    # Zero-fill date axis anchored on midnight(today, local TZ).
    range_days = days or 5
    today_local = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    date_range = [
        (today_local - timedelta(days=range_days - 1 - i)).strftime("%Y-%m-%d")
        for i in range(range_days)
    ]

    cost_and_tokens = []
    messages_list = []
    performance_list = []

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
                    "model": m["model"], "provider": m["provider"], "calls": m["calls"],
                    "tokens": m["tokens"], "cost": round(m["cost"], 6),
                }
                for m in model_map.values()
            ],
            key=lambda m: -m["cost"],
        ),
    }


@router.get("/summary/card")
async def get_usage_summary_card(
    agent_id: Optional[str] = Query(None, description="Sub-agent id; defaults to all"),
    days: int = Query(5, description="Number of days to include (default 5)"),
):
    """Lightweight headline card: totals + per-day cost/tokens/assistant-msgs."""
    from collections import defaultdict

    mod = _load_or_501()
    start_ms, end_ms = _gateway_window_ms(days)

    all_entries = _collect_entries(mod, agent_id, start_ms, end_ms)

    if not all_entries:
        return {"days": days, "totalCost": 0, "totalMessages": 0, "totalTokens": 0, "dailyCost": []}

    daily: dict[str, dict] = defaultdict(lambda: {"date": "", "cost": 0.0, "tokens": 0, "messages": 0})
    total_cost = 0.0
    total_tokens = 0
    total_messages = 0  # legacy semantic: assistant-only count

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


@router.get("/summary")
async def get_usage_summary(
    agent_id: Optional[str] = Query(None, description="Sub-agent id; defaults to all"),
    days: Optional[int] = Query(None, description="Limit to last N days"),
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
):
    """Aggregated SessionCostSummary across all sessions in the window,
    plus per-session sub-summaries in ``sessions[]``."""
    mod = _load_or_501()
    start_ms, end_ms = _explicit_window_ms(start, end)
    if days and not start_ms:
        start_ms, end_ms = _gateway_window_ms(days)

    files = mod.get_session_files(agent_id=agent_id)
    if not files:
        return {"error": "No session files found", "agentId": agent_id}

    all_entries: list = []
    session_summaries: list = []
    for sf in files:
        meta, entries = mod.parse_file(sf, start_ms=start_ms, end_ms=end_ms)
        if entries:
            session_summaries.append(mod.build_summary(meta, entries))
            all_entries.extend(entries)

    if not all_entries:
        return {"error": "No usage data found in the given range", "agentId": agent_id}

    combined = mod.build_summary(
        {"sessionId": "all", "sessionFile": f"{len(files)} files"},
        all_entries,
    )
    combined["sessionCount"] = len(session_summaries)
    combined["sessions"] = session_summaries
    return combined


@router.get("/sessions")
async def get_session_list(
    agent_id: Optional[str] = Query(None, description="Sub-agent id; defaults to all"),
):
    """List every discovered session with basic metadata. ``messageCount`` is
    assistant-only (preserved legacy contract)."""
    import os as _os
    mod = _load_or_501()

    sessions = []
    for sf in mod.get_session_files(agent_id=agent_id):
        meta, entries = mod.parse_file(sf, start_ms=None, end_ms=None)
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

    return {"agentId": agent_id, "count": len(sessions), "sessions": sessions}


@router.get("/sessions/{session_id}")
async def get_session_usage(
    session_id: str,
    agent_id: Optional[str] = Query(None, description="Sub-agent id; defaults to all"),
    start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
):
    """Detailed SessionCostSummary for one session, optionally windowed."""
    import os as _os
    mod = _load_or_501()
    start_ms, end_ms = _explicit_window_ms(start, end)

    for sf in mod.get_session_files(agent_id=agent_id):
        if session_id in _os.path.basename(sf):
            meta, entries = mod.parse_file(sf, start_ms=start_ms, end_ms=end_ms)
            if not entries:
                return {"error": "No usage data found for this session in the given range"}
            return mod.build_summary(meta, entries)

    return {"error": f"Session {session_id} not found", "agentId": agent_id}
