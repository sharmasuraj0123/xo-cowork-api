"""
Antigravity (agy) usage — discovery + parser + dashboard aggregator.

Loaded by ``engine.usage_loader.load_usage_module()`` when
``AGENT_NAME=antigravity``. Same public contract as claude_code/openclaw; the
entries-based views (summary/analytics/sessions/summary_card/get_session/sync)
are delegated to :mod:`services.cowork_agent.adapters.usage_common` via a
``Source`` bound to this module's discovery + parser.

Sources:
  * **structure** — the transcript ``brain/<uuid>/…/transcript_full.jsonl``
    (turns, timestamps, tool names). Discovery walks the brain tree directly, so
    the UI shows the full agy usage the way ``agy`` itself would.
  * **tokens** — ``conversations/<uuid>.db`` ``gen_metadata`` protobuf
    (:mod:`tokens`). ⚠️ These are **client-side ESTIMATES**, not billed usage;
    there is **no cache_read/cache_write** for agy, and cost is always ``0.0``.
    Because per-call→per-turn attribution needs the packed ``f2`` field, we
    attribute a conversation's whole token total to its **last** assistant entry
    (documented approximation — totals are exact, per-turn split is not).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional

from services.cowork_agent.adapters import usage_common as _uc
from services.cowork_agent.adapters.antigravity import transcript as _t
from services.cowork_agent.adapters.antigravity.paths import BRAIN_DIR, transcript_path
from services.cowork_agent.adapters.antigravity.tokens import conversation_tokens


def _resolve_tz(tz: str) -> tzinfo:
    if tz == "utc":
        return timezone.utc
    return datetime.now().astimezone().tzinfo or timezone.utc


def _empty_tokens() -> dict:
    return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}


def _provider_for(model: str | None) -> str:
    low = (model or "").lower()
    if "gemini" in low:
        return "google"
    if "claude" in low:
        return "anthropic"
    if "gpt" in low or "oss" in low:
        return "open"
    return "google"


def _discover() -> list[str]:
    """Every agy conversation id that has a transcript on disk."""
    if not BRAIN_DIR.is_dir():
        return []
    out: list[str] = []
    for d in BRAIN_DIR.iterdir():
        if d.is_dir() and transcript_path(d.name).is_file():
            out.append(d.name)
    return out


def get_session_files(*, agent_id: Optional[str] = None) -> list[str]:
    """Conversation ids (``agent_id`` accepted for contract symmetry, ignored)."""
    return _discover()


def parse_file(
    conversation_id: str,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> tuple[dict, list]:
    """Parse one agy conversation into ``(meta, entries)`` in the shared shape.

    ``entries`` are normalized usage records (role=user|assistant). The whole
    conversation's token total is placed on the last assistant entry; earlier
    assistant entries carry a zero-usage dict (required by the shared views)."""
    meta: dict = {"sessionId": conversation_id, "sessionFile": conversation_id}
    steps = _t.read_steps(conversation_id)
    if not steps:
        return meta, []

    turns = list(_t.iter_turns(steps))
    # Title = first user prompt.
    for turn in turns:
        if turn["role"] == "user" and turn["text"]:
            meta["title"] = turn["text"][:80]
            break

    tok = conversation_tokens(conversation_id)
    model = tok.get("model") or "Gemini 3.5 Flash"
    provider = _provider_for(model)

    # Index of the last assistant turn (carries the conversation's tokens).
    last_assistant_idx = max(
        (i for i, t in enumerate(turns) if t["role"] == "assistant"),
        default=None,
    )

    entries: list = []
    last_user_ts: Optional[int] = None
    for i, turn in enumerate(turns):
        ts = turn.get("ts_ms")
        if ts is not None:
            if start_ms and ts < start_ms:
                continue
            if end_ms and ts > end_ms:
                continue

        if turn["role"] == "user":
            last_user_ts = ts
            entries.append({"role": "user", "timestamp": ts})
            continue

        is_last = i == last_assistant_idx
        inp = int(tok.get("total_input", 0)) if is_last else 0
        out = int(tok.get("total_output", 0)) if is_last else 0
        duration_ms = (ts - last_user_ts) if (last_user_ts and ts) else None
        entries.append({
            "role": "assistant",
            "usage": {
                "input": inp,
                "output": out,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": inp + out,
                "cost": {"total": 0.0, "input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0},
            },
            "provider": provider,
            "model": model,
            "timestamp": ts,
            "stopReason": None,
            "toolNames": turn.get("tool_names", []),
            "toolResultCounts": {"total": 0, "errors": 0},
            "durationMs": duration_ms,
            # Flag agy tokens as non-comparable client-side estimates.
            "estimated": True,
        })
    return meta, entries


# ── /api/usage dashboard (UsageStats) ─────────────────────────────────────────


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

    for cid in _discover():
        meta, entries = parse_file(cid, start_ms=cutoff_ms, end_ms=None)
        sid = meta.get("sessionId") or cid
        sess = session_stats.setdefault(sid, {
            "session_id": sid, "title": meta.get("title", ""),
            "total_tokens": 0, "message_count": 0, "time_created": None,
        })
        first_user_ts = None
        for e in entries:
            ts = e.get("timestamp")
            day = (
                datetime.fromtimestamp(ts / 1000, tz=z).date().isoformat()
                if ts else today.date().isoformat()
            )
            dbucket = by_day.setdefault(day, {"date": day, "cost": 0.0, "tokens": 0, "messages": 0})
            if e["role"] == "user":
                user_messages += 1
                dbucket["messages"] += 1
                if first_user_ts is None:
                    first_user_ts = ts
                continue
            assistant_messages += 1
            session_ids.add(sid)
            u = e["usage"]
            total_tokens["input"] += u["input"]
            total_tokens["output"] += u["output"]
            flat = u["input"] + u["output"]
            dbucket["tokens"] += flat
            dbucket["messages"] += 1
            sess["total_tokens"] += flat
            sess["message_count"] += 1
            mkey = (e.get("provider", ""), e.get("model", ""))
            mstat = by_model_key.setdefault(mkey, {
                "provider": mkey[0], "model": mkey[1],
                "message_count": 0, "total_tokens": 0, "cost": 0.0,
            })
            mstat["message_count"] += 1
            mstat["total_tokens"] += flat
            if e.get("durationMs"):
                response_times.append(e["durationMs"] / 1000)
        if sess["message_count"] > 0 and sess["time_created"] is None:
            sess["time_created"] = (
                datetime.fromtimestamp(first_user_ts / 1000, tz=timezone.utc).isoformat()
                if first_user_ts else datetime.now(timezone.utc).isoformat()
            )

    daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).date().isoformat()
        daily.append(by_day.get(d, {"date": d, "cost": 0.0, "tokens": 0, "messages": 0}))

    by_model = sorted(by_model_key.values(), key=lambda m: m["message_count"], reverse=True)
    by_session = sorted(
        (s for s in session_stats.values() if s["message_count"] > 0),
        key=lambda s: s["total_tokens"], reverse=True,
    )[:10]

    if response_times:
        rt = sorted(response_times); n = len(rt)
        rt_stats = {"avg": sum(rt) / n, "median": rt[n // 2],
                    "p95": rt[min(n - 1, int(n * 0.95))], "min": rt[0], "max": rt[-1], "count": n}
    else:
        rt_stats = {"avg": 0, "median": 0, "p95": 0, "min": 0, "max": 0, "count": 0}

    total_sessions = len(session_ids)
    flat_tokens = total_tokens["input"] + total_tokens["output"] + total_tokens["reasoning"]
    avg_tokens = flat_tokens / total_sessions if total_sessions else 0

    return {
        "total_cost": 0.0,
        "total_tokens": total_tokens,
        "total_sessions": total_sessions,
        "total_messages": assistant_messages + user_messages,
        "avg_tokens_per_session": round(avg_tokens, 2),
        "avg_response_time": round(rt_stats["avg"], 3),
        "by_model": by_model,
        "by_session": by_session,
        "daily": daily,
        "response_time": {
            "avg": round(rt_stats["avg"], 3), "median": round(rt_stats["median"], 3),
            "p95": round(rt_stats["p95"], 3), "min": round(rt_stats["min"], 3),
            "max": round(rt_stats["max"], 3), "count": rt_stats["count"],
        },
        # Signal to the frontend that agy tokens are client-side estimates.
        "estimated_tokens": True,
    }


def dashboard(*, window: dict) -> dict:
    if "days" in window:
        return aggregate_for_dashboard(days=int(window["days"]), tz=window.get("tz", "local"))
    _s, _e, derived = _uc.window_to_ms(window)
    return aggregate_for_dashboard(days=derived or 30, tz="local")


# ── Shared entries-based views ────────────────────────────────────────────────

_SOURCE = _uc.Source(discover=lambda: get_session_files(), parse_file=parse_file)


def build_summary(session_meta: dict, entries: list) -> dict:
    return _uc.build_summary(session_meta, entries)


def analytics(*, window: dict) -> dict:
    return _uc.analytics(_SOURCE, window=window)


def summary(*, window: dict) -> dict:
    return _uc.summary(_SOURCE, window=window)


def summary_card(*, window: dict) -> dict:
    return _uc.summary_card(_SOURCE, window=window)


def list_sessions() -> dict:
    return _uc.list_sessions(_SOURCE)


def get_session(session_id: str, *, window: dict | None = None) -> dict | None:
    return _uc.get_session(_SOURCE, session_id, window=window)


def aggregate_for_sync(*, since_date: Optional[str] = None) -> dict:
    return _uc.aggregate_for_sync(_SOURCE, since_date=since_date)


sync_payload = aggregate_for_sync


__all__ = [
    "get_session_files", "parse_file", "aggregate_for_dashboard", "dashboard",
    "build_summary", "analytics", "summary", "summary_card", "list_sessions",
    "get_session", "aggregate_for_sync", "sync_payload",
]
