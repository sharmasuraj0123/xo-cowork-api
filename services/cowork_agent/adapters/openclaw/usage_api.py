"""
OpenClaw usage rollup for ``OpenclawAdapter.aggregate_usage``.

Returns the same response shape as ``GET /api/usage`` but populated from
OpenClaw sessions only. Ported from the OpenClaw block of
``routers/cowork_agent/usage.py`` during Phase 4 — the route handler keeps
its own copy for now (both code paths in parallel); Phase 5/6 will
collapse the duplicates once the shared route merges per-adapter
contributions through the dispatcher.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .settings import AGENTS_DIR
from services.cowork_agent.helpers import iso_now, parse_jsonl


def _empty_tokens() -> dict[str, int]:
    return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}


def _openclaw_session_id_from_filename(name: str) -> str | None:
    """Mirror OpenClaw's ``isUsageCountedSessionTranscriptFileName``:
    counts active + ``.reset.<iso>`` + ``.deleted.<iso>`` archives, skips
    ``.bak.<iso>`` and ``*.checkpoint.<uuid>.jsonl``.
    """
    for marker in (".jsonl.reset.", ".jsonl.deleted."):
        idx = name.find(marker)
        if idx > 0:
            return name[:idx]
    if name.endswith(".jsonl") and ".checkpoint." not in name:
        return name[: -len(".jsonl")]
    return None


def _record_time(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def aggregate_openclaw_usage(days: int = 30) -> dict:
    """Return the OpenClaw portion of the usage stats.

    Shape matches the existing ``GET /api/usage`` response — totals,
    by_day, by_model, by_session, response_time, etc. — so this can drop
    in as the OpenClaw contribution when the shared route starts merging
    per-adapter rollups.
    """
    days = max(1, min(days, 365))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    total_tokens = _empty_tokens()
    total_cost = 0.0
    assistant_messages = 0
    user_messages = 0
    session_ids: set[str] = set()

    by_day: dict[str, dict] = {}
    by_model_key: dict[tuple[str, str], dict] = {}
    session_stats: dict[str, dict] = {}
    response_times: list[float] = []

    if AGENTS_DIR.exists():
        for agent_dir in AGENTS_DIR.iterdir():
            if not agent_dir.is_dir():
                continue
            sessions_dir = agent_dir / "sessions"
            if not sessions_dir.is_dir():
                continue

            for session_file in sessions_dir.iterdir():
                if not session_file.is_file():
                    continue
                session_id = _openclaw_session_id_from_filename(session_file.name)
                if session_id is None:
                    continue
                try:
                    records = parse_jsonl(session_file)
                except Exception:
                    continue

                session_title: str | None = None
                first_user_ts: str | None = None
                session_entry = {
                    "session_id": session_id,
                    "title": "Untitled Session",
                    "total_cost": 0.0,
                    "total_tokens": 0,
                    "message_count": 0,
                    "time_created": None,
                }
                last_user_time: datetime | None = None

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
                    cost_val = (
                        float(cost_raw.get("total") or 0)
                        if isinstance(cost_raw, dict)
                        else float(cost_raw or 0)
                    )

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
                    day = by_day.setdefault(
                        day_key,
                        {"date": day_key, "cost": 0.0, "tokens": 0, "messages": 0},
                    )
                    day["cost"] += cost_val
                    day["tokens"] += inp + out
                    day["messages"] += 1

                    model_id = msg.get("model") or "unknown"
                    provider_id = msg.get("provider") or ""
                    mk = (model_id, provider_id)
                    m = by_model_key.setdefault(
                        mk,
                        {
                            "model_id": model_id,
                            "provider_id": provider_id,
                            "total_cost": 0.0,
                            "total_tokens": _empty_tokens(),
                            "message_count": 0,
                        },
                    )
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
                    session_entry["time_created"] = first_user_ts or iso_now()
                    session_stats[session_id] = session_entry

    today = datetime.now(timezone.utc).date()
    daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        daily.append(by_day.get(d, {"date": d, "cost": 0.0, "tokens": 0, "messages": 0}))

    by_model = sorted(by_model_key.values(), key=lambda m: m["total_cost"], reverse=True)
    by_session = sorted(
        session_stats.values(), key=lambda s: s["total_tokens"], reverse=True
    )[:10]

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
