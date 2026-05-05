"""
Aggregated usage statistics across all agent sessions.

Sources:
  - ~/.openclaw/agents/*/sessions/*.jsonl  (OpenClaw, camelCase usage fields)
  - ~/.claude/projects/*/*.jsonl           (native Claude CLI, snake_case usage fields)

Returns the `UsageStats` shape defined in the frontend (`src/types/usage.ts`).
"""

import glob
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter

from services.cowork_agent.settings import AGENTS_DIR
from services.cowork_agent.helpers import iso_now, parse_jsonl

_CLAUDE_NATIVE_PROJECTS_DIR = Path(os.path.expanduser(
    os.getenv("CLAUDE_NATIVE_PROJECTS_DIR", "~/.claude/projects")
))

router = APIRouter()


@router.get("/api/usage")
def usage(days: int = 30):
    """
    Aggregate OpenClaw usage across all agents/sessions within the last `days`.
    Walks ~/.openclaw/agents/*/sessions/*.jsonl and sums assistant message usage.
    Returns the UsageStats shape expected by the frontend (src/types/usage.ts).
    """
    days = max(1, min(days, 365))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    total_tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
    total_cost = 0.0
    assistant_messages = 0
    user_messages = 0
    session_ids: set[str] = set()

    by_day: dict[str, dict] = {}
    # (model_id, provider_id) -> ModelUsage dict
    by_model_key: dict[tuple[str, str], dict] = {}
    # session_id -> SessionUsage dict
    session_stats: dict[str, dict] = {}
    # response-time samples: user→assistant latency in seconds
    response_times: list[float] = []

    def _empty_tokens():
        return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}

    def _scan_root(root_dir):
        nonlocal total_cost, assistant_messages, user_messages
        if not root_dir.exists():
            return
        for agent_dir in root_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            sessions_dir = agent_dir / "sessions"
            if not sessions_dir.is_dir():
                continue

            for session_file in sessions_dir.glob("*.jsonl"):
                session_id = session_file.stem
                if session_id in session_stats:
                    continue  # already counted from the other root
                try:
                    records = parse_jsonl(session_file)
                except Exception:
                    continue

                session_title: str | None = None
                first_user_time_created: str | None = None
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
                    ts = record.get("timestamp")
                    try:
                        record_time = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
                    except Exception:
                        record_time = None
                    if record_time is None or record_time < cutoff:
                        continue

                    if role == "user":
                        user_messages += 1
                        session_ids.add(session_id)
                        last_user_time = record_time
                        if session_title is None:
                            for block in msg.get("content", []):
                                if block.get("type") == "text":
                                    text = block["text"].strip()
                                    if text and not text.startswith("Read HEARTBEAT.md"):
                                        session_title = text[:80] + ("..." if len(text) > 80 else "")
                                        break
                        if first_user_time_created is None:
                            first_user_time_created = ts
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
                    if isinstance(cost_raw, dict):
                        cost_val = float(cost_raw.get("total") or 0)
                    else:
                        cost_val = float(cost_raw or 0)

                    total_tokens["input"] += inp
                    total_tokens["output"] += out
                    total_tokens["cache_read"] += cache_r
                    total_tokens["cache_write"] += cache_w
                    total_cost += cost_val
                    assistant_messages += 1
                    session_ids.add(session_id)

                    # response-time: seconds between last user message and this assistant reply
                    if last_user_time is not None:
                        delta = (record_time - last_user_time).total_seconds()
                        if 0 <= delta <= 600:  # sanity cap at 10 min
                            response_times.append(delta)
                        last_user_time = None

                    day_key = record_time.date().isoformat()
                    day = by_day.setdefault(day_key, {
                        "date": day_key, "cost": 0.0, "tokens": 0, "messages": 0,
                    })
                    day["cost"] += cost_val
                    day["tokens"] += inp + out + cache_r + cache_w
                    day["messages"] += 1

                    model_id = msg.get("model") or "unknown"
                    provider_id = msg.get("provider") or ""
                    mk = (model_id, provider_id)
                    m = by_model_key.setdefault(mk, {
                        "model_id": model_id,
                        "provider_id": provider_id,
                        "total_cost": 0.0,
                        "total_tokens": _empty_tokens(),
                        "message_count": 0,
                    })
                    m["total_cost"] += cost_val
                    m["total_tokens"]["input"] += inp
                    m["total_tokens"]["output"] += out
                    m["total_tokens"]["cache_read"] += cache_r
                    m["total_tokens"]["cache_write"] += cache_w
                    m["message_count"] += 1

                    session_entry["total_cost"] += cost_val
                    session_entry["total_tokens"] += inp + out + cache_r + cache_w
                    session_entry["message_count"] += 1

                if session_entry["message_count"] > 0:
                    if session_title:
                        session_entry["title"] = session_title
                    session_entry["time_created"] = first_user_time_created or iso_now()
                    session_stats[session_id] = session_entry

    def _scan_native_cli_dir(native_dir: Path):
        nonlocal total_cost, assistant_messages, user_messages
        if not native_dir.exists():
            return
        for project_dir in native_dir.iterdir():
            if not project_dir.is_dir():
                continue
            for session_file in project_dir.glob("*.jsonl"):
                session_id = session_file.stem
                if session_id in session_stats:
                    continue
                try:
                    records = parse_jsonl(session_file)
                except Exception:
                    continue

                session_title: str | None = None
                first_user_time_created: str | None = None
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
                    rtype = record.get("type", "")
                    ts = record.get("timestamp")
                    try:
                        record_time = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
                    except Exception:
                        record_time = None
                    if record_time is None or record_time < cutoff:
                        continue

                    if rtype == "user":
                        msg = record.get("message", {})
                        user_messages += 1
                        session_ids.add(session_id)
                        last_user_time = record_time
                        if session_title is None:
                            for block in msg.get("content", []):
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block["text"].strip()
                                    if text:
                                        session_title = text[:80] + ("..." if len(text) > 80 else "")
                                        break
                        if first_user_time_created is None:
                            first_user_time_created = ts
                        continue

                    if rtype != "assistant":
                        continue

                    msg = record.get("message", {})
                    usage_data = msg.get("usage") or {}
                    if not usage_data:
                        continue

                    inp = int(usage_data.get("input_tokens", 0) or 0)
                    out = int(usage_data.get("output_tokens", 0) or 0)
                    cache_r = int(usage_data.get("cache_read_input_tokens", 0) or 0)
                    cache_w = int(usage_data.get("cache_creation_input_tokens", 0) or 0)
                    cost_val = 0.0

                    total_tokens["input"] += inp
                    total_tokens["output"] += out
                    total_tokens["cache_read"] += cache_r
                    total_tokens["cache_write"] += cache_w
                    assistant_messages += 1
                    session_ids.add(session_id)

                    if last_user_time is not None:
                        delta = (record_time - last_user_time).total_seconds()
                        if 0 <= delta <= 600:
                            response_times.append(delta)
                        last_user_time = None

                    day_key = record_time.date().isoformat()
                    day = by_day.setdefault(day_key, {
                        "date": day_key, "cost": 0.0, "tokens": 0, "messages": 0,
                    })
                    day["tokens"] += inp + out + cache_r + cache_w
                    day["messages"] += 1

                    model_id = msg.get("model") or "unknown"
                    mk = (model_id, "anthropic")
                    m = by_model_key.setdefault(mk, {
                        "model_id": model_id,
                        "provider_id": "anthropic",
                        "total_cost": 0.0,
                        "total_tokens": _empty_tokens(),
                        "message_count": 0,
                    })
                    m["total_tokens"]["input"] += inp
                    m["total_tokens"]["output"] += out
                    m["total_tokens"]["cache_read"] += cache_r
                    m["total_tokens"]["cache_write"] += cache_w
                    m["message_count"] += 1

                    session_entry["total_tokens"] += inp + out + cache_r + cache_w
                    session_entry["message_count"] += 1

                if session_entry["message_count"] > 0:
                    if session_title:
                        session_entry["title"] = session_title
                    session_entry["time_created"] = first_user_time_created or iso_now()
                    session_stats[session_id] = session_entry

    _scan_root(AGENTS_DIR)
    _scan_native_cli_dir(_CLAUDE_NATIVE_PROJECTS_DIR)

    # Fill daily series with zeros
    today = datetime.now(timezone.utc).date()
    daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        daily.append(by_day.get(d, {"date": d, "cost": 0.0, "tokens": 0, "messages": 0}))

    by_model = sorted(by_model_key.values(), key=lambda m: m["total_cost"], reverse=True)
    by_session = sorted(session_stats.values(), key=lambda s: s["total_cost"], reverse=True)[:10]

    # Response-time stats
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
        avg_response_time = rt_stats["avg"]
    else:
        rt_stats = {"avg": 0, "median": 0, "p95": 0, "min": 0, "max": 0, "count": 0}
        avg_response_time = 0

    total_sessions = len(session_ids)
    flat_tokens = (
        total_tokens["input"] + total_tokens["output"]
        + total_tokens["cache_read"] + total_tokens["cache_write"]
        + total_tokens["reasoning"]
    )
    avg_tokens_per_session = flat_tokens / total_sessions if total_sessions else 0

    return {
        "total_cost": round(total_cost, 6),
        "total_tokens": total_tokens,
        "total_sessions": total_sessions,
        "total_messages": assistant_messages + user_messages,
        "avg_tokens_per_session": round(avg_tokens_per_session, 2),
        "avg_response_time": round(avg_response_time, 3),
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
