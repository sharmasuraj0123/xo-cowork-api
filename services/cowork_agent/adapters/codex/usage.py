"""
Codex CLI usage — discovery + parser + dashboard aggregator.

Loaded by ``services.cowork_agent.engine.usage_loader.load_usage_module()`` when
``AGENT_NAME=codex``. Same public contract as claude_code/openclaw/antigravity;
the entries-based views (summary/analytics/sessions/summary_card/get_session/
sync) are delegated to :mod:`services.cowork_agent.adapters.usage_common` via a
``Source`` bound to this module's discovery + parser.

Source: ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ISO8601>-<uuid>.jsonl`` — the
on-disk rollout Codex writes per session. Discovery walks the sessions tree
**recursively** (codex nests by date, unlike claude's one-dir-per-project) via
``codex.paths.iter_rollouts`` — the single source of truth for the CODEX_HOME
resolution (env override, then manifest ``home_dir``, then ``~/.codex``). No
xo-project filtering, so the UI shows the full codex usage.

Token shape uses codex rollout fields from ``event_msg/token_count`` →
``info.last_token_usage`` (per-turn delta): ``input_tokens`` (INCLUDES the
cached portion), ``cached_input_tokens``, ``cache_write_input_tokens``,
``output_tokens``. We de-cache the input so downstream never double-counts.
Cost is always 0.0 — codex rollouts have no billing field (subscription plan).
Provider is the constant ``"openai"``; model is ``turn_context.model`` per turn.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional

from services.cowork_agent.adapters import usage_common as _uc
from services.cowork_agent.adapters.codex import paths as _paths


# ─────────────────────────────────────────────────────────────────────────────
# Configuration + helpers (used by discovery / parse_file / dashboard)
# ─────────────────────────────────────────────────────────────────────────────


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
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_jsonl(path: str) -> list[dict]:
    """Tolerant JSONL reader — skips malformed lines instead of raising,
    so one bad rollout record doesn't poison a whole session.
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


# Standard 8-4-4-4-12 UUID; the rollout filename is
# ``rollout-<ISO8601 with dashes>-<uuid>.jsonl`` so a trailing-UUID search is the
# only reliable split (the timestamp segment also contains dashes). This is an
# unanchored ``search`` variant of ``paths._UUID_RE`` (which is a private,
# fullmatch-anchored guard and not part of the paths public surface).
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _uuid_from_name(basename: str) -> str:
    """Extract the session UUID from a rollout filename.

    Kept identical to what get_session substring-matches on: the UUID appears in
    the basename, so returning it as ``meta.sessionId`` makes
    ``session_id in os.path.basename(sf)`` (usage_common.get_session) match.
    """
    m = _UUID_RE.search(basename)
    if m:
        return m.group(0)
    stem = basename[: -len(".jsonl")] if basename.endswith(".jsonl") else basename
    return stem


# TODO(codex): CONFIRM the exact injected-frame markers against a real rollout's
# event_msg/user_message frames. Verified: codex injects an environment/context
# preamble that must NOT count as a user turn. These two prefixes are the
# high-confidence defaults; widen if a real rollout shows other wrappers.
#   Check: for f in ~/.codex/sessions/**/rollout-*.jsonl; do
#     jq -c 'select(.type=="event_msg" and .payload.type=="user_message")
#            | .payload.message' "$f"; done | head
_INJECTED_USER_PREFIXES = ("<environment_context>", "<user_instructions>")


def _user_text(message) -> str:
    """Normalize a user_message.message (str, or list of content blocks) to text."""
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        return " ".join(
            b.get("text", "")
            for b in message
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        ).strip()
    return ""


def _is_genuine_user(message) -> bool:
    """True only for a real user prompt — filters injected developer /
    <environment_context> frames so protocol scaffolding is not a 'turn'."""
    text = _user_text(message)
    if not text:
        return False
    return not any(text.startswith(p) for p in _INJECTED_USER_PREFIXES)


def _derive_title(records: list[dict]) -> str:
    """First genuine user prompt in the rollout becomes the session title."""
    for r in records:
        if r.get("type") != "event_msg":
            continue
        payload = r.get("payload") or {}
        if payload.get("type") != "user_message":
            continue
        msg = payload.get("message")
        if _is_genuine_user(msg):
            text = _user_text(msg)
            return text[:80] + ("..." if len(text) > 80 else "")
    return "Untitled Session"


# ─────────────────────────────────────────────────────────────────────────────
# File discovery — every rollout under $CODEX_HOME/sessions/**/  (RECURSIVE)
# ─────────────────────────────────────────────────────────────────────────────


def _discover() -> list[str]:
    """Return every ``rollout-*.jsonl`` under the sessions root, RECURSIVELY.

    Delegates the recursive walk + CODEX_HOME resolution to
    ``codex.paths.iter_rollouts`` (the canonical codex discovery primitive: it
    rglobs ``sessions/YYYY/MM/DD/`` — NOT one level deep like claude_code's
    projects walk — and returns an empty iterator when the tree is absent). We
    re-sort by path string for a deterministic order independent of filesystem
    mtimes. No xo-project filtering: we want the full picture the Settings →
    Usage tab needs.
    """
    return sorted(str(p) for p in _paths.iter_rollouts() if p.is_file())


# ─────────────────────────────────────────────────────────────────────────────
# Contract — discovery + parser (codex-specific)
# ─────────────────────────────────────────────────────────────────────────────


def get_session_files(*, agent_id: Optional[str] = None) -> list[str]:
    """Every codex rollout .jsonl on disk.

    ``agent_id`` is accepted for contract symmetry with openclaw but ignored —
    codex is single-tenant per deployment.
    """
    return _discover()


def parse_file(
    path: str,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> tuple[dict, list]:
    """Parse a single codex rollout .jsonl into ``(meta, entries)``.

    Entries are the shared normalized shape (role=user|assistant). One assistant
    entry per ``event_msg/token_count`` using ``info.last_token_usage`` (per-turn
    delta). ``meta.sessionId`` is the session UUID (from session_meta or the
    filename) so ``usage_common.get_session`` substring-matches it in the basename.
    """
    meta: dict = {"sessionId": None, "sessionFile": os.path.basename(path)}
    entries: list = []
    last_user_ts: Optional[int] = None
    current_model: Optional[str] = None

    records = _read_jsonl(path)

    # Default session id = UUID from the filename; session_meta overrides below.
    meta["sessionId"] = _uuid_from_name(os.path.basename(path))

    for record in records:
        top = record.get("type")
        payload = record.get("payload") or {}
        ts_str = record.get("timestamp")
        rt = _record_time(ts_str)
        ts_epoch_ms = int(rt.timestamp() * 1000) if rt else None

        if top == "session_meta":
            sid = payload.get("session_id") or payload.get("id")
            if isinstance(sid, str) and sid:
                meta["sessionId"] = sid
            continue

        if top == "turn_context":
            m = payload.get("model")
            if isinstance(m, str) and m:
                current_model = m
            continue

        if top != "event_msg":
            continue

        # Window filter applies to timestamped event rows (parity w/ claude_code).
        if ts_epoch_ms is not None:
            if start_ms and ts_epoch_ms < start_ms:
                continue
            if end_ms and ts_epoch_ms > end_ms:
                continue

        etype = payload.get("type")

        if etype == "user_message":
            if _is_genuine_user(payload.get("message")):
                last_user_ts = ts_epoch_ms
                entries.append({"role": "user", "timestamp": ts_epoch_ms})
            continue

        if etype != "token_count":
            continue

        info = payload.get("info") or {}
        # last_token_usage = per-turn delta (NOT the cumulative total_token_usage).
        ltu = info.get("last_token_usage") or {}
        if not ltu:
            continue

        input_tokens = int(ltu.get("input_tokens", 0) or 0)
        cached = int(ltu.get("cached_input_tokens", 0) or 0)
        output_tokens = int(ltu.get("output_tokens", 0) or 0)
        cache_write = int(ltu.get("cache_write_input_tokens", 0) or 0)

        # codex input_tokens INCLUDES the cached portion — de-cache it so the
        # shared views never double-count (input + cacheRead == input_tokens).
        inp = input_tokens - cached
        if inp < 0:
            inp = input_tokens  # defensive: cached should never exceed input

        total = input_tokens + output_tokens  # VERIFIED rule (excludes cacheWrite)
        duration_ms = (ts_epoch_ms - last_user_ts) if (last_user_ts and ts_epoch_ms) else None

        entries.append({
            "role": "assistant",
            "usage": {
                "input": inp,
                "output": output_tokens,
                "cacheRead": cached,
                "cacheWrite": cache_write,
                "totalTokens": total,
                "cost": {"total": 0.0, "input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0},
            },
            "provider": "openai",
            # TODO(codex): "gpt-5" is only a fallback when no turn_context.model
            # precedes the first token_count (shouldn't happen in a real session).
            "model": current_model or "gpt-5",
            "timestamp": ts_epoch_ms,
            "stopReason": None,
            "toolNames": [],
            "toolResultCounts": {"total": 0, "errors": 0},
            "durationMs": duration_ms,
        })

    return meta, entries


# ─────────────────────────────────────────────────────────────────────────────
# Contract — aggregate_for_dashboard (/api/usage)
# Codex-specific (separate from shared entries views): re-reads rollout lines
# with empty-session mtime parity and a response-time clamp. Always returns
# total_cost=0.0 (no billing in codex rollouts). NO 'estimated' flag — codex
# token_count is real usage, unlike agy's client-side estimates.
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
        session_id = _uuid_from_name(os.path.basename(path))

        # Empty-session mtime parity: any rollout whose mtime falls in the
        # window contributes to total_sessions, even with zero usage rows.
        try:
            if os.path.getmtime(path) * 1000 >= cutoff_ms:
                session_ids.add(session_id)
        except OSError:
            pass

        records = _read_jsonl(path)
        if not records:
            continue

        # session_meta (line 1) overrides the filename-derived id.
        for r in records:
            if r.get("type") == "session_meta":
                sid = (r.get("payload") or {}).get("session_id")
                if isinstance(sid, str) and sid:
                    session_id = sid
                break

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
        current_model: Optional[str] = None

        for record in records:
            top = record.get("type")
            payload = record.get("payload") or {}

            if top == "turn_context":
                m = payload.get("model")
                if isinstance(m, str) and m:
                    current_model = m
                continue

            if top != "event_msg":
                continue

            rt = _record_time(record.get("timestamp"))
            if rt is None or rt < cutoff:
                continue

            etype = payload.get("type")

            if etype == "user_message":
                if _is_genuine_user(payload.get("message")):
                    user_messages += 1
                    session_ids.add(session_id)
                    last_user_time = rt
                    if first_user_ts is None:
                        first_user_ts = record.get("timestamp")
                continue

            if etype != "token_count":
                continue

            info = payload.get("info") or {}
            ltu = info.get("last_token_usage") or {}
            if not ltu:
                continue

            input_tokens = int(ltu.get("input_tokens", 0) or 0)
            cached = int(ltu.get("cached_input_tokens", 0) or 0)
            output_tokens = int(ltu.get("output_tokens", 0) or 0)
            cache_write = int(ltu.get("cache_write_input_tokens", 0) or 0)
            inp = input_tokens - cached
            if inp < 0:
                inp = input_tokens

            total_tokens["input"] += inp
            total_tokens["output"] += output_tokens
            total_tokens["cache_read"] += cached
            total_tokens["cache_write"] += cache_write
            assistant_messages += 1
            session_ids.add(session_id)

            if last_user_time is not None:
                delta = (rt - last_user_time).total_seconds()
                if 0 <= delta <= 600:
                    response_times.append(delta)
                last_user_time = None

            day_key = rt.date().isoformat()
            day = by_day.setdefault(day_key, {"date": day_key, "cost": 0.0, "tokens": 0, "messages": 0})
            day["tokens"] += input_tokens + output_tokens
            day["messages"] += 1

            model_id = current_model or "gpt-5"
            mk = (model_id, "openai")
            m = by_model_key.setdefault(mk, {
                "model_id": model_id, "provider_id": "openai",
                "total_cost": 0.0, "total_tokens": _empty_tokens(), "message_count": 0,
            })
            m["total_tokens"]["input"] += inp
            m["total_tokens"]["output"] += output_tokens
            m["total_tokens"]["cache_read"] += cached
            m["total_tokens"]["cache_write"] += cache_write
            m["message_count"] += 1

            session_entry["total_tokens"] += input_tokens + output_tokens
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


def dashboard(*, window: dict) -> dict:
    """UsageStats — codex's /api/usage payload. Wraps aggregate_for_dashboard."""
    if "days" in window:
        return aggregate_for_dashboard(days=int(window["days"]), tz=window.get("tz", "local"))
    _start_ms, _end_ms, derived_days = _uc.window_to_ms(window)
    return aggregate_for_dashboard(days=derived_days or 30, tz="local")


# ─────────────────────────────────────────────────────────────────────────────
# Shared entries-based views — bound to this module's discovery + parser.
# (Delegates + __all__ copied from antigravity/usage.py:258-298.)
# ─────────────────────────────────────────────────────────────────────────────

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


# Canonical sync surface name (used by services/usage_sync.py:191).
sync_payload = aggregate_for_sync


__all__ = [
    "get_session_files", "parse_file", "aggregate_for_dashboard", "dashboard",
    "build_summary", "analytics", "summary", "summary_card", "list_sessions",
    "get_session", "aggregate_for_sync", "sync_payload",
]
