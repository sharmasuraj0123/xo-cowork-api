# Space — the workspace knowledge graph UI

An explorable map of `~/xo-projects`: a force-directed **graph** (click to
focus, double-click clusters, re-root on any node), a scrubbable **timeline**,
and **six degrees** pathfinding between any two artifacts.

This folder is a bundled snapshot of the xo-atlas UI (originally a standalone
folder with no remote), trimmed to the single endpoint-driven page and served
by this API — so every workspace that runs xo-cowork-api gets the graph with
zero configuration.

## Files

| File | What it is |
|------|------------|
| `index.html` | The whole app (was xo-atlas `v3.html`). Self-contained, no dependencies. |

## How it's served

`routers/space.py` mounts this folder read-only at `/space` (so the app is at
`http://localhost:5002/space/`) and registers `GET /space/data/space.json`
**before** the mount — the graph data the page fetches is generated live from
`~/xo-projects` by `services/cowork_agent/visualizer/space_index.py`. If the
builder throws, the route answers 503 and the app shows its "no data source"
panel. (The route can also fall back to a `data/space.json` file in this
folder; none is bundled — a wrong-looking demo map beats nothing, but a
truthful error panel beats both.)

- Override the folder with the `SPACE_DIR` env var (e.g. to point at a live
  xo-atlas checkout during UI development).
- The footer server pill polls `GET /space/server/status`; **Stop** calls
  `POST /space/server/stop` (localhost only).

Local change vs upstream xo-atlas: `simTick()` clamps per-tick node velocity
to 60 units — generated data can put 100+ leaves in one cluster, whose summed
spring stiffness makes the original explicit-Euler sim diverge (positions hit
1e20 and the canvas goes blank).

## Sessions tab

The fourth topbar tab (`Graph | Timeline | Six Degrees | Sessions`) is an
Argus telemetry dashboard: Claude Code session stats rendered as cards,
tables, and hand-drawn canvas charts (no dependencies), re-skinned to the
Space theme. It lives in its own closure, independent of the graph's
`boot()` — either can fail without taking the other down.

- Data: `GET /space/data/sessions.json`, one pre-aggregated payload built
  live from the Argus DB (`ARGUS_DB` env, default `~/.argus/argus.db`) by
  `services/cowork_agent/visualizer/argus_index.py`. Fetched lazily on
  first open; the Refresh button re-fetches (server rebuilds behind the
  same 30 s TTL).
- Sub-views: Overview · Sessions (list → detail with sub-agents and
  per-session tools) · Tools · Models · Trends · Prompts. The
  `Today/7d/30d/All` window selector filters client-side over per-day
  rollups shipped in the payload.
- No alerts view by design; prompt full-text search is out of scope (the
  Prompts view is a client-side filter over the latest 200).

## Data format

```jsonc
{
  "meta":       { "title", "tagline", "mappedOn", "workspace" },
  "categories": { "p_<project>": {"name": "...", "color": "#a2b56b"}, ... },
  "hubAngles":  { "p_<project>": -1.57, ... },      // radians, one region per project
  "timeline":   { "start": "2026-01-27", "end": "2026-07-20" },
  "root":       { "id": "xo", "label", "blurb" },
  "hubs":       [ { "id", "cat", "label", "blurb" } ],          // one per project
  "groups":     [ { "id", "cat", "label", "blurb" } ],          // one per top-level dir
  "leaves":     [ { "id", "group", "shape", "tag", "label",
                    "date", "blurb", "path" } ],                // one per file
  "ties":       [ { "s", "t", "label" } ],      // derived cross-links (see below)
  "milestones": [ { "d": "YYYY-MM-DD", "t": "caption" } ]       // first commits
}
```

Shapes are semantic: `disc` = code, `ring` = document, `diamond` = everything
else. Leaf `date` is the git first-added date when the project is a repo,
else file mtime. Tree edges (leaf → cluster → project → root) are derived by
the UI; only cross-ties are listed.

Ties are derived facts, never editorial: files that repeatedly share commits
("changed together ×N", from the same git log that dates the leaves), docs
whose text names another file's relative path ("references"), and
`test_x` ↔ `x` filename pairs ("tests"). Strongest first, capped at 60.
