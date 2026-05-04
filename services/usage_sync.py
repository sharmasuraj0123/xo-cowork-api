"""
usage_sync.py — Daily OpenClaw usage sync to xo-swarm-api.

Runs as an asyncio background task started from the FastAPI lifespan.
On first run (no watermark): full historical backfill of all JSONL data.
Subsequently: only processes dates after the last-synced watermark.
"""

import asyncio
import glob
import json
import os
import datetime
from collections import defaultdict

import httpx

from services.cowork_agent.adapters.openclaw.usage import (
    discover_session_files,
    parse_session_file,
    date_from_ms,
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

OPENCLAW_AGENT_ID = os.getenv("OPENCLAW_AGENT_ID", "main")

_CLAUDE_NATIVE_PROJECTS_DIR = os.path.expanduser(os.getenv("CLAUDE_NATIVE_PROJECTS_DIR", "~/.claude/projects"))

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


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
# Native Claude CLI session discovery and parsing
# ---------------------------------------------------------------------------


def _discover_claude_native_session_files() -> list[str]:
    """Discover ~/.claude/projects/*/*.jsonl (all native Claude CLI sessions)."""
    pattern = os.path.join(_CLAUDE_NATIVE_PROJECTS_DIR, "*", "*.jsonl")
    return sorted(glob.glob(pattern))


def _parse_claude_native_session_file(path: str) -> list[dict]:
    """
    Parse ~/.claude/projects/<project>/<uuid>.jsonl into the normalized entry
    shape used by _aggregate_by_date.

    Native field names (snake_case) are mapped to the standard camelCase shape:
      input_tokens                  → input
      output_tokens                 → output
      cache_read_input_tokens       → cacheRead
      cache_creation_input_tokens   → cacheWrite
    """
    entries: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return entries

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        rtype = record.get("type", "")
        if rtype == "assistant":
            msg = record.get("message", {})
            ts_raw = record.get("timestamp", "")
        else:
            continue

        usage_raw = msg.get("usage") or {}
        inp = int(usage_raw.get("input_tokens", 0) or 0)
        out = int(usage_raw.get("output_tokens", 0) or 0)
        cache_r = int(usage_raw.get("cache_read_input_tokens", 0) or 0)
        cache_w = int(usage_raw.get("cache_creation_input_tokens", 0) or 0)
        total = inp + out + cache_r + cache_w

        if not total:
            continue

        try:
            ts_ms = int(
                datetime.datetime.fromisoformat(
                    ts_raw.replace("Z", "+00:00")
                ).timestamp() * 1000
            )
        except Exception:
            continue

        tool_names: list[str] = []
        for block in msg.get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name", "")
                if name:
                    tool_names.append(name)

        entries.append({
            "timestamp": ts_ms,
            "provider": "anthropic",
            "model": msg.get("model") or "",
            "toolNames": tool_names,
            "usage": {
                "input": inp,
                "output": out,
                "cacheRead": cache_r,
                "cacheWrite": cache_w,
                "totalTokens": total,
                "cost": {},
            },
        })

    return entries


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

        date_str = date_from_ms(ts)
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

        mkey = f"{entry.get('provider') or ''}|{entry.get('model') or ''}"
        if mkey not in d["_model_map"]:
            d["_model_map"][mkey] = {
                "provider": entry.get("provider") or "",
                "model": entry.get("model") or "",
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

    # Collect entries from all three session sources
    all_entries = []
    session_dates = defaultdict(set)  # date -> set of session file indices
    sf_idx = 0

    def _collect(files: list[str], parse_fn) -> None:
        nonlocal sf_idx
        for sf in files:
            entries = parse_fn(sf)
            if last_synced_date:
                entries = [
                    e for e in entries
                    if e.get("timestamp") and date_from_ms(e["timestamp"]) >= last_synced_date
                ]
            for e in entries:
                if e.get("timestamp"):
                    session_dates[date_from_ms(e["timestamp"])].add(sf_idx)
            all_entries.extend(entries)
            sf_idx += 1

    # OpenClaw sessions
    _collect(discover_session_files(OPENCLAW_AGENT_ID), lambda sf: parse_session_file(sf)[1])
    # All native Claude CLI sessions — covers cowork and any other Claude Code usage
    _collect(_discover_claude_native_session_files(), _parse_claude_native_session_file)

    if not all_entries:
        print(f"usage_sync: no new entries since {last_synced_date}, skipping")
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
            print(f"usage_sync: successfully synced {upserted} day(s) to swarm")

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
            print(f"usage_sync: POST failed with {response.status_code}: {response.text[:200]}")
    except Exception as e:
        print(f"usage_sync: error posting to swarm (will retry next cycle): {e}")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


async def _seconds_until_next_run() -> float:
    """Calculate seconds until next SYNC_HOUR_UTC:00 UTC."""
    now = datetime.datetime.now(datetime.timezone.utc)
    target = now.replace(hour=SYNC_HOUR_UTC, minute=0, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


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
    if not state.get("last_synced_date"):
        # First-ever run: full historical backfill
        print("usage_sync: no watermark found, starting historical backfill...")
        try:
            await _run_sync(is_backfill=True)
            print("usage_sync: backfill complete")
        except Exception as e:
            print(f"usage_sync: backfill error (non-fatal): {e}")
    else:
        # Check if we missed a scheduled run (last sync > 24h ago).
        # This handles restarts after the 2 AM window was missed.
        last_sync_at = state.get("last_sync_at")
        needs_catchup = False
        if last_sync_at:
            try:
                last_dt = datetime.datetime.fromisoformat(last_sync_at)
                hours_since = (datetime.datetime.now(datetime.timezone.utc) - last_dt).total_seconds() / 3600
                if hours_since > 24:
                    needs_catchup = True
                    print(f"usage_sync: last sync was {hours_since:.1f}h ago, running catch-up sync...")
            except Exception:
                needs_catchup = True

        if needs_catchup:
            try:
                await _run_sync(is_backfill=False)
                print("usage_sync: catch-up sync complete")
            except Exception as e:
                print(f"usage_sync: catch-up sync error (non-fatal): {e}")
        else:
            print(f"usage_sync: watermark found (last synced: {state['last_synced_date']}), next run at {SYNC_HOUR_UTC:02d}:00 UTC")

    # Daily loop
    while True:
        wait = await _seconds_until_next_run()
        print(f"usage_sync: next sync in {wait / 3600:.1f}h (at {SYNC_HOUR_UTC:02d}:00 UTC)")
        await asyncio.sleep(wait)
        try:
            await _run_sync(is_backfill=False)
        except Exception as e:
            print(f"usage_sync: daily sync error (will retry tomorrow): {e}")
