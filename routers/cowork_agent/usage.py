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

import sqlite3
from pathlib import Path

from fastapi import APIRouter

from services.cowork_agent.settings import AGENTS_DIR, HERMES_DIR
from services.cowork_agent.helpers import iso_now, parse_jsonl, derive_title_native_claude
from services.cowork_agent.project_layout import xo_projects_root
from services.cowork_agent.sessions_io import _find_native_claude_file, _resolve_index_path

router = APIRouter()


def _hermes_state_dbs() -> "list[tuple[str, Path]]":
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


def _empty_tokens():
    return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}


def _openclaw_session_id_from_filename(name: str) -> str | None:
    """Return the base session id for any transcript file OpenClaw counts.

    Matches OpenClaw's `isUsageCountedSessionTranscriptFileName` (active +
    `.reset.<iso>` + `.deleted.<iso>` archives). Excludes `.bak.<iso>` and
    `*.checkpoint.<uuid>.jsonl`, which OpenClaw's dashboard also excludes.

    Without the archive variants we miss every compacted/deleted session,
    which is what was causing /api/usage to undercount tokens vs the
    OpenClaw dashboard by ~10x.
    """
    for marker in (".jsonl.reset.", ".jsonl.deleted."):
        idx = name.find(marker)
        if idx > 0:
            return name[:idx]
    if name.endswith(".jsonl") and ".checkpoint." not in name:
        return name[:-len(".jsonl")]
    return None


@router.get("/api/usage")
def usage(days: int = 30):
    """
    Aggregate usage across all agents/sessions within the last `days`.
    Returns the UsageStats shape expected by the frontend (src/types/usage.ts).
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

    def _record_time(ts: str | None) -> datetime | None:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return None

    # ── OpenClaw sessions ─────────────────────────────────────────────────────

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
                    session_entry["time_created"] = first_user_ts or iso_now()
                    session_stats[session_id] = session_entry

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

    # ── Hermes sessions (all profiles aggregated) ─────────────────────────────
    #
    # Hermes stores per-profile session metadata in SQLite at
    # `~/.hermes/state.db` (default profile) and
    # `~/.hermes/profiles/<name>/state.db` (named profiles). The `sessions`
    # table already carries rolled-up tokens, message counts, model, billing
    # provider, and start time — so we read at session granularity rather than
    # walking every message row.
    #
    # We could spawn `hermes insights` subprocesses per profile to get the
    # rich engine output (platform/tool/skill breakdowns) — but that adds
    # ~200ms × N profiles and the engine's killer feature (pricing lookup)
    # currently returns 0 for the models in production (claude-opus-4-6,
    # kimi-k2.5). Plain SQL gives identical numbers at <10ms total.

    cutoff_epoch = cutoff.timestamp()
    for _profile, db_path in _hermes_state_dbs():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            continue
        try:
            rows = conn.execute(
                "SELECT id, source, model, started_at, title, "
                "       message_count, input_tokens, output_tokens, "
                "       cache_read_tokens, cache_write_tokens, "
                "       reasoning_tokens, "
                "       billing_provider, estimated_cost_usd, actual_cost_usd "
                "FROM sessions WHERE started_at >= ?",
                (cutoff_epoch,),
            ).fetchall()
        except sqlite3.OperationalError:
            # Schema mismatch (older/newer hermes); skip rather than 500.
            conn.close()
            continue
        conn.close()

        for row in rows:
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
            # `assistant_messages + user_messages` in the final response, so
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

    # ── Aggregate ─────────────────────────────────────────────────────────────

    today = datetime.now(timezone.utc).date()
    daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
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
