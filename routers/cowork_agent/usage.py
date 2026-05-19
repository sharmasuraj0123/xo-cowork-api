"""
Aggregated usage statistics across all agents/sessions.

Scans two sources:
- OpenClaw: ~/.openclaw/agents/*/sessions/*.jsonl
- Claude Code: ~/.claude/projects/{encoded}/*.jsonl via sessionslist.json index

Returns the UsageStats shape defined in the frontend (src/types/usage.ts).
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter

from services.cowork_agent.helpers import iso_now, parse_jsonl, derive_title_native_claude
from services.cowork_agent.project_layout import xo_projects_root
from services.cowork_agent.sessions_io import _find_native_claude_file, _resolve_index_path

router = APIRouter()


def _empty_tokens():
    return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}


@router.get("/api/usage")
async def usage(days: int = 30):
    """
    Aggregate usage across all agents/sessions within the last `days`.
    Returns the UsageStats shape expected by the frontend (src/types/usage.ts).

    OpenClaw contribution flows through ``AgentDispatcher("openclaw").aggregate_usage``
    (Phase 6b). The Claude Code scan still runs inline below — out of scope for
    the OpenClaw modularization. The two contributions are merged into the
    single UsageStats response.
    """
    days = max(1, min(days, 365))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # ── OpenClaw contribution: dispatched through the adapter (Phase 6b) ──────
    # Falls back to an empty rollup if the OpenClaw adapter isn't registered
    # (downstream fork running with only claude_code / hermes).
    from services.cowork_agent.dispatcher import AgentDispatcher
    try:
        oc = await AgentDispatcher("openclaw").aggregate_usage(days)
    except (KeyError, ValueError):
        today = datetime.now(timezone.utc).date()
        oc = {
            "total_cost": 0.0,
            "total_tokens": _empty_tokens(),
            "total_sessions": 0,
            "total_messages": 0,
            "avg_tokens_per_session": 0,
            "avg_response_time": 0,
            "by_model": [],
            "by_session": [],
            "daily": [
                {"date": (today - timedelta(days=i)).isoformat(), "cost": 0.0, "tokens": 0, "messages": 0}
                for i in range(days - 1, -1, -1)
            ],
            "response_time": {"avg": 0, "median": 0, "p95": 0, "min": 0, "max": 0, "count": 0},
        }

    # Seed accumulators from the OpenClaw aggregate so the Claude Code scan
    # below can merge directly into them. (Deep-copy lists/dicts so mutating
    # the accumulators doesn't reach back into ``oc``.)
    total_tokens = dict(oc["total_tokens"])
    total_cost = float(oc["total_cost"])
    assistant_messages = 0  # Claude-only — OC already counted in oc["total_messages"]
    user_messages = 0       # Claude-only
    session_ids: set[str] = set()  # Claude-only — OC already counted in oc["total_sessions"]

    by_day: dict[str, dict] = {d["date"]: dict(d) for d in oc["daily"]}
    by_model_key: dict[tuple[str, str], dict] = {}
    for m in oc["by_model"]:
        by_model_key[(m["model_id"], m["provider_id"])] = {
            "model_id":     m["model_id"],
            "provider_id":  m["provider_id"],
            "total_cost":   m["total_cost"],
            "total_tokens": dict(m["total_tokens"]),
            "message_count": m["message_count"],
        }
    session_stats: dict[str, dict] = {s["session_id"]: dict(s) for s in oc["by_session"]}
    response_times: list[float] = []  # Claude-only raw samples; OC's pre-aggregated stats merged at the end

    def _record_time(ts: str | None) -> datetime | None:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return None

    # ── Claude Code sessions ──────────────────────────────────────────────────

    projects_root = xo_projects_root()
    if projects_root.exists():
        for project_dir in sorted(projects_root.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith("."):
                continue
            idx_path = _resolve_index_path(project_dir / ".xo" / "sessions")
            if not idx_path:
                continue
            try:
                index = json.loads(idx_path.read_text(encoding="utf-8"))
            except Exception:
                continue

            for _key, meta in index.items():
                if not isinstance(meta, dict) or meta.get("backend") != "claude_code":
                    continue
                session_id = meta.get("sessionId", "")
                native_id = meta.get("nativeSessionId", "")
                directory = meta.get("directory", "")
                if not session_id or not native_id:
                    continue

                native_path = _find_native_claude_file(native_id, directory)
                if not native_path:
                    continue

                try:
                    records = parse_jsonl(native_path)
                except Exception:
                    continue

                session_entry = {
                    "session_id": session_id,
                    "title": derive_title_native_claude(records),
                    "total_cost": 0.0,
                    "total_tokens": 0,
                    "message_count": 0,
                    "time_created": None,
                }
                first_user_ts: str | None = None
                last_user_time: datetime | None = None
                # Dedup by Anthropic message id — every streaming record of
                # the same API call shares the same ``msg.id``. Robust to
                # interleaved user records (tool_results, etc.) — the prior
                # ``(in,out,cr,cw)`` tuple + reset-on-user-record approach
                # over-counted by ~75% in real sessions because tool_result
                # records between streaming chunks reset the dedup and the
                # next chunk of the same turn was counted as new.
                seen_message_ids: set[str] = set()

                for record in records:
                    rtype = record.get("type")
                    msg = record.get("message", {})
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
                            # Only count records with actual user text, not tool_result-only
                            # records that are internal protocol messages.
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

                    # Dedup by Anthropic message id (unique per API call,
                    # shared across all streaming chunks of the same turn).
                    msg_id = msg.get("id")
                    if isinstance(msg_id, str) and msg_id:
                        if msg_id in seen_message_ids:
                            continue
                        seen_message_ids.add(msg_id)

                    inp = int(usage_data.get("input_tokens", 0) or 0)
                    out = int(usage_data.get("output_tokens", 0) or 0)
                    cache_r = int(usage_data.get("cache_read_input_tokens", 0) or 0)
                    cache_w = int(usage_data.get("cache_creation_input_tokens", 0) or 0)

                    cost_val = 0.0  # Claude Code JSONL doesn't include billing cost

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
                    session_entry["time_created"] = first_user_ts or iso_now()
                    session_stats[session_id] = session_entry

    # ── Aggregate ─────────────────────────────────────────────────────────────

    today = datetime.now(timezone.utc).date()
    daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        daily.append(by_day.get(d, {"date": d, "cost": 0.0, "tokens": 0, "messages": 0}))

    by_model = sorted(by_model_key.values(), key=lambda m: m["total_cost"], reverse=True)
    by_session = sorted(session_stats.values(), key=lambda s: s["total_tokens"], reverse=True)[:10]

    # Claude-side response-time stats from raw samples.
    if response_times:
        sorted_rt = sorted(response_times)
        n = len(sorted_rt)
        claude_rt = {
            "avg": sum(sorted_rt) / n,
            "median": sorted_rt[n // 2],
            "p95": sorted_rt[min(n - 1, int(n * 0.95))],
            "min": sorted_rt[0],
            "max": sorted_rt[-1],
            "count": n,
        }
    else:
        claude_rt = {"avg": 0, "median": 0, "p95": 0, "min": 0, "max": 0, "count": 0}

    # Merge OpenClaw's pre-aggregated response_time with Claude's. We don't
    # have OC's raw samples, so percentile fields (avg/median/p95) are
    # weighted by sample count — mathematically approximate but the best we
    # can do without surfacing raw samples through the adapter contract.
    # min/max use element-wise extremes; count is exact.
    oc_rt = oc["response_time"]
    oc_count = int(oc_rt.get("count", 0))
    claude_count = claude_rt["count"]
    total_rt_count = oc_count + claude_count
    if total_rt_count > 0:
        def _wmerge(key: str) -> float:
            return (oc_rt[key] * oc_count + claude_rt[key] * claude_count) / total_rt_count
        if oc_count == 0:
            rt_min, rt_max = claude_rt["min"], claude_rt["max"]
        elif claude_count == 0:
            rt_min, rt_max = oc_rt["min"], oc_rt["max"]
        else:
            rt_min = min(oc_rt["min"], claude_rt["min"])
            rt_max = max(oc_rt["max"], claude_rt["max"])
        rt_stats = {
            "avg":    _wmerge("avg"),
            "median": _wmerge("median"),
            "p95":    _wmerge("p95"),
            "min":    rt_min,
            "max":    rt_max,
            "count":  total_rt_count,
        }
    else:
        rt_stats = {"avg": 0, "median": 0, "p95": 0, "min": 0, "max": 0, "count": 0}

    # OpenClaw sessions were counted inside aggregate_usage; here we add only
    # the Claude-side ones to avoid double-counting.
    total_sessions = oc["total_sessions"] + len(session_ids)
    # OpenClaw's user+assistant messages are baked into oc["total_messages"];
    # here we add Claude's user+assistant counters on top.
    total_messages = oc["total_messages"] + assistant_messages + user_messages
    flat_tokens = total_tokens["input"] + total_tokens["output"] + total_tokens["reasoning"]
    avg_tokens_per_session = flat_tokens / total_sessions if total_sessions else 0

    return {
        "total_cost": round(total_cost, 6),
        "total_tokens": total_tokens,
        "total_sessions": total_sessions,
        "total_messages": total_messages,
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
