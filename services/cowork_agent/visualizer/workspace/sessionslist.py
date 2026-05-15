"""``~/xo-projects/.xo/sessions/sessionslist.json`` — union of every
project's adapter-written ``sessionslist.json``.

Each row already carries ``directory`` (absolute path of the
project) so the workspace tier doesn't need a separate ``project_id``
field — the BFF route derives ``projectId`` from the directory or
from the row's enclosing scope.

Watcher-written at this tier; adapters don't touch workspace-level
files. Composite-key collisions across projects are not expected
(the adapter generates an 8-hex suffix) but if they happened, the
later project would win — same last-write-wins behaviour any union
has.
"""

from __future__ import annotations

from services.cowork_agent.project_layout import workspace_xo_dir, xo_dir
from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.workspace_index import list_project_ids


def apply() -> bool:
    merged: dict[str, dict] = {}
    for pid in list_project_ids():
        sl = read_json(xo_dir(pid) / "sessions" / "sessionslist.json")
        if not isinstance(sl, dict):
            continue
        for key, row in sl.items():
            if isinstance(row, dict):
                merged[key] = row
    # sessionslist.json has no top-level wrapper — it's the flat map.
    write_json_atomic(workspace_xo_dir() / "sessions" / "sessionslist.json", merged)
    return True
