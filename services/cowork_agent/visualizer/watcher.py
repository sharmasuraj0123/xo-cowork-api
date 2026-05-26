"""Watcher main loop — drains the runtime source(s), fans events to
sinks, refreshes the workspace tier, and persists offsets.

Started from FastAPI lifespan in ``server.py`` (next to the existing
``usage_sync`` task). Non-fatal: failure to start, or any exception
inside a tick, logs and continues — the BFF endpoints keep serving
whatever data is on disk.

Tick budget for v1: ~1 s. Source poll + sinks + workspace tier per
tick. The blocking I/O work runs in ``asyncio.to_thread`` so the
event loop stays responsive to other FastAPI traffic.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Optional

from services.cowork_agent.agent_registry import get_active_agent
from services.cowork_agent.project_layout import xo_dir
from services.cowork_agent.visualizer.ingest import jsonl_tail
from services.cowork_agent.visualizer.ingest.events import UsageObserved
from services.cowork_agent.visualizer.sinks import (
    activity,
    project_json,
    sessions_augment,
    stats,
    timeline,
    todos,
)
from services.cowork_agent.visualizer.source_loader import load_source_module
from services.cowork_agent.visualizer.workspace import (
    activity as ws_activity,
)
from services.cowork_agent.visualizer.workspace import (
    sessions_augment as ws_sessions_augment,
)
from services.cowork_agent.visualizer.workspace import (
    sessionslist as ws_sessionslist,
)
from services.cowork_agent.visualizer.workspace import (
    stats as ws_stats,
)
from services.cowork_agent.visualizer.workspace import (
    timeline as ws_timeline,
)
from services.cowork_agent.visualizer.workspace import (
    workspace_json,
)
from services.cowork_agent.visualizer.workspace_index import list_project_ids

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.0


class Watcher:
    """Single-tick orchestrator.

    Holds the source(s) and any in-process caches sinks need. State
    that survives restarts lives on disk (the source's offset store,
    the sinks' own files). Only in-memory state here is
    ``model_by_session`` — a cache of the last assistant model id
    seen per session, used by the activity sink to satisfy the
    schema's required ``agent`` field. Lost on restart; refilled
    from the next assistant turn (acceptable per
    docs/watcher-design.md §8.2).
    """

    def __init__(self) -> None:
        # One source: whichever the active AGENT_NAME resolves to.
        # See services/cowork_agent/visualizer/source_loader.py for the
        # dispatch. Agents without a visualizer_source.py module (today:
        # hermes) produce an empty source list — the watcher still runs
        # so sinks can serve whatever data is already on disk.
        offsets = jsonl_tail.OffsetStore()
        active_name = get_active_agent().name
        mod = load_source_module()
        if mod is None:
            self.sources = []
        else:
            source = mod.Source(offsets=offsets)
            assert source.name == active_name, (
                f"visualizer_source.Source.name {source.name!r} does not match "
                f"active agent {active_name!r}"
            )
            self.sources = [source]
        self.model_by_session: dict[str, str] = {}

    # ── One tick ────────────────────────────────────────────────────────

    def tick(self) -> None:
        # 1. Drain every source.
        events: list = []
        for src in self.sources:
            try:
                events.extend(src.poll_events())
            except Exception:
                logger.exception("source %s failed; continuing with others", src.name)

        # 2. Maintain the model cache from UsageObserved (model id is
        # attached there for assistant turns).
        for ev in events:
            if isinstance(ev, UsageObserved) and ev.model:
                self.model_by_session[ev.native_session_id] = ev.model

        # 3. Group by project_id.
        events_by_project: dict[str, list] = defaultdict(list)
        for ev in events:
            if ev.project_id:
                events_by_project[ev.project_id].append(ev)

        # 4. Per-project sinks. Run project_json first so identity is
        # filled before any sink writes a record referencing it; run
        # timeline last so its emitted lines can be fanned to the
        # workspace timeline.
        for project_id, project_events in events_by_project.items():
            x = xo_dir(project_id)
            try:
                project_json.fill_identity(x, project_id)
                sessions_augment.apply(x, project_events)
                todos.apply(x, project_events)
                stats.apply(x, project_events)
                timeline_lines = timeline.apply(x, project_events)
            except Exception:
                logger.exception("sink batch failed for project %s", project_id)
                continue

            # Workspace timeline gets the same rendered lines, tagged.
            if timeline_lines:
                try:
                    ws_timeline.apply(timeline_lines, project_id=project_id)
                except Exception:
                    logger.exception("workspace timeline failed for %s", project_id)

        # 5. Activity sink — driven by presence snapshot, not events.
        # Runs for every project (even those with no events this tick)
        # so a session that exited gets evicted from activity.json.
        presence: list[dict] = []
        for src in self.sources:
            try:
                presence.extend(src.poll_presence())
            except Exception:
                logger.exception("presence poll failed for %s", src.name)
        presence_by_project: dict[str, list] = defaultdict(list)
        for row in presence:
            pid = row.get("project_id")
            if isinstance(pid, str) and pid:
                presence_by_project[pid].append(row)

        for pid in list_project_ids():
            try:
                activity.apply(
                    xo_dir(pid),
                    presence_by_project.get(pid, []),
                    model_by_session=self.model_by_session,
                )
            except Exception:
                logger.exception("activity sink failed for %s", pid)

        # 6. Workspace tier — re-aggregate every tick (cheap; small
        # JSON files). Timeline is append-only and handled in step 4.
        try:
            workspace_json.apply()
            ws_stats.apply()
            ws_activity.apply()
            ws_sessionslist.apply()
            ws_sessions_augment.apply()
        except Exception:
            logger.exception("workspace tier failed")

    # ── Async runner ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Long-running coroutine for the lifespan task.

        Each tick's blocking I/O happens in ``asyncio.to_thread`` so
        the FastAPI loop stays responsive. Per-tick exceptions are
        swallowed (logged) so one bad tick doesn't stop the watcher
        — only ``CancelledError`` ends the loop.
        """
        logger.info("Watcher started; polling every %.1fs", POLL_INTERVAL_S)
        try:
            while True:
                try:
                    await asyncio.to_thread(self.tick)
                except Exception:
                    logger.exception("watcher tick failed (non-fatal)")
                await asyncio.sleep(POLL_INTERVAL_S)
        except asyncio.CancelledError:
            logger.info("Watcher shutting down")
            raise


# ── Public entry-point used by ``server.py`` lifespan ────────────────────────


_watcher: Optional[Watcher] = None


async def start_watcher() -> None:
    """Construct (once) and run the watcher loop. Designed to be
    spawned as an asyncio task from FastAPI's lifespan handler.
    """
    global _watcher
    if _watcher is None:
        _watcher = Watcher()
    await _watcher.run()
