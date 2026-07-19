"""Read-only Space session telemetry for the Claude Code runtime.

Argus owns ingestion and pricing. This capability only resolves its database
location and adapts the existing builder to the generic multi-provider
telemetry contract.
"""

from __future__ import annotations

import os
from pathlib import Path

from services.cowork_agent.visualizer.argus_index import build_argus_stats


SOURCE_ID = "claude_code"
SOURCE_LABEL = "Claude Code"
META_PRIORITY = 100  # keep the legacy Argus meta fields at the top level
COST_STATUS = "estimated"


def collect_session_telemetry() -> dict:
    db_path = Path(os.getenv("ARGUS_DB", "~/.argus/argus.db")).expanduser()
    # Argus can contain rows from several runtimes. This capability represents
    # only its owning runtime so another provider cannot duplicate those rows.
    data = build_argus_stats(db_path, agent=SOURCE_ID)
    return {
        "source": {
            "id": SOURCE_ID,
            "label": SOURCE_LABEL,
            "cost_status": COST_STATUS,
        },
        "meta_priority": META_PRIORITY,
        "meta": data["meta"],
        "totals": data["totals"],
        "project_keys": data["project_keys"],
        "sessions": data["sessions"],
        "daily_models": data["daily_models"],
        "daily_sessions": data["daily_sessions"],
        "daily_tools": data["daily_tools"],
    }
