"""One watcher tick — sink ordering, tier keying, and failure containment.

Guards ``services/cowork_agent/visualizer/watcher.py::Watcher.tick``. Driven
through the ``Watcher.__new__`` seam in ``tests/fakes.py``, so no adapter, no
``OffsetStore`` and no ``AGENT_NAME`` are involved: every assertion here is
about the orchestration, not about any one backend's log format.

**The ordering invariant.** ``fill_identity`` mints ``project.json:pid`` and the
runtime tier is keyed *by that pid* (``~/.xo/<pid>/``). Resolve the runtime root
first and ``runtime_key`` has nothing to key on yet, so it falls back to the
folder name (``project_layout.py:234``) — the tick then writes that project's
telemetry to ``~/.xo/<folder>/`` while every later tick, and every reader, uses
``~/.xo/<pid>/``. Nothing errors; the data is simply split in two and half of it
is never read again. The design doc rates this High risk
(docs/xo-runtime-tier-restructure.md:105), and the two loops that need the
ordering were fixed separately — hence the first test asserts *both*.
"""

from __future__ import annotations

import json
import uuid

from fakes import (
    FakeSource,
    file_touched,
    make_watcher,
    presence_row,
    session_first_seen,
    task_created,
    usage_observed,
)

from services.cowork_agent.visualizer import watcher as watcher_mod

# Module-level app imports are safe: pytest imports conftest.py (and its Layer 1
# environment rewrite) before it imports any test module.


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_identity_is_filled_before_the_runtime_root_is_resolved_in_both_loops(
    scaffolded_project, monkeypatch
):
    """``fill_identity`` must precede ``project_runtime_dir`` in *each* loop.

    ``tick`` resolves the runtime root twice per tick from two different places:
    the per-project event loop (``watcher.py:126-128``) and the activity loop
    (``:166-177``). They were ordered correctly in separate changes, so a
    regression in one is invisible if only the other is covered.

    ``alpha`` has events and so passes through both loops; ``beta`` has none and
    so exercises the activity loop alone. Both projects start unfilled, so a
    ``project_runtime_dir`` call that arrives first would be answering with the
    folder-name fallback.
    """
    scaffolded_project("alpha", pid=None)
    scaffolded_project("beta", pid=None)

    calls: list[tuple[str, str]] = []
    real_fill = watcher_mod.project_json.fill_identity
    real_runtime_dir = watcher_mod.project_runtime_dir

    def spy_fill(xo, project_id):
        calls.append(("fill", project_id))
        return real_fill(xo, project_id)

    def spy_runtime_dir(name):
        calls.append(("runtime", name))
        return real_runtime_dir(name)

    monkeypatch.setattr(watcher_mod.project_json, "fill_identity", spy_fill)
    monkeypatch.setattr(watcher_mod, "project_runtime_dir", spy_runtime_dir)

    make_watcher(FakeSource(events=[session_first_seen(project_id="alpha")])).tick()

    # Two fill→runtime pairs for the project that went through both loops...
    assert [op for op, name in calls if name == "alpha"] == [
        "fill",
        "runtime",
        "fill",
        "runtime",
    ]
    # ...and one for the project the activity loop reached on its own.
    assert [op for op, name in calls if name == "beta"] == ["fill", "runtime"]


def test_a_first_tick_keys_the_runtime_tier_by_the_pid_it_just_minted(
    scaffolded_project, xo_roots
):
    """The behavioural half of the ordering invariant: where the bytes land.

    A brand-new project arrives as ``_template: true`` / ``pid: null``. After one
    tick every runtime artefact must be under ``~/.xo/<minted-uuid>/`` and the
    folder-name fallback directory must never have been created — its existence
    is the fingerprint of a runtime root resolved before the identity fill.
    """
    proj = scaffolded_project("demo", pid=None)
    before = _read(proj.xo / "project.json")
    assert before["_template"] is True and before["pid"] is None

    src = FakeSource(
        events=[
            session_first_seen(project_id="demo"),
            usage_observed(project_id="demo"),
            file_touched(project_id="demo", created=True),
            task_created(project_id="demo"),
        ],
        presence=[presence_row(project_id="demo")],
    )
    make_watcher(src).tick()

    meta = _read(proj.xo / "project.json")
    uuid.UUID(meta["pid"])  # raises if the pid is not a real UUID
    assert "_template" not in meta

    runtime = xo_roots.runtime / meta["pid"]
    assert (runtime / "stats.json").is_file()
    assert (runtime / "timeline.jsonl").is_file()
    assert (runtime / "activity.json").is_file()
    assert (runtime / "sessions" / "sessions-augment.json").is_file()

    # `proj.runtime` is what `runtime_key` resolved to *before* the tick, i.e.
    # the transitional folder-name key. Nothing may ever have been written there.
    assert proj.runtime == xo_roots.runtime / "demo"
    assert not proj.runtime.exists()

    # And the synced tier still holds only the synced contract.
    assert sorted(p.name for p in proj.xo.iterdir()) == [
        "peers.json",
        "project.json",
        "todos.json",
    ]


def test_a_source_that_raises_does_not_stop_the_activity_loop(scaffolded_project):
    """A broken source degrades the tick, it does not end it.

    ``watcher.py:99-102`` swallows a failing ``poll_events`` so the rest of the
    tick still runs. That matters most for the activity loop (``:144-179``),
    which is what *evicts* dead sessions: if an event-poll failure skipped it,
    ``activity.json`` would freeze showing sessions that have long exited.
    """
    proj = scaffolded_project("demo")
    src = FakeSource(
        events_error=RuntimeError("transcript store is unreadable"),
        presence=[presence_row(project_id="demo")],
    )
    # Model cache pre-primed: the activity sink drops rows whose model it does
    # not know, and the usage events that would have filled it were in the
    # batch that just failed.
    make_watcher(src, model_by_session={"sess-0001": "fake-model-1"}).tick()

    assert src.poll_presence_calls == 1
    activity = _read(proj.runtime / "activity.json")
    assert [row["session_id"] for row in activity["open_sessions"]] == ["sess-0001"]


def test_events_without_a_project_id_are_dropped_before_any_sink(
    scaffolded_project, xo_roots
):
    """An untagged event has no project to write to, so it writes nowhere.

    ``watcher.py:112-114`` filters on a truthy ``project_id``. Sources emit one
    when a transcript's cwd cannot be mapped into the workspace; letting that
    through would resolve ``xo_dir("")`` — ``normalize_agent_id("")`` is
    ``"main"`` (``helpers.py:46-47``), so the events would silently land in a
    project called *main*.
    """
    proj = scaffolded_project("demo")

    src = FakeSource(
        events=[
            session_first_seen(project_id=""),
            file_touched(project_id=""),
            usage_observed(project_id=""),
        ]
    )
    make_watcher(src).tick()

    # The activity loop ran (it is presence-driven, not event-driven)...
    assert (proj.runtime / "activity.json").is_file()
    # ...but no event sink did, for this project or any other.
    assert not (proj.runtime / "timeline.jsonl").exists()
    assert not (proj.runtime / "stats.json").exists()
    assert not (proj.runtime / "sessions").exists()
    assert not (xo_roots.runtime / "workspace" / "timeline.jsonl").exists()
    # No stray project folder was conjured from the empty id.
    assert [p.name for p in xo_roots.projects.iterdir()] == ["demo"]


def test_the_model_cache_fills_from_usage_and_outlives_the_tick_that_filled_it(
    scaffolded_project,
):
    """``UsageObserved`` is the only source of the activity row's ``agent``.

    ``watcher.py:106-108`` caches the model id per session; the activity sink
    requires it and drops any row it cannot name (``sinks/activity.py:88-92``).
    The cache is in-memory and must survive across ticks — a live session only
    reports usage on the turns it takes, so a cache scoped to one tick would
    make every quiet session vanish from ``activity.json`` and reappear later.
    """
    proj = scaffolded_project("demo")
    src = FakeSource(
        events=[usage_observed(project_id="demo", model="fake-model-7")],
        presence=[presence_row(project_id="demo")],
    )
    w = make_watcher(src)  # cache starts empty

    w.tick()
    rows = _read(proj.runtime / "activity.json")["open_sessions"]
    assert [(r["session_id"], r["agent"]) for r in rows] == [
        ("sess-0001", "fake-model-7")
    ]

    # Second tick: the queue is drained (no new usage event), presence unchanged.
    w.tick()
    rows = _read(proj.runtime / "activity.json")["open_sessions"]
    assert [(r["session_id"], r["agent"]) for r in rows] == [
        ("sess-0001", "fake-model-7")
    ]


def test_a_session_missing_from_presence_is_evicted_on_the_next_tick(
    scaffolded_project,
):
    """``activity.json`` is a snapshot of *now*, so exits have to disappear.

    The activity loop runs for every known project rather than only those with
    events this tick (``watcher.py:159``) precisely so an exited session is
    cleared. Presence is a snapshot and not a drain (see ``fakes`` module
    docstring): the row is gone on tick 2 because the source stopped reporting
    it, not because reading it consumed it.
    """
    proj = scaffolded_project("demo")
    src = FakeSource(
        events=[usage_observed(project_id="demo")],
        presence=[presence_row(project_id="demo")],
    )
    w = make_watcher(src)

    w.tick()
    assert len(_read(proj.runtime / "activity.json")["open_sessions"]) == 1

    src.set_presence([])  # the session exited
    w.tick()
    assert _read(proj.runtime / "activity.json")["open_sessions"] == []


def test_timeline_lines_fan_out_to_the_workspace_tier_tagged_by_project(
    scaffolded_project, xo_roots
):
    """The same rendered lines reach both tiers; only the workspace copy is tagged.

    ``watcher.py:138-142`` forwards the per-project sink's return value instead
    of re-rendering, and ``workspace/timeline.py:41-43`` adds ``project_id`` on
    the way in — that field is the only thing that makes a multiplexed workspace
    timeline demultiplexable. The workspace timeline is runtime tier too, so it
    must land in the runtime home and nowhere near a project tree.
    """
    proj = scaffolded_project("demo")
    src = FakeSource(
        events=[
            session_first_seen(project_id="demo"),
            file_touched(project_id="demo", relative_path="src/app.py", created=True),
        ]
    )
    make_watcher(src).tick()

    project_lines = _jsonl(proj.runtime / "timeline.jsonl")
    workspace_lines = _jsonl(xo_roots.runtime / "workspace" / "timeline.jsonl")

    assert [line["type"] for line in project_lines] == [
        "session.started",
        "file.created",
    ]
    assert [line["type"] for line in workspace_lines] == [
        "session.started",
        "file.created",
    ]
    assert {line["project_id"] for line in workspace_lines} == {"demo"}
    assert all("project_id" not in line for line in project_lines)
    assert list(xo_roots.projects.rglob("timeline*.jsonl")) == []


def test_one_project_s_sink_failure_does_not_abort_the_others(
    scaffolded_project, monkeypatch
):
    """Per-project sink batches are isolated — ``watcher.py:133-135``.

    One project with an unwritable ``.xo/`` (a permissions problem, a full disk,
    a corrupt JSON document a sink chokes on) must not stop every other project
    on the machine from being materialised. ``alpha`` is queued first, so
    ``beta``'s timeline existing is proof the loop resumed rather than never
    reaching the failure.
    """
    alpha = scaffolded_project("alpha")
    beta = scaffolded_project("beta")

    real_todos_apply = watcher_mod.todos.apply

    def flaky(synced, events):
        if synced == alpha.xo:
            raise OSError("no space left on device")
        return real_todos_apply(synced, events)

    monkeypatch.setattr(watcher_mod.todos, "apply", flaky)

    src = FakeSource(
        events=[
            session_first_seen(project_id="alpha"),
            session_first_seen(project_id="beta", session_id="sess-beta"),
        ]
    )
    make_watcher(src).tick()

    # alpha aborted mid-batch: the sink before the failure ran, the ones after
    # it did not.
    assert (alpha.runtime / "sessions" / "sessions-augment.json").is_file()
    assert not (alpha.runtime / "timeline.jsonl").exists()
    # beta was processed in full regardless.
    assert [line["type"] for line in _jsonl(beta.runtime / "timeline.jsonl")] == [
        "session.started"
    ]
