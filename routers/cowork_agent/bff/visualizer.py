"""Project-scope BFF endpoints over ``<project>/.xo/``.

This module never imports ``os`` or ``pathlib`` (P2). All filesystem
reads happen behind ``services.cowork_agent.scopes.VisualizerScope``,
which delegates to ``services.cowork_agent.visualizer.reader``. See
docs/watcher-design.md §6.

Phase 1 ships six endpoints with empty/zero state where Phase 2's
watcher has not yet written the backing files:

* ``/usage/summary/card`` — totals from adapter-owned
  ``sessionslist.json``; daily breakdown is single-bucket by session
  ``updatedAt`` (true daily roll-up comes from the watcher's
  ``stats.json`` in Phase 2).
* ``/todos`` — empty until the watcher writes ``todos.json``.
* ``/activity`` — empty until the watcher writes ``activity.json``.

The remaining five project-scope endpoints (``/usage/analytics``,
``/usage/summary``, ``/usage/sessions``, ``/usage/sessions/{id}``,
``/timeline``) ship in §13.1.6.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from routers.cowork_agent.bff._visualizer_models import (
    ActivityResponse,
    AnalyticsStats,
    CostAndTokensEntry,
    CreateTodoRequest,
    DailyCostEntry,
    DeleteTodoResponse,
    MessageCounts,
    MessagesEntry,
    ModelUsageEntry,
    OpenSession,
    PerformanceEntry,
    SessionCostSummary,
    SessionListItem,
    SessionListResponse,
    SessionTodos,
    TimelineEvent,
    TimelineResponse,
    Todo,
    TodosResponse,
    ToolUsage,
    UpdateTodoRequest,
    UsageAnalyticsResponse,
    UsageSummaryCardResponse,
)
from services.cowork_agent import scopes

router = APIRouter()


# ── Common helpers ────────────────────────────────────────────────────────────


def _require_project(project_id: str) -> scopes.VisualizerScope:
    """Resolve a project-scope visualizer handle or 404."""
    scope = scopes.resolve_scope("xo-projects-visualizer", project_id)
    if not scope.project_exists():
        raise HTTPException(
            status_code=404,
            detail={"code": "project_not_found", "message": "Project not found."},
        )
    return scope


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


# ── /api/xo-projects/{id}/usage/summary/card ─────────────────────────────────


def _sum_session_totals(sessionslist: dict[str, dict]) -> tuple[int, int]:
    """Return (totalTokens, totalMessages) summed across rows.

    Tokens are read from the adapter-owned ``usage`` block. Message
    count comes from the watcher-augment ``messageCount`` field if
    present (Phase 2), else ``0``.
    """
    total_tokens = 0
    total_messages = 0
    for row in sessionslist.values():
        usage = row.get("usage") or {}
        total_tokens += int(usage.get("input_tokens", 0) or 0)
        total_tokens += int(usage.get("output_tokens", 0) or 0)
        total_messages += int(row.get("messageCount", 0) or 0)
    return total_tokens, total_messages


def _bucket_daily_cost(
    sessionslist: dict[str, dict], *, days: int
) -> list[DailyCostEntry]:
    """Build the ``dailyCost`` array, zero-filled for the requested
    window, sorted oldest→newest.

    Phase 1 limitation: one bucket per session, keyed by the session's
    ``updatedAt`` date. Phase 2's ``stats.json`` will replace this with
    a true per-event roll-up. Totals are correct either way.
    """
    buckets: dict[str, dict[str, int | float]] = {}
    for row in sessionslist.values():
        d = _date_from_ms(row.get("updatedAt"))
        if d is None:
            continue
        b = buckets.setdefault(d, {"cost": 0.0, "tokens": 0, "messages": 0})
        usage = row.get("usage") or {}
        b["tokens"] += int(usage.get("input_tokens", 0) or 0)
        b["tokens"] += int(usage.get("output_tokens", 0) or 0)
        b["messages"] += int(row.get("messageCount", 0) or 0)
        # Cost stays 0 for Claude Code in v1 (no cost model — see
        # docs/watcher-design.md §8.3).

    today = datetime.now(timezone.utc)
    out: list[DailyCostEntry] = []
    for i in range(days):
        d = (today.timestamp() - (days - 1 - i) * 86400)
        date_str = datetime.fromtimestamp(d, tz=timezone.utc).strftime("%Y-%m-%d")
        b = buckets.get(date_str, {"cost": 0.0, "tokens": 0, "messages": 0})
        out.append(
            DailyCostEntry(
                date=date_str,
                cost=round(float(b["cost"]), 6),
                tokens=int(b["tokens"]),
                messages=int(b["messages"]),
            )
        )
    return out


@router.get(
    "/api/xo-projects/{project_id}/usage/summary/card",
    response_model=UsageSummaryCardResponse,
)
def project_usage_summary_card(
    project_id: str,
    days: int = Query(5, ge=1, le=365),
) -> UsageSummaryCardResponse:
    """Lightweight usage widget for one project.

    Token totals come from the watcher's per-project ``stats.json``
    (input + output, no cache_read / cache_creation) — same number
    users see in the UI. Message totals come from the per-session
    augment counters. Daily breakdown still buckets by session
    ``updatedAt`` for now.
    """
    scope = _require_project(project_id)
    stats = scope.read_stats() or {}
    total_tokens = _tokens_from_stats(stats, days)
    sessionslist = scope.read_sessionslist()
    _, total_messages = _sum_session_totals(sessionslist)
    return UsageSummaryCardResponse(
        days=days,
        totalCost=0.0,  # cost-per-token table is Phase 3 polish
        totalMessages=total_messages,
        totalTokens=total_tokens,
        dailyCost=_bucket_daily_cost(sessionslist, days=days),
    )


# ── /api/xo-projects/{id}/todos ──────────────────────────────────────────────


def _shape_todos(project_id: str, raw: Optional[dict]) -> TodosResponse:
    """Convert the on-disk ``todos.json`` shape to the wire shape.

    On-disk:  {schema, updated_at, sessions: {sid: {runtime, source_file,
                session_started_at, todos: [{id, content, status, ...}]}}}
    On wire:  {project_id, updated_at, sessions: {sid: SessionTodos}}

    Pydantic's ``extra="forbid"`` on ``Todo`` is the wire allowlist —
    unexpected keys raise 500 ``scope_unavailable`` (we'd rather fail
    closed than leak a watcher mistake).
    """
    if not raw:
        return TodosResponse(project_id=project_id, updated_at=None, sessions={})

    out_sessions: dict[str, SessionTodos] = {}
    for sid, entry in (raw.get("sessions") or {}).items():
        if not isinstance(entry, dict):
            continue
        todos: list[Todo] = []
        for t in entry.get("todos") or []:
            if not isinstance(t, dict):
                continue
            todos.append(
                Todo(
                    id=str(t.get("id", "")),
                    content=str(t.get("content", "")),
                    status=str(t.get("status", "pending")),
                    description=t.get("description"),
                    active_form=t.get("active_form"),
                )
            )
        out_sessions[str(sid)] = SessionTodos(
            runtime=str(entry.get("runtime", "")),
            source_file=None,  # P1: never echo absolute paths back (§3.4.1)
            session_started_at=entry.get("session_started_at"),
            todos=todos,
        )

    return TodosResponse(
        project_id=project_id,
        updated_at=raw.get("updated_at"),
        sessions=out_sessions,
    )


@router.get(
    "/api/xo-projects/{project_id}/todos",
    response_model=TodosResponse,
)
def project_todos(project_id: str) -> TodosResponse:
    """Per-session task list for one project.

    Empty ``{sessions: {}}`` until the Phase 2 watcher writes
    ``todos.json``. Adapter-written ``sessionslist.json`` is NOT a
    source for todos — todos live exclusively in the watcher-derived
    ``todos.json``.
    """
    scope = _require_project(project_id)
    try:
        raw = scope.read_todos()
    except Exception as exc:  # malformed JSON — fail closed
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable",
                    "message": "todos.json is not readable."},
        ) from exc
    return _shape_todos(project_id, raw)


# ── /api/xo-projects/{id}/todos — CRUD for any runtime ──────────────────────
#
# Agents (OpenClaw / Hermes / future runtimes) write todos via this API
# rather than touching .xo/todos.json directly. The handle's CRUD methods
# delegate to services.cowork_agent.visualizer.todos_store, which shares
# a flock with the watcher's todos sink so writes never tear each other.
# See docs/visualizer-overview.md for the full contract.


def _make_todo_model(d: dict) -> Todo:
    return Todo(
        id=str(d.get("id", "")),
        content=str(d.get("content", "")),
        status=str(d.get("status", "pending")),
        description=d.get("description"),
        active_form=d.get("active_form"),
    )


@router.post(
    "/api/xo-projects/{project_id}/todos",
    response_model=Todo,
    status_code=201,
)
def project_todos_create(project_id: str, body: CreateTodoRequest) -> Todo:
    """Create a new todo under the project (any runtime can call).

    ``session_id`` defaults to the ``"_project"`` pseudo-session so
    callers without a session concept don't have to invent one.
    """
    scope = _require_project(project_id)
    try:
        new = scope.create_todo(
            runtime=body.runtime,
            content=body.content,
            description=body.description,
            active_form=body.active_form,
            session_id=body.session_id,
            status=body.status,
        )
    except Exception as exc:
        # Surface validation errors from todos_store as 400; the store
        # raises TodosStoreError with .code set to a stable machine code.
        code = getattr(exc, "code", None)
        if code in {"invalid_runtime", "invalid_session_id", "invalid_value", "invalid_status"}:
            raise HTTPException(
                status_code=400,
                detail={"code": code, "message": str(exc)},
            ) from exc
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable", "message": "todos.json write failed."},
        ) from exc
    return _make_todo_model(new)


@router.get(
    "/api/xo-projects/{project_id}/todos/{todo_id}",
    response_model=Todo,
)
def project_todos_get(project_id: str, todo_id: str) -> Todo:
    """Fetch one todo by id."""
    scope = _require_project(project_id)
    found = scope.get_todo(todo_id)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "todo_not_found", "message": "Todo not found."},
        )
    _, todo = found
    return _make_todo_model(todo)


@router.patch(
    "/api/xo-projects/{project_id}/todos/{todo_id}",
    response_model=Todo,
)
def project_todos_update(
    project_id: str, todo_id: str, body: UpdateTodoRequest,
) -> Todo:
    """Update fields on an existing todo. Most common: status transition
    ``pending`` → ``in_progress`` → ``completed``.
    """
    scope = _require_project(project_id)
    try:
        updated = scope.update_todo(
            todo_id,
            status=body.status,
            content=body.content,
            description=body.description,
            active_form=body.active_form,
        )
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code == "todo_not_found":
            raise HTTPException(
                status_code=404,
                detail={"code": "todo_not_found", "message": "Todo not found."},
            ) from exc
        if code in {"invalid_status", "invalid_value"}:
            raise HTTPException(
                status_code=400,
                detail={"code": code, "message": str(exc)},
            ) from exc
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable", "message": "todos.json write failed."},
        ) from exc
    return _make_todo_model(updated)


@router.delete(
    "/api/xo-projects/{project_id}/todos/{todo_id}",
    response_model=DeleteTodoResponse,
)
def project_todos_delete(project_id: str, todo_id: str) -> DeleteTodoResponse:
    """Idempotent delete — returns ``deleted: false`` if the todo
    was already absent (never 404, matches /api/secrets/{key} pattern)."""
    scope = _require_project(project_id)
    try:
        deleted = scope.delete_todo(todo_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable", "message": "todos.json write failed."},
        ) from exc
    return DeleteTodoResponse(project_id=project_id, todo_id=todo_id, deleted=deleted)


# ── /api/xo-projects/{id}/activity ───────────────────────────────────────────


def _shape_activity(project_id: str, raw: Optional[dict]) -> ActivityResponse:
    if not raw:
        return ActivityResponse(project_id=project_id, updated_at=None, open_sessions=[])

    open_sessions: list[OpenSession] = []
    for s in raw.get("open_sessions") or []:
        if not isinstance(s, dict):
            continue
        # Pydantic's extra="forbid" handles the allowlist; we only
        # pre-check required keys (schema requires them but a fresh-
        # boot empty file may omit). Missing required field → skip
        # the row.
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
                )
            )
        except (KeyError, ValueError):
            continue

    return ActivityResponse(
        project_id=project_id,
        updated_at=raw.get("updated_at"),
        open_sessions=open_sessions,
    )


@router.get(
    "/api/xo-projects/{project_id}/activity",
    response_model=ActivityResponse,
)
def project_activity(project_id: str) -> ActivityResponse:
    """Live presence — which sessions are open in this project right now.

    Empty ``{open_sessions: []}`` until the Phase 2 watcher writes
    ``activity.json``. AGENTS.md §4 (boot ritual) reads this file to
    answer "is anyone else working here right now?".
    """
    scope = _require_project(project_id)
    try:
        raw = scope.read_activity()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "scope_unavailable",
                    "message": "activity.json is not readable."},
        ) from exc
    return _shape_activity(project_id, raw)


# ── Shared helpers for the usage-aggregation routes ──────────────────────────


def _zero_filled_dates(days: int) -> list[str]:
    from datetime import timedelta
    today = datetime.now(timezone.utc)
    return [
        (today - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        for i in range(days)
    ]


def _tokens_from_stats(stats: dict, days: int) -> int:
    """Sum input+output tokens from a stats.json rolling window.
    No cache_read / cache_creation — matches what users see in the UI.
    """
    window = "30d" if days > 7 else "7d"
    rolling = (stats.get("rolling") or {}).get(window) or {}
    t = rolling.get("tokens") or {}
    return int(t.get("input", 0) or 0) + int(t.get("output", 0) or 0)


def _row_total_tokens(usage: dict) -> int:
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )


def _row_to_list_item(composite_key: str, row: dict, *, project_id: Optional[str]) -> SessionListItem:
    """One sessionslist row → ``SessionListItem`` (no path leakage)."""
    usage = row.get("usage") or {}
    native = row.get("nativeSessionId") or ""
    return SessionListItem(
        sessionId=composite_key,
        sessionFile=f"{native}.jsonl" if native else "",
        messageCount=int(row.get("messageCount", 0) or 0),
        totalTokens=_row_total_tokens(usage),
        totalCost=0.0,
        firstActivity=row.get("firstActivity"),
        lastActivity=row.get("lastActivity") or row.get("updatedAt"),
        projectId=project_id,
    )


def _aggregate_session_summary(
    composite_key: str, row: dict, *, single_session: bool
) -> SessionCostSummary:
    """Build a ``SessionCostSummary`` from one sessionslist row.

    Phase 1: token totals are real (from adapter ``usage``); cost,
    per-day, per-tool, per-model breakdowns are zero/empty until
    Phase 2 writes the full daily roll-up. Wire shape matches
    ``/openclaw/usage/summary`` field-for-field.
    """
    usage = row.get("usage") or {}
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
    total = inp + out + cache_read + cache_write
    native = row.get("nativeSessionId") or ""

    return SessionCostSummary(
        sessionId=composite_key if single_session else "all",
        sessionFile=f"{native}.jsonl" if (single_session and native) else "1 file",
        firstActivity=row.get("firstActivity") or row.get("updatedAt"),
        lastActivity=row.get("lastActivity") or row.get("updatedAt"),
        durationMs=None,
        activityDates=[],
        input=inp,
        output=out,
        cacheRead=cache_read,
        cacheWrite=cache_write,
        totalTokens=total,
        totalCost=0.0,
        inputCost=0.0,
        outputCost=0.0,
        cacheReadCost=0.0,
        cacheWriteCost=0.0,
        missingCostEntries=0,
        dailyBreakdown=[],
        dailyLatency=[],
        dailyModelUsage=[],
        messageCounts=MessageCounts(
            total=int(row.get("messageCount", 0) or 0),
            user=0,
            assistant=0,
            toolCalls=int(row.get("toolCallCount", 0) or 0),
            toolResults=0,
            errors=0,
        ),
        toolUsage=ToolUsage(totalCalls=0, uniqueTools=0, tools=[]),
        modelUsage=[],
    )


def _bucket_by_date(sessionslist: dict[str, dict]) -> dict[str, dict[str, int | float]]:
    buckets: dict[str, dict[str, int | float]] = {}
    for row in sessionslist.values():
        d = _date_from_ms(row.get("updatedAt"))
        if d is None:
            continue
        b = buckets.setdefault(d, {"tokens": 0, "cost": 0.0})
        b["tokens"] += _row_total_tokens(row.get("usage") or {})
    return buckets


# ── /api/xo-projects/{id}/usage/analytics ────────────────────────────────────


@router.get(
    "/api/xo-projects/{project_id}/usage/analytics",
    response_model=UsageAnalyticsResponse,
)
def project_usage_analytics(
    project_id: str,
    days: Optional[int] = Query(None, ge=1, le=365),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
) -> UsageAnalyticsResponse:
    """Per-project analytics dashboard. Shape mirrors ``/openclaw/usage/analytics``.

    Phase 1 fills ``stats`` (totals) from ``sessionslist.json``;
    daily/tool/model arrays are zero-filled until Phase 2 writes
    ``stats.json``.
    """
    try:
        if start:
            datetime.strptime(start, "%Y-%m-%d")
        if end:
            datetime.strptime(end, "%Y-%m-%d")
    except ValueError as exc:
        raise _bad_query("start / end must be YYYY-MM-DD") from exc

    scope = _require_project(project_id)
    stats = scope.read_stats() or {}
    window_days = days or 5
    total_tokens = _tokens_from_stats(stats, window_days)
    sessionslist = scope.read_sessionslist()
    _, total_messages = _sum_session_totals(sessionslist)
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


# ── /api/xo-projects/{id}/usage/sessions ─────────────────────────────────────


@router.get(
    "/api/xo-projects/{project_id}/usage/sessions",
    response_model=SessionListResponse,
)
def project_usage_sessions(
    project_id: str,
    agent_id: Optional[str] = Query(None),
) -> SessionListResponse:
    """List sessions for one project. Mirrors ``/openclaw/usage/sessions``."""
    scope = _require_project(project_id)
    sessionslist = scope.read_sessionslist()

    items: list[SessionListItem] = []
    for composite_key, row in sessionslist.items():
        if agent_id:
            # Composite key shape is "<backend>:<agent_id>:<surface>:<8hex>"
            parts = composite_key.split(":")
            if len(parts) < 2 or parts[1] != agent_id:
                continue
        items.append(_row_to_list_item(composite_key, row, project_id=None))

    items.sort(key=lambda s: s.lastActivity or 0, reverse=True)
    return SessionListResponse(agentId=agent_id, count=len(items), sessions=items)


# ── /api/xo-projects/{id}/usage/summary ──────────────────────────────────────


@router.get(
    "/api/xo-projects/{project_id}/usage/summary",
    response_model=SessionCostSummary,
)
def project_usage_summary(
    project_id: str,
    days: Optional[int] = Query(None, ge=1, le=365),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
) -> SessionCostSummary:
    """Aggregate ``SessionCostSummary`` across this project's sessions.

    Mirrors ``/openclaw/usage/summary``: returns the combined summary
    plus a ``sessions[]`` array with per-session sub-summaries.
    """
    try:
        if start:
            datetime.strptime(start, "%Y-%m-%d")
        if end:
            datetime.strptime(end, "%Y-%m-%d")
    except ValueError as exc:
        raise _bad_query("start / end must be YYYY-MM-DD") from exc

    scope = _require_project(project_id)
    sessionslist = scope.read_sessionslist()

    per_session = [
        _aggregate_session_summary(k, r, single_session=True)
        for k, r in sessionslist.items()
    ]

    # Combined totals
    inp = sum(s.input for s in per_session)
    out = sum(s.output for s in per_session)
    cr = sum(s.cacheRead for s in per_session)
    cw = sum(s.cacheWrite for s in per_session)
    msgs = sum(s.messageCounts.total for s in per_session)

    return SessionCostSummary(
        sessionId="all",
        sessionFile=f"{len(per_session)} files",
        firstActivity=min((s.firstActivity for s in per_session if s.firstActivity), default=None),
        lastActivity=max((s.lastActivity for s in per_session if s.lastActivity), default=None),
        durationMs=None,
        activityDates=[],
        input=inp, output=out, cacheRead=cr, cacheWrite=cw,
        totalTokens=inp + out + cr + cw,
        totalCost=0.0, inputCost=0.0, outputCost=0.0,
        cacheReadCost=0.0, cacheWriteCost=0.0, missingCostEntries=0,
        dailyBreakdown=[], dailyLatency=[], dailyModelUsage=[],
        messageCounts=MessageCounts(total=msgs, user=0, assistant=0, toolCalls=0, toolResults=0, errors=0),
        toolUsage=ToolUsage(totalCalls=0, uniqueTools=0, tools=[]),
        modelUsage=[],
        sessionCount=len(per_session),
        sessions=per_session,
    )


# ── /api/xo-projects/{id}/usage/sessions/{session_id} ────────────────────────


@router.get(
    "/api/xo-projects/{project_id}/usage/sessions/{session_id}",
    response_model=SessionCostSummary,
)
def project_usage_one_session(
    project_id: str,
    session_id: str,
) -> SessionCostSummary:
    """Single-session detail. Mirrors ``/openclaw/usage/sessions/{sid}``.

    ``session_id`` accepts either the composite key
    (``claude:blackhole:web:67a1ac06``) or the ``nativeSessionId``
    (``aa4b140b-…``) — same dual-id lookup as
    ``services/cowork_agent/sessions_io.py:107``.
    """
    scope = _require_project(project_id)
    found = scope.read_one_session(session_id)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "session_not_found", "message": "Session not found."},
        )
    composite_key, row = found
    return _aggregate_session_summary(composite_key, row, single_session=True)


# ── /api/xo-projects/{id}/timeline ───────────────────────────────────────────


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
    "/api/xo-projects/{project_id}/timeline",
    response_model=TimelineResponse,
)
def project_timeline(
    project_id: str,
    limit: int = Query(100, ge=1, le=500),
    before: Optional[str] = Query(None),
    types: Optional[str] = Query(None),
) -> TimelineResponse:
    """Newest-first event stream for one project.

    Reads from ``<project>/.xo/timeline.jsonl``. Empty until Phase 2
    watcher emits events.
    """
    if before is not None:
        try:
            datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _bad_query("before must be an ISO-8601 timestamp") from exc

    type_set = _parse_types_param(types)
    scope = _require_project(project_id)
    events = scope.read_timeline(limit=limit, before=before, types=type_set)

    out: list[TimelineEvent] = []
    for ev in events:
        try:
            out.append(TimelineEvent(**ev))
        except Exception:
            # malformed event line — already logged by reader; skip
            continue

    next_cursor = out[-1].ts if len(out) == limit else None
    return TimelineResponse(project_id=project_id, events=out, next_cursor=next_cursor)
