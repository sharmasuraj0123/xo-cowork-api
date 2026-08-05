"""Test doubles and event factories for the watcher suite.

Imported as a top-level module — ``tests/`` has no ``__init__.py``, so pytest's
default prepend import mode puts this directory on ``sys.path``::

    from fakes import FakeSource, make_watcher, usage_observed

**The seam.** ``Watcher``'s only coupling to a specific agent lives in
``Watcher.__init__`` (``visualizer/watcher.py:73-91``): it builds an
``OffsetStore``, calls ``get_active_agent()`` and ``load_source_module()`` to
find the one source for the active ``AGENT_NAME``, and asserts the two agree.
``tick()`` itself touches the agent world through exactly two calls —
``src.poll_events()`` and ``src.poll_presence()``. So :func:`make_watcher`
bypasses ``__init__`` entirely and injects fakes for both.

That is not a hack, it is the correct boundary: a suite built on this seam never
imports an adapter's ``visualizer_source``, never constructs an ``OffsetStore``
(whose default offsets path is frozen to ``~/.xo-cowork`` at import time), and
never resolves an agent — which makes ``AGENT_NAME`` irrelevant to every tick
test, and makes the tick tests genuinely agent-agnostic rather than
"claude_code's behaviour, tested".

**The asymmetry.** ``visualizer/sources/base.py`` gives the two poll methods
different contracts, and :class:`FakeSource` preserves it:

* ``poll_events()`` **drains** — "events observed since the last call". Queue an
  event, tick twice, and the second tick sees nothing.
* ``poll_presence()`` returns a **snapshot** — "currently-open session rows",
  re-derived every call. Whatever is set stays visible until it is changed.

Getting this backwards makes the session-eviction test (activity.json must drop
a session once it stops appearing in presence) pass for the wrong reason: a
draining presence would empty itself on tick 2 regardless of the sink's logic.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from services.cowork_agent.visualizer.ingest.events import (
    Event,
    FileTouched,
    MessageObserved,
    SessionFirstSeen,
    TaskCreated,
    TaskStatusChanged,
    ToolUseObserved,
    UsageObserved,
)

# Shared defaults so a test only has to name the fields it actually cares about.
DEFAULT_TS = "2026-01-01T00:00:00Z"
DEFAULT_SESSION_ID = "sess-0001"
DEFAULT_RUNTIME = "fake_agent"
DEFAULT_PROJECT_ID = "demo"

# 2026-01-01T00:00:00Z, matching DEFAULT_TS — presence rows carry epoch millis
# while events carry ISO strings (see `_ms_to_iso` in sinks/activity.py).
DEFAULT_STARTED_AT_MS = 1767225600000


# ── The source double ────────────────────────────────────────────────────────


class FakeSource:
    """Stand-in for an adapter's ``visualizer_source.Source``.

    Implements the ``sources.base.Source`` protocol and nothing else: a ``name``
    plus the two poll methods. Constructed with an initial event queue and
    presence snapshot; both can be changed between ticks.

        src = FakeSource(events=[session_first_seen()], presence=[presence_row()])
        w = make_watcher(src)
        w.tick()                 # drains the queue, sees the presence row
        src.set_presence([])     # session exited
        w.tick()                 # queue is empty; presence is now empty

    ``events_error`` / ``presence_error`` make the corresponding poll raise, for
    the "one source blows up, the tick keeps going" paths the watcher guards
    with try/except (``watcher.py:99-102`` and ``:148-152``).
    """

    def __init__(
        self,
        *,
        name: str = DEFAULT_RUNTIME,
        events: Optional[Iterable[Event]] = None,
        presence: Optional[Iterable[dict]] = None,
        events_error: Optional[BaseException] = None,
        presence_error: Optional[BaseException] = None,
    ) -> None:
        self.name = name
        self._queue: list[Event] = list(events or [])
        self._presence: list[dict] = [dict(row) for row in (presence or [])]
        self.events_error = events_error
        self.presence_error = presence_error
        # Call counters — the eviction/idempotence tests assert on tick counts.
        self.poll_events_calls = 0
        self.poll_presence_calls = 0

    # -- queue control (test-side API, not part of the protocol) --------------

    def queue(self, *events: Event) -> "FakeSource":
        """Add events to be returned by the next ``poll_events()``."""
        self._queue.extend(events)
        return self

    def set_presence(self, rows: Iterable[dict]) -> "FakeSource":
        """Replace the presence snapshot returned by every later call."""
        self._presence = [dict(row) for row in rows]
        return self

    # -- the protocol ---------------------------------------------------------

    def poll_events(self) -> list[Event]:
        """DRAINS. Returns everything queued since the last call, then empties."""
        self.poll_events_calls += 1
        if self.events_error is not None:
            raise self.events_error
        drained, self._queue = self._queue, []
        return drained

    def poll_presence(self) -> list[dict]:
        """SNAPSHOT. Re-derived every call; unchanged by being read."""
        self.poll_presence_calls += 1
        if self.presence_error is not None:
            raise self.presence_error
        # Copy so a sink mutating a row cannot corrupt the next tick's snapshot
        # (the real sources rebuild their rows from disk each time).
        return [dict(row) for row in self._presence]


def make_watcher(*sources: Any, model_by_session: Optional[dict] = None):
    """Build a ``Watcher`` through the ``__new__`` seam (see module docstring).

    ``__init__`` is bypassed, so the only attributes that exist are the two
    ``tick()`` actually reads: ``sources`` and ``model_by_session``. If a future
    change to ``tick()`` starts reading a third attribute, tests fail with a
    loud ``AttributeError`` rather than silently exercising a stale shape —
    which is the point of not faking the whole object.
    """
    # Imported lazily on purpose. ``watcher.py`` transitively pulls in the agent
    # manifest cache, ``jsonl_tail`` (whose DEFAULT_OFFSETS_PATH freezes
    # ``~/.xo-cowork`` at import) and every sink — all of which must not be
    # imported until conftest's Layer 1 has rewritten HOME. The module-level
    # ``ingest.events`` import above is safe by contrast: it is pure dataclasses
    # and touches no path.
    from services.cowork_agent.visualizer.watcher import Watcher

    w = Watcher.__new__(Watcher)
    w.sources = list(sources)
    w.model_by_session = dict(model_by_session or {})
    return w


# ── Event factories ──────────────────────────────────────────────────────────
#
# Every event in ``ingest/events.py`` is a frozen, slots-backed, keyword-only
# dataclass sharing four base fields: ts, native_session_id, runtime, project_id.
# These factories default all four and forward the type-specific ones, so a test
# reads as the one thing it is asserting about.


def _base(
    ts: str = DEFAULT_TS,
    session_id: str = DEFAULT_SESSION_ID,
    runtime: str = DEFAULT_RUNTIME,
    project_id: str = DEFAULT_PROJECT_ID,
) -> dict:
    return {
        "ts": ts,
        "native_session_id": session_id,
        "runtime": runtime,
        "project_id": project_id,
    }


def session_first_seen(
    *,
    cwd: str = "/sandbox/xo-projects/demo",
    **base: Any,
) -> SessionFirstSeen:
    """First-ever observation of a session. Sinks read it as ``session.started``."""
    return SessionFirstSeen(**_base(**base), cwd=cwd)


def message_observed(
    *,
    role: str = "assistant",
    model: Optional[str] = None,
    **base: Any,
) -> MessageObserved:
    """One message. ``role`` ∈ {``user``, ``assistant``}; ``model`` assistant-only."""
    return MessageObserved(**_base(**base), role=role, model=model)


def usage_observed(
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    model: Optional[str] = "fake-model-1",
    latency_ms: Optional[int] = 1200,
    **base: Any,
) -> UsageObserved:
    """Token usage for an assistant turn.

    ``model`` defaults to a non-None value on purpose: this is the *only* event
    that feeds ``Watcher.model_by_session``, and the activity sink drops any
    presence row whose session has no entry there. A usage event with
    ``model=None`` is therefore the setup for "session is live but invisible".
    """
    return UsageObserved(
        **_base(**base),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        model=model,
        latency_ms=latency_ms,
    )


def tool_use_observed(*, tool: str = "Bash", **base: Any) -> ToolUseObserved:
    """A tool_use — the tool NAME only, never its inputs."""
    return ToolUseObserved(**_base(**base), tool=tool)


def file_touched(
    *,
    relative_path: str = "src/app.py",
    created: bool = False,
    **base: Any,
) -> FileTouched:
    """An Edit/Write touch. The path is always project-relative by contract."""
    return FileTouched(**_base(**base), relative_path=relative_path, created=created)


def task_created(
    *,
    task_id: str = "1",
    content: str = "Wire up the thing",
    description: Optional[str] = None,
    active_form: Optional[str] = None,
    **base: Any,
) -> TaskCreated:
    """A task, already paired with its tool_result (post-pairing event)."""
    return TaskCreated(
        **_base(**base),
        task_id=task_id,
        content=content,
        description=description,
        active_form=active_form,
    )


def task_status_changed(
    *,
    task_id: str = "1",
    status: str = "completed",
    **base: Any,
) -> TaskStatusChanged:
    """``status`` ∈ {pending, in_progress, completed, cancelled, blocked}."""
    return TaskStatusChanged(**_base(**base), task_id=task_id, status=status)


# ── Presence rows ────────────────────────────────────────────────────────────


def presence_row(
    *,
    session_id: str = DEFAULT_SESSION_ID,
    runtime: str = DEFAULT_RUNTIME,
    project_id: str = DEFAULT_PROJECT_ID,
    started_at_ms: int = DEFAULT_STARTED_AT_MS,
    updated_at_ms: Optional[int] = None,
    **extra: Any,
) -> dict:
    """One "this session is open right now" row, as ``poll_presence()`` yields.

    Presence rows are plain dicts, not dataclasses. Only these keys are load
    bearing today:

    * ``project_id`` — ``Watcher.tick()`` groups rows by it; a row without one
      is dropped before the sink ever sees it (``watcher.py:153-157``).
    * ``session_id`` and ``runtime`` — the activity sink drops the row if either
      is missing, rather than risk mis-tagging it to the wrong backend.
    * ``started_at_ms`` / ``updated_at_ms`` — **integer epoch millis**, which the
      sink converts to the ISO ``opened_at`` / ``last_activity_at`` fields. A
      falsy value there means "now", not "epoch 0".

    ``agent`` is deliberately absent: the sink fills it from the watcher's
    ``model_by_session`` cache and drops the row when it is unknown. ``**extra``
    passes through so a test can add fields a future sink might read.
    """
    row = {
        "session_id": session_id,
        "runtime": runtime,
        "project_id": project_id,
        "started_at_ms": started_at_ms,
        "updated_at_ms": (
            started_at_ms if updated_at_ms is None else updated_at_ms
        ),
    }
    row.update(extra)
    return row
