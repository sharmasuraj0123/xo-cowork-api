"""
usage_sync.py — Daily OpenClaw usage sync to xo-swarm-api.

Runs as an asyncio background task started from the FastAPI lifespan.
On first run (no watermark): full historical backfill of all JSONL data.
Subsequently: only processes dates after the last-synced watermark.
"""

import asyncio
import json
import os
import datetime
from collections import defaultdict

import httpx

from routers.openclaw_usage import (
    _discover_session_files,
    _parse_session_file,
    _date_from_ms,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CHAT_API_BASE_URL = os.getenv("CHAT_API_BASE_URL", "https://api-swarm-beta.xo.builders")
USAGE_REPORT_PATH = "/usage/report"

# Watermark file lives inside the cowork repo, not in openclaw's directory.
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_WATERMARK_PATH = os.path.join(_REPO_DIR, "data", "openclaw", "usage_sync_state.json")
SYNC_STATE_FILE = os.getenv("USAGE_SYNC_STATE_FILE", _DEFAULT_WATERMARK_PATH)

SYNC_HOUR_UTC = int(os.getenv("USAGE_SYNC_HOUR_UTC", "2"))
DEBUG_ENABLED = (os.getenv("USAGE_SYNC_DEBUG", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}
DEBUG_INTERVAL_MINUTES = int(os.getenv("USAGE_SYNC_DEBUG_INTERVAL_MINUTES", "0") or "0")

OPENCLAW_AGENT_ID = os.getenv("OPENCLAW_AGENT_ID", "main")

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _timestamp_prefix() -> str:
    tz_pref = (os.getenv("USAGE_SYNC_LOG_TZ", "UTC") or "UTC").strip().upper()
    if tz_pref == "IST":
        tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")
        tz_name = "IST"
    else:
        tz = datetime.timezone.utc
        tz_name = "UTC"

    ts = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    return f"[{ts} {tz_name}]"


def _debug_log(message: str) -> None:
    if DEBUG_ENABLED:
        print(f"{_timestamp_prefix()} usage_sync_debug: {message}")


# ---------------------------------------------------------------------------
# Watermark persistence
# ---------------------------------------------------------------------------


def _load_sync_state() -> dict:
    """Load last-synced state from local JSON file."""
    if os.path.exists(SYNC_STATE_FILE):
        try:
            with open(SYNC_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_sync_state(state: dict) -> None:
    """Persist sync state atomically."""
    os.makedirs(os.path.dirname(SYNC_STATE_FILE), exist_ok=True)
    tmp = SYNC_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, SYNC_STATE_FILE)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _aggregate_by_date(all_entries: list) -> dict:
    """
    Group parsed JSONL entries by calendar date and compute per-day totals.
    Returns {date_str: {report_date, tokens, costs, messages, model_usage, tool_usage}}.
    """
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

    for entry in all_entries:
        ts = entry.get("timestamp")
        if not ts:
            continue

        date_str = _date_from_ms(ts)
        d = days[date_str]

        usage = entry["usage"]
        cost_obj = usage.get("cost", {}) or {}

        inp = usage.get("input", 0) or 0
        out = usage.get("output", 0) or 0
        cr = usage.get("cacheRead", 0) or 0
        cw = usage.get("cacheWrite", 0) or 0
        tok = usage.get("totalTokens", 0) or (inp + out + cr + cw)
        c_total = cost_obj.get("total", 0) or 0

        d["total_input_tokens"] += inp
        d["total_output_tokens"] += out
        d["total_cache_read_tokens"] += cr
        d["total_cache_write_tokens"] += cw
        d["total_tokens"] += tok
        d["total_cost"] += c_total
        d["input_cost"] += cost_obj.get("input", 0) or 0
        d["output_cost"] += cost_obj.get("output", 0) or 0
        d["cache_read_cost"] += cost_obj.get("cacheRead", 0) or 0
        d["cache_write_cost"] += cost_obj.get("cacheWrite", 0) or 0
        d["total_messages"] += 1

        for tn in entry.get("toolNames", []):
            d["_tool_counter"][tn] += 1
            d["total_tool_calls"] += 1

        mkey = f"{entry.get('provider', '')}|{entry.get('model', '')}"
        if mkey not in d["_model_map"]:
            d["_model_map"][mkey] = {
                "provider": entry.get("provider", ""),
                "model": entry.get("model", ""),
                "calls": 0,
                "tokens": 0,
                "cost": 0.0,
            }
        mm = d["_model_map"][mkey]
        mm["calls"] += 1
        mm["tokens"] += tok
        mm["cost"] += c_total

    # Finalize: convert internal maps to lists, round costs
    result = {}
    for date_str, d in days.items():
        result[date_str] = {
            "report_date": date_str,
            "total_input_tokens": d["total_input_tokens"],
            "total_output_tokens": d["total_output_tokens"],
            "total_cache_read_tokens": d["total_cache_read_tokens"],
            "total_cache_write_tokens": d["total_cache_write_tokens"],
            "total_tokens": d["total_tokens"],
            "total_cost": round(d["total_cost"], 8),
            "input_cost": round(d["input_cost"], 8),
            "output_cost": round(d["output_cost"], 8),
            "cache_read_cost": round(d["cache_read_cost"], 8),
            "cache_write_cost": round(d["cache_write_cost"], 8),
            "total_messages": d["total_messages"],
            "total_sessions": 0,  # filled below
            "total_tool_calls": d["total_tool_calls"],
            "model_usage": list(d["_model_map"].values()),
            "tool_usage": [
                {"name": k, "count": v}
                for k, v in sorted(d["_tool_counter"].items(), key=lambda x: -x[1])
            ],
        }
    return result


# ---------------------------------------------------------------------------
# Core sync
# ---------------------------------------------------------------------------


async def _run_sync(is_backfill: bool = False) -> None:
    """
    Read all JSONL files, aggregate by date, POST to xo-swarm-api.

    Args:
        is_backfill: If True, ignore watermark and process all historical data.
    """
    from routers.auth import get_auth_token

    workspace_id = os.getenv("CODER_WORKSPACE_ID") or "unknown"
    workspace_name = os.getenv("CODER_WORKSPACE_NAME") or None
    project_id = os.getenv("XO_PROJECT_ID") or None

    state = _load_sync_state()
    last_synced_date = None if is_backfill else state.get("last_synced_date")

    session_files = _discover_session_files(OPENCLAW_AGENT_ID)
    if not session_files:
        print(f"{_timestamp_prefix()} usage_sync: no session files found, skipping")
        return

    # Parse all session files and collect entries + per-date session counts
    all_entries = []
    session_dates = defaultdict(set)  # date -> set of session file indices

    for sf_idx, sf in enumerate(session_files):
        _, entries = _parse_session_file(sf)

        if last_synced_date:
            entries = [
                e for e in entries
                if e.get("timestamp") and _date_from_ms(e["timestamp"]) >= last_synced_date
            ]

        for e in entries:
            if e.get("timestamp"):
                session_dates[_date_from_ms(e["timestamp"])].add(sf_idx)

        all_entries.extend(entries)

    if not all_entries:
        print(f"{_timestamp_prefix()} usage_sync: no new entries since {last_synced_date}, skipping")
        return

    # Aggregate by date
    daily = _aggregate_by_date(all_entries)

    # Fill in session counts and workspace identifiers
    for date_str, day in daily.items():
        day["total_sessions"] = len(session_dates.get(date_str, set()))
        day["workspace_id"] = workspace_id
        day["workspace_name"] = workspace_name
        day["project_id"] = project_id

    records = list(daily.values())
    if not records:
        return

    # POST to xo-swarm-api
    token = get_auth_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{CHAT_API_BASE_URL.rstrip('/')}{USAGE_REPORT_PATH}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(url, json={"records": records}, headers=headers)

        if response.status_code == 200:
            result = response.json()
            upserted = result.get("upserted", 0)
            print(f"{_timestamp_prefix()} usage_sync: successfully synced {upserted} day(s) to swarm")

            # Advance watermark to yesterday (not today) so today's partial
            # data gets re-sent and updated on the next cycle.
            yesterday = (
                datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
            ).strftime("%Y-%m-%d")
            latest_date = max(daily.keys())
            # Use the earlier of yesterday and the latest date we actually sent,
            # so we never skip unsent dates.
            watermark = min(yesterday, latest_date)
            state["last_synced_date"] = watermark
            state["last_sync_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            _save_sync_state(state)
        else:
            print(f"{_timestamp_prefix()} usage_sync: POST failed with {response.status_code}: {response.text[:200]}")
    except Exception as e:
        print(f"{_timestamp_prefix()} usage_sync: error posting to swarm (will retry next cycle): {e}")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


async def _seconds_until_next_run() -> float:
    """Calculate seconds until next scheduled run (hourly target or debug interval)."""
    if DEBUG_INTERVAL_MINUTES > 0:
        return float(DEBUG_INTERVAL_MINUTES * 60)

    now = datetime.datetime.now(datetime.timezone.utc)
    target = now.replace(hour=SYNC_HOUR_UTC, minute=0, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


def _next_target_utc(now: datetime.datetime | None = None) -> datetime.datetime:
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if DEBUG_INTERVAL_MINUTES > 0:
        return current + datetime.timedelta(minutes=DEBUG_INTERVAL_MINUTES)
    target = current.replace(hour=SYNC_HOUR_UTC, minute=0, second=0, microsecond=0)
    if target <= current:
        target += datetime.timedelta(days=1)
    return target


async def start_usage_sync_scheduler() -> None:
    """
    Entry point for the background task:
    1. If no watermark exists, run full backfill.
    2. Then run daily at SYNC_HOUR_UTC:00 UTC.

    Errors are caught and logged — a failure never crashes the server.
    """
    # Small delay to let the server finish starting up
    await asyncio.sleep(5)

    state = _load_sync_state()
    _debug_log(
        f"scheduler_started pid={os.getpid()} state_file={SYNC_STATE_FILE} "
        f"last_synced_date={state.get('last_synced_date')} last_sync_at={state.get('last_sync_at')} "
        f"mode={'debug_interval' if DEBUG_INTERVAL_MINUTES > 0 else 'daily'} "
        f"interval_minutes={DEBUG_INTERVAL_MINUTES if DEBUG_INTERVAL_MINUTES > 0 else 'n/a'}"
    )
    if not state.get("last_synced_date"):
        # First-ever run: full historical backfill
        print(f"{_timestamp_prefix()} usage_sync: no watermark found, starting historical backfill...")
        _debug_log("initial_path=backfill reason=no_watermark")
        try:
            await _run_sync(is_backfill=True)
            print(f"{_timestamp_prefix()} usage_sync: backfill complete")
            _debug_log("backfill_completed")
        except Exception as e:
            print(f"{_timestamp_prefix()} usage_sync: backfill error (non-fatal): {e}")
            _debug_log(f"backfill_failed error={e}")
    else:
        # Check if we missed a scheduled run (last sync > 24h ago).
        # This handles restarts after the 2 AM window was missed.
        last_sync_at = state.get("last_sync_at")
        needs_catchup = False
        catchup_reason = "no_last_sync_at"
        if last_sync_at:
            try:
                last_dt = datetime.datetime.fromisoformat(last_sync_at)
                hours_since = (datetime.datetime.now(datetime.timezone.utc) - last_dt).total_seconds() / 3600
                if hours_since > 24:
                    needs_catchup = True
                    catchup_reason = f"hours_since_gt_24 ({hours_since:.2f})"
                    print(f"{_timestamp_prefix()} usage_sync: last sync was {hours_since:.1f}h ago, running catch-up sync...")
                else:
                    catchup_reason = f"hours_since_le_24 ({hours_since:.2f})"
            except Exception:
                needs_catchup = True
                catchup_reason = "last_sync_at_parse_error"

        _debug_log(f"catchup_decision needs_catchup={needs_catchup} reason={catchup_reason}")

        if needs_catchup:
            try:
                _debug_log("trigger_enter source=startup_catchup")
                await _run_sync(is_backfill=False)
                print(f"{_timestamp_prefix()} usage_sync: catch-up sync complete")
                _debug_log("trigger_exit source=startup_catchup status=ok")
            except Exception as e:
                print(f"{_timestamp_prefix()} usage_sync: catch-up sync error (non-fatal): {e}")
                _debug_log(f"trigger_exit source=startup_catchup status=error error={e}")
        else:
            print(f"{_timestamp_prefix()} usage_sync: watermark found (last synced: {state['last_synced_date']}), next run at {SYNC_HOUR_UTC:02d}:00 UTC")

    # Daily loop
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        wait = await _seconds_until_next_run()
        target = _next_target_utc(now)
        _debug_log(
            f"sleep_begin pid={os.getpid()} now_utc={now.isoformat()} "
            f"target_utc={target.isoformat()} wait_seconds={wait:.1f}"
        )
        if DEBUG_INTERVAL_MINUTES > 0:
            print(
                f"{_timestamp_prefix()} usage_sync: next sync in {wait / 60:.1f}m "
                f"(debug interval mode: every {DEBUG_INTERVAL_MINUTES}m)"
            )
        else:
            print(f"{_timestamp_prefix()} usage_sync: next sync in {wait / 3600:.1f}h (at {SYNC_HOUR_UTC:02d}:00 UTC)")
        await asyncio.sleep(wait)
        wake = datetime.datetime.now(datetime.timezone.utc)
        _debug_log(f"sleep_end pid={os.getpid()} woke_at_utc={wake.isoformat()} skew_seconds={(wake - target).total_seconds():.1f}")
        try:
            _debug_log("trigger_enter source=daily_loop")
            await _run_sync(is_backfill=False)
            _debug_log("trigger_exit source=daily_loop status=ok")
        except Exception as e:
            print(f"{_timestamp_prefix()} usage_sync: daily sync error (will retry tomorrow): {e}")
            _debug_log(f"trigger_exit source=daily_loop status=error error={e}")
