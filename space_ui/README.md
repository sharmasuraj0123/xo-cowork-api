# Space — the workspace knowledge graph UI

An explorable map of `~/xo-projects`: a force-directed **graph** (click to
focus, double-click clusters, re-root on any node), a scrubbable **timeline**,
and **six degrees** pathfinding between any two artifacts. The same shell also
includes multi-runtime **sessions**, project commit-relay/sharing state, and streamed
agent **chat**. The **Experiment** tab turns any listed XO project into a
short-lived Docker sandbox with its own Space server and an interactive,
boot-verified Agents API executor.

This folder is a bundled snapshot of the xo-atlas UI (originally a standalone
folder with no remote), trimmed to the single endpoint-driven page and served
by this API — so every workspace that runs xo-cowork-api gets the graph with
zero configuration.

Start with [ONE_PAGER.md](./ONE_PAGER.md) for the audited product/architecture
summary and [FEATURE_INVENTORY.md](./FEATURE_INVENTORY.md) for the complete
implemented behavior, formulas, APIs, refresh rules, and known constraints.

## Files

Build-free ES modules (no bundler, no dependencies); the browser loads them
directly. Descended from the single-file xo-atlas `v3.html`.

| Path | What it is |
|------|------------|
| `index.html` | Thin shell: markup + stylesheet links + `js/app.js` entry. |
| `css/` | The original stylesheet split at its section banners, loaded in original order (cascade unchanged). |
| `js/app.js` | Entry point. Registers views; **adding a view = one new file in `js/views/` + one import line here.** |
| `js/core/registry.js` | View registry: tab nav, `1..n` hotkeys, `#/<id>` hash routing, lazy mount, per-view failure isolation. |
| `js/core/api.js` | The one fetch layer: `API_BASE`, query-string auth forwarding, offline / HTTP-error / 501 classification, single-flight GETs. |
| `js/core/store.js` | Idempotency helpers: single-flight promises, slotted (non-stacking) intervals. |
| `js/core/ui.js` | Shared UI helpers (toast). |
| `js/core/server-widget.js` | Footer server pill (status poll + offline restart guidance). |
| `js/views/atlas.js` | Graph + Timeline + Six Degrees — three lenses over one dataset, one shared closure, three exported views. |
| `js/views/sessions.js` | The Sessions multi-runtime telemetry view and source filters. |
| `js/views/projects.js` | The Projects view: project inventory, local/remote commit feed, commit-relay health, members, share, and revoke. |
| `js/views/chat.js` | The Chat view: Plane-B chat (`/api/chat/prompt` → SSE stream → transcript refetch) with session sidebar, project binding for new sessions, and mini-markdown rendering. Works across claude_code / hermes / openclaw. |
| `js/views/experiment.js` | The Experiment view: project-root picker, one-click launch, interactive agent workbench, sandbox Space link, lifecycle polling, and cleanup controls. |
| `js/core/markdown.js` | Escape-first mini-markdown (fences, inline code, emphasis, links, headings, lists, quotes, rules, and tables). |

The view contract and current operating details are documented in
[FEATURE_INVENTORY.md](./FEATURE_INVENTORY.md).

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
- The footer server pill polls `GET /space/server/status`. (The backend also
  exposes `POST /space/server/stop`, localhost-only, but the UI deliberately
  carries no stop control.)

Local change vs upstream xo-atlas: `simTick()` clamps per-tick node velocity
to 60 units — generated data can put 100+ leaves in one cluster, whose summed
spring stiffness makes the original explicit-Euler sim diverge (positions hit
1e20 and the canvas goes blank).

## Experiment tab

See the dedicated [Experiment technical one-pager](../docs/EXPERIMENT_ONE_PAGER.md)
for the complete architecture, current Agents API contract, isolation model,
technology stack, and production path.

The topbar has seven views: Graph, Timeline, Six Degrees, Sessions, Projects,
Chat, and Experiment. `#/experiment` loads the XO project inventory and checks
the local provider before enabling Launch. A launch creates an Agents API
self-hosted session, prepares sanitized copies of both the selected project and
the current `xo-cowork-api` worktree, starts a sandbox-local Space server plus
`codex exec-server`, runs a read-only boot turn, and becomes ready only after
both services and the selected project inventory pass readiness checks.

Local setup, from `xo-cowork-api`:

```bash
./cowork-api.sh install && ./cowork-api.sh restart
```

`OPENAI_API_KEY` must be present in `.env` and authorized for the early-access
Agents API. `GET /api/experiments/options` verifies the SDK, Docker daemon,
image, endpoint reachability, and project authorization without returning the
credential. The UI then uses `POST /api/experiments`, polls
`GET /api/experiments`, sends follow-up work through
`POST /api/experiments/{id}/turns`, opens the returned loopback-only sandbox
`space_url`, and stops through `POST /api/experiments/{id}/stop`.
The Experiment tab inside that returned sandbox Space is inspection-only: it
links back to the parent workbench and never reports the child container's
intentionally absent API key, SDK, or Docker CLI as host setup failures. Set
`EXPERIMENT_PARENT_SPACE_URL` when the parent is exposed through a Coder/XO
proxy instead of local loopback.

The host project is never mounted into the agent container. A temporary staging
copy recursively omits dotenv files, common credential stores, dependency
trees, caches, build output, symlinks, and special files; Git projects retain a
local Git checkout with remotes removed and receive sanitized modified/untracked
work. Staging is mounted read-only and deleted after boot. Containers run as an
unprivileged user with a read-only root filesystem, tmpfs-backed writable
workspace, dropped Linux capabilities, `no-new-privileges`, CPU/memory/PID
limits, a default one-hour auto-stop, and a default capacity of two. Startup
reconciles labelled containers left by an unclean exit; normal shutdown stops
all tracked experiments.

Launch is still a data-boundary decision: the OpenAI-hosted agent can inspect
the filtered sandbox copy. The UI states this next to the button; choose only a
project you intend to expose to that agent. Filtering reduces accidental secret
exposure but cannot prove that arbitrary project files contain no sensitive
content.

This first provider is deliberately local-development only. Production Coder/XO
environments need the same provider-neutral API backed by a workspace-native
sandbox service and scoped short-lived executor credentials, rather than a
Docker socket inside the workspace. Configure `EXPERIMENT_SPACE_URL_TEMPLATE`
for a trusted Coder port-forwarding URL; local Docker binds dynamic ports only
to `127.0.0.1`.

## Sessions tab

The fourth view, Sessions, combines Claude Code and Codex session stats in
cards, tables, and hand-drawn canvas charts (no dependencies), re-skinned to the
Space theme. Independent checked-by-default source checkboxes show both together
or isolate either runtime. It lives in its own module
(`js/views/sessions.js`), independent of the atlas's `boot()` — either can fail
without taking the other down, and the registry keeps the tabs switchable
regardless.

- Data: `GET /space/data/sessions.json`, one pre-aggregated payload built from
  every discovered `session_telemetry` capability. Claude Code reads Argus
  (`ARGUS_DB`, default `~/.argus/argus.db`); Codex reads its state database and
  referenced rollouts (`CODEX_HOME`, default `~/.codex`). Fetched lazily on
  first open; Refresh re-fetches behind the same 30 s server TTL. A failed
  source degrades independently while another readable source still returns
  a useful response.
- Sub-views: Overview · Sessions (list → detail with sub-agents and
  per-session tools) · Tools · Models · Trends. The `Today/7d/30d/All`
  window selector filters client-side over per-day rollups shipped in the
  payload.
- Every session and daily rollup carries an `agent` field plus a collision-safe
  session key. Every subview filters its own raw rollups, so toggling a source
  recalculates overview totals, lists, tools, models, and trends consistently.
- Codex does not expose authoritative cost, so its cost renders as unavailable;
  combined cost is explicitly marked partial rather than treating Codex as $0.
- No alerts, prompts, titles, reasoning, tool arguments, or tool results enter
  the payload. Only metadata, numeric usage, model IDs, and tool names are kept.

### Sessions validation checklist

After changing a telemetry provider or the Sessions UI:

- `venv/bin/python -m unittest discover -v` passes.
- `GET /space/data/sessions.json` returns both sources in `meta.sources` when
  their stores exist; session keys are unique and every rollup is source-tagged.
- Both source checkboxes start checked. Claude-only, Codex-only, combined, and
  neither-selected states all render without reloading the page.
- Overview, Sessions, Tools, Models, and Trends recalculate from the selected
  sources; the checkbox that changed retains keyboard focus.
- Codex-only cost says unavailable, combined cost says partial, and Claude-only
  cost remains estimated.
- A real Codex detail distinguishes tree-inclusive and own-only totals and never
  displays unknown usage as fresh input; content-bearing fields remain absent
  from the API response.

## Data format

```jsonc
{
  "meta":       { "title", "tagline", "mappedOn", "workspace" },
  "categories": { "p_<project>": {"name": "...", "color": "#a2b56b"}, ... },
  "hubAngles":  { "p_<project>": -1.57, ... },      // radians, one region per project
  "timeline":   { "start": "2026-01-27", "end": "2026-07-20" },
  "root":       { "id": "xo", "label", "blurb" },
  "hubs":       [ { "id", "cat", "label", "blurb" } ],          // one per project
  "groups":     [ { "id", "cat", "label", "blurb" } ],          // emitted directory buckets
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

Ties are derived facts within each project, never editorial: files that
repeatedly share commits
("changed together ×N", from the same git log that dates the leaves), docs
whose text names another file's relative path ("references"), and
`test_x` ↔ `x` filename pairs ("tests"). Strongest first, capped at 60.
