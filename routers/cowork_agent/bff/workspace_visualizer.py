"""Workspace-scope BFF endpoints over ``~/xo-projects/.xo/``.

Aggregate-of-all-projects view. Same wire contract as the project-
scope endpoints; the aggregation is "union of all per-project files"
done by the watcher (see docs/watcher-design.md §3.2). Until Phase 2,
the watcher hasn't materialised ``~/xo-projects/.xo/*`` yet, so this
module computes the workspace aggregates on the fly from per-project
``sessionslist.json`` (already adapter-written today). After Phase 2,
the same routes will read pre-materialised workspace files — no API
change.

Phase 1 ships three MVP endpoints from §13.0:

* ``/usage/summary/card``
* ``/usage/analytics``
* ``/usage/sessions``

The remaining four workspace endpoints (``/usage/summary``,
``/usage/sessions/{id}``, ``/activity``, ``/timeline``) ship in
§13.1.6.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from routers.cowork_agent.bff._visualizer_models import (
    ActivityResponse,
    AnalyticsStats,
    CostAndTokensEntry,
    DailyCostEntry,
    MessageCounts,
    MessagesEntry,
    ModelUsageEntry,
    OpenSession,
    PerformanceEntry,
    SessionCostSummary,
    SessionListItem,
    SessionListResponse,
    TimelineEvent,
    TimelineResponse,
    ToolUsage,
    UsageAnalyticsResponse,
    UsageSummaryCardResponse,
)
from services.cowork_agent import scopes
from services.cowork_agent.visualizer.workspace_index import list_project_ids

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _bad_query(message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"code": "invalid_query", "message": message},
    )


def _date_from_ms(epoch_ms: Optional[int]) -> Optional[str]:
    if not epoch_ms:
        return None
    return (
        datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
        .strftime("%Y-%m-%d")
    )


def _all_projects() -> list[str]:
    """Names of every project the watcher tracks (scaffolded + bare).

    Workspace endpoints fan out over this list. We mirror the
    watcher's ``workspace_index.list_project_ids`` to keep BFF
    visibility aligned with what the watcher writes — otherwise a
    bare project with adapter-written ``sessionslist.json`` (the
    chat router can create one before scaffolding completes) would
    be invisible to the workspace endpoints."""
    return list_project_ids()


def _union_sessionslist() -> dict[str, dict]:
    """Union of every project's merged ``sessionslist`` map.

    Keys remain composite (e.g. ``claude:blackhole:web:67a1ac06``);
    duplicate keys across projects are impossible because the
    composite key embeds the agent id, and adapters allocate unique
    8-hex suffixes.
    """
    merged: dict[str, dict] = {}
    for pid in _all_projects():
        scope = scopes.resolve_scope("xo-projects-visualizer", pid)
        rows = scope.read_sessionslist()
        for key, row in rows.items():
            # Tag the row with its project for the workspace endpoints.
            r = dict(row)
            r["_project_id"] = pid
            merged[key] = r
    return merged


def _zero_filled_dates(days: int) -> list[str]:
    today = datetime.now(timezone.utc)
    return [
        (today - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        for i in range(days)
    ]


def _rolling_key_for(days: int) -> str:
    """Pick the watcher's rolling window that best matches the request."""
    return "30d" if days > 7 else "7d"


def _tokens_from_stats(stats: dict, days: int) -> int:
    """Sum input+output tokens from a stats.json rolling window.
    No cache_read / cache_creation — matches what users see in the UI.
    """
    rolling = (stats.get("rolling") or {}).get(_rolling_key_for(days)) or {}
    t = rolling.get("tokens") or {}
    return int(t.get("input", 0) or 0) + int(t.get("output", 0) or 0)


# ── /api/xo-projects/usage/summary/card ──────────────────────────────────────


@router.get(
    "/api/xo-projects/usage/summary/card",
    response_model=UsageSummaryCardResponse,
)
def workspace_usage_summary_card(
    days: int = Query(5, ge=1, le=365),
) -> UsageSummaryCardResponse:
    """Workspace-wide usage card (all projects combined).

    Token totals come from the watcher's workspace ``stats.json``
    (input + output, no cache_read / cache_creation) — same number
    users see in the UI. Message totals come from each session's
    augment counter summed. Daily breakdown is single-bucket by
    session ``updatedAt``; true per-day roll-up is Phase 3 polish.
    """
    workspace = scopes.resolve_scope("xo-workspace-visualizer")
    stats = workspace.read_stats() or {}
    total_tokens = _tokens_from_stats(stats, days)

    sessionslist = _union_sessionslist()
    total_messages = 0
    buckets: dict[str, dict[str, int | float]] = {}
    for row in sessionslist.values():
        msgs = int(row.get("messageCount", 0) or 0)
        total_messages += msgs
        d = _date_from_ms(row.get("updatedAt"))
        if d is not None:
            usage = row.get("usage") or {}
            tk_row = int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
            b = buckets.setdefault(d, {"cost": 0.0, "tokens": 0, "messages": 0})
            b["tokens"] += tk_row
            b["messages"] += msgs

    daily = [
        DailyCostEntry(
            date=d,
            cost=round(float((buckets.get(d) or {"cost": 0.0})["cost"]), 6),
            tokens=int((buckets.get(d) or {"tokens": 0})["tokens"]),
            messages=int((buckets.get(d) or {"messages": 0})["messages"]),
        )
        for d in _zero_filled_dates(days)
    ]
    return UsageSummaryCardResponse(
        days=days,
        totalCost=0.0,  # cost-per-token table is Phase 3 polish
        totalMessages=total_messages,
        totalTokens=total_tokens,
        dailyCost=daily,
    )


# ── /api/xo-projects/usage/analytics ─────────────────────────────────────────


@router.get(
    "/api/xo-projects/usage/analytics",
    response_model=UsageAnalyticsResponse,
)
def workspace_usage_analytics(
    days: Optional[int] = Query(None, ge=1, le=365),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
) -> UsageAnalyticsResponse:
    """Workspace-wide analytics dashboard.

    Mirrors ``/openclaw/usage/analytics`` shape verbatim. Phase 1 fills
    the top-level ``stats`` (totals) and zero-fills the daily/tool/
    model arrays; Phase 2's watcher will populate the granular detail
    via ``stats.json`` aggregation.
    """
    # Date-range parse — keep error codes consistent with
    # routers/openclaw_usage.py.
    try:
        if start:
            datetime.strptime(start, "%Y-%m-%d")
        if end:
            datetime.strptime(end, "%Y-%m-%d")
    except ValueError as exc:
        raise _bad_query("start / end must be YYYY-MM-DD") from exc

    window_days = days or 5
    workspace = scopes.resolve_scope("xo-workspace-visualizer")
    stats = workspace.read_stats() or {}
    total_tokens = _tokens_from_stats(stats, window_days)

    sessionslist = _union_sessionslist()
    total_messages = sum(int(r.get("messageCount", 0) or 0) for r in sessionslist.values())

    zero_dates = _zero_filled_dates(window_days)

    return UsageAnalyticsResponse(
        stats=AnalyticsStats(
            totalCost=0.0,
            totalTokens=total_tokens,
            totalMessages=total_messages,
            avgLatencyMs=0,
        ),
        costAndTokens=[CostAndTokensEntry(date=d, tokens=0, cost=0.0) for d in zero_dates],
        messages=[
            MessagesEntry(date=d, total=0, user=0, assistant=0, toolCalls=0)
            for d in zero_dates
        ],
        performance=[
            PerformanceEntry(date=d, avgMs=0, p95Ms=0, minMs=0, maxMs=0)
            for d in zero_dates
        ],
        toolUsage=ToolUsage(totalCalls=0, uniqueTools=0, tools=[]),
        modelUsage=[],
    )


# ── /api/xo-projects/usage/sessions ──────────────────────────────────────────


@router.get(
    "/api/xo-projects/usage/sessions",
    response_model=SessionListResponse,
)
def workspace_usage_sessions() -> SessionListResponse:
    """Workspace-wide session list.

    One row per session across all projects; each row tagged with
    ``projectId``. Mirrors ``/openclaw/usage/sessions`` shape with the
    extra ``projectId`` discriminator.
    """
    sessionslist = _union_sessionslist()

    items: list[SessionListItem] = []
    for composite_key, row in sessionslist.items():
        usage = row.get("usage") or {}
        total_tokens = (
            int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("output_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
        )
        # ``nativeSessionId`` → ``sessionFile`` (basename, no path — P1).
        native = row.get("nativeSessionId") or ""
        session_file = f"{native}.jsonl" if native else ""
        items.append(
            SessionListItem(
                sessionId=composite_key,
                sessionFile=session_file,
                messageCount=int(row.get("messageCount", 0) or 0),
                totalTokens=total_tokens,
                totalCost=0.0,
                firstActivity=row.get("firstActivity"),
                lastActivity=row.get("lastActivity") or row.get("updatedAt"),
                projectId=row.get("_project_id"),
            )
        )

    items.sort(key=lambda s: s.lastActivity or 0, reverse=True)
    return SessionListResponse(agentId=None, count=len(items), sessions=items)


# ── /api/xo-projects/usage/summary ───────────────────────────────────────────


def _row_total_tokens(usage: dict) -> int:
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )


def _row_to_summary(composite_key: str, row: dict) -> SessionCostSummary:
    usage = row.get("usage") or {}
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
    cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
    native = row.get("nativeSessionId") or ""
    return SessionCostSummary(
        sessionId=composite_key,
        sessionFile=f"{native}.jsonl" if native else "",
        firstActivity=row.get("firstActivity") or row.get("updatedAt"),
        lastActivity=row.get("lastActivity") or row.get("updatedAt"),
        input=inp, output=out, cacheRead=cr, cacheWrite=cw,
        totalTokens=inp + out + cr + cw,
        messageCounts=MessageCounts(
            total=int(row.get("messageCount", 0) or 0),
            user=0, assistant=0,
            toolCalls=int(row.get("toolCallCount", 0) or 0),
            toolResults=0, errors=0,
        ),
        toolUsage=ToolUsage(totalCalls=0, uniqueTools=0, tools=[]),
        modelUsage=[],
    )


@router.get(
    "/api/xo-projects/usage/summary",
    response_model=SessionCostSummary,
)
def workspace_usage_summary(
    days: Optional[int] = Query(None, ge=1, le=365),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
) -> SessionCostSummary:
    """Workspace-wide aggregate ``SessionCostSummary``.

    Mirrors ``/openclaw/usage/summary`` shape. ``sessionId`` is the
    sentinel ``"all-projects"``; ``sessions[]`` lists every session
    across every project.
    """
    try:
        if start:
            datetime.strptime(start, "%Y-%m-%d")
        if end:
            datetime.strptime(end, "%Y-%m-%d")
    except ValueError as exc:
        raise _bad_query("start / end must be YYYY-MM-DD") from exc

    sessionslist = _union_sessionslist()
    per_session = [_row_to_summary(k, r) for k, r in sessionslist.items()]
    inp = sum(s.input for s in per_session)
    out = sum(s.output for s in per_session)
    cr = sum(s.cacheRead for s in per_session)
    cw = sum(s.cacheWrite for s in per_session)
    msgs = sum(s.messageCounts.total for s in per_session)

    return SessionCostSummary(
        sessionId="all-projects",
        sessionFile=f"{len(per_session)} files",
        firstActivity=min((s.firstActivity for s in per_session if s.firstActivity), default=None),
        lastActivity=max((s.lastActivity for s in per_session if s.lastActivity), default=None),
        input=inp, output=out, cacheRead=cr, cacheWrite=cw,
        totalTokens=inp + out + cr + cw,
        messageCounts=MessageCounts(total=msgs, user=0, assistant=0, toolCalls=0, toolResults=0, errors=0),
        toolUsage=ToolUsage(totalCalls=0, uniqueTools=0, tools=[]),
        modelUsage=[],
        sessionCount=len(per_session),
        sessions=per_session,
    )


# ── /api/xo-projects/usage/sessions/{session_id} ─────────────────────────────


@router.get(
    "/api/xo-projects/usage/sessions/{session_id}",
    response_model=SessionCostSummary,
)
def workspace_usage_one_session(session_id: str) -> SessionCostSummary:
    """Single-session detail across the whole workspace.

    Scans every project's sessionslist until a match is found by
    composite key or ``nativeSessionId``.
    """
    for pid in _all_projects():
        scope = scopes.resolve_scope("xo-projects-visualizer", pid)
        found = scope.read_one_session(session_id)
        if found is not None:
            composite_key, row = found
            return _row_to_summary(composite_key, row)
    raise HTTPException(
        status_code=404,
        detail={"code": "session_not_found", "message": "Session not found."},
    )


# ── /api/xo-projects/activity ────────────────────────────────────────────────


@router.get(
    "/api/xo-projects/activity",
    response_model=ActivityResponse,
)
def workspace_activity() -> ActivityResponse:
    """Workspace-wide live presence — union of every project's open sessions.

    Empty until Phase 2 watcher writes per-project ``activity.json``.
    Each open-session row carries ``project_id`` so the UI can group
    by project.
    """
    open_sessions: list[OpenSession] = []
    for pid in _all_projects():
        scope = scopes.resolve_scope("xo-projects-visualizer", pid)
        raw = scope.read_activity()
        if not raw:
            continue
        for s in raw.get("open_sessions") or []:
            if not isinstance(s, dict):
                continue
            try:
                open_sessions.append(
                    OpenSession(
                        session_id=str(s["session_id"]),
                        runtime=s.get("runtime"),
                        agent=str(s["agent"]),
                        user_id=str(s["user_id"]),
                        opened_at=str(s["opened_at"]),
                        last_activity_at=str(s["last_activity_at"]),
                        host=s.get("host"),
                        project_id=pid,
                    )
                )
            except (KeyError, ValueError):
                continue
    return ActivityResponse(project_id=None, updated_at=None, open_sessions=open_sessions)


# ── /api/xo-projects/timeline ────────────────────────────────────────────────


_TIMELINE_TYPES = frozenset({
    "project.created", "session.started", "session.closed",
    "todo.added", "todo.completed",
    "file.edited", "file.created",
    "plan.written", "episode.written",
    "peer.sync.started", "peer.sync.applied", "peer.sync.conflict",
})


def _parse_types_param(types: Optional[str]) -> Optional[frozenset[str]]:
    if types is None:
        return None
    requested = {t.strip() for t in types.split(",") if t.strip()}
    unknown = requested - _TIMELINE_TYPES
    if unknown:
        raise _bad_query(f"unknown timeline type(s): {sorted(unknown)}")
    return frozenset(requested)


@router.get(
    "/api/xo-projects/timeline",
    response_model=TimelineResponse,
)
def workspace_timeline(
    limit: int = Query(100, ge=1, le=500),
    before: Optional[str] = Query(None),
    types: Optional[str] = Query(None),
) -> TimelineResponse:
    """Multiplexed workspace timeline. Each event tagged with ``project_id``.

    Reads from ``~/xo-projects/.xo/timeline.jsonl`` once Phase 2's
    workspace tier materialises it. Until then returns empty.
    """
    if before is not None:
        try:
            datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _bad_query("before must be an ISO-8601 timestamp") from exc

    type_set = _parse_types_param(types)
    workspace = scopes.resolve_scope("xo-workspace-visualizer")
    events = workspace.read_timeline(limit=limit, before=before, types=type_set)

    out: list[TimelineEvent] = []
    for ev in events:
        try:
            out.append(TimelineEvent(**ev))
        except Exception:
            continue

    next_cursor = out[-1].ts if len(out) == limit else None
    return TimelineResponse(project_id=None, events=out, next_cursor=next_cursor)
