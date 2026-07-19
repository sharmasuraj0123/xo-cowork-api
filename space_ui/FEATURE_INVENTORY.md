# `/space` implemented feature and calculation inventory

This document records what `/space` actually implements on
`feat/commit-telepathy` as of 19 July 2026. It separates current behavior from
product copy and future seams. The companion [ONE_PAGER.md](./ONE_PAGER.md)
explains the product utility and architecture at a glance.

## 1. System boundary

`/space` is a static, build-free browser application served by the FastAPI
process. It combines three independent read/operation planes:

1. **Atlas plane** — Graph, Timeline, and Six Degrees consume a request-built
   projection of project folders and Git history.
2. **Telemetry plane** — Sessions merges request-built, read-only projections
   from Claude Code's Argus database and Codex's local state/rollout store.
3. **Operational plane** — Projects, Chat, and Experiment use live BFF,
   commit-relay, session, message, agent-dispatch, Docker, and Agents API
   lifecycle services.

A fourth subsystem, the visualizer watcher, writes `.xo` observability state for
the wider product. It is related but is not the direct feed for Atlas or the
Sessions telemetry providers.

### Primary implementation map

| Concern | Implementation |
|---|---|
| Static shell | `space_ui/index.html`, `space_ui/css/*` |
| View registry and routing | `space_ui/js/core/registry.js`, `space_ui/js/app.js` |
| Fetch/single-flight state | `space_ui/js/core/api.js`, `space_ui/js/core/store.js` |
| Graph, Timeline, Six Degrees | `space_ui/js/views/atlas.js` |
| Sessions | `space_ui/js/views/sessions.js` |
| Projects | `space_ui/js/views/projects.js` |
| Chat | `space_ui/js/views/chat.js`, `space_ui/js/core/markdown.js` |
| Experiment | `space_ui/js/views/experiment.js`, `space_ui/css/experiment.css` |
| `/space` routes and static mount | `routers/space.py`, `server.py` |
| Atlas builder | `services/cowork_agent/visualizer/space_index.py` |
| Telemetry discovery/merge | `services/cowork_agent/visualizer/session_telemetry.py` |
| Claude Code telemetry | `services/cowork_agent/adapters/claude_code/session_telemetry.py`, `services/cowork_agent/visualizer/argus_index.py` |
| Codex telemetry | `services/cowork_agent/adapters/codex/session_telemetry.py` |
| Project inventory BFF | `routers/cowork_agent/bff/xo_projects.py` |
| Commit/share BFF | `routers/cowork_agent/bff/relay.py` |
| Relay engine | `services/cowork_agent/commit_relay/*` |
| Chat/session BFF | `routers/cowork_agent/chat.py`, `routers/cowork_agent/sessions.py` |
| Experiment BFF | `routers/cowork_agent/bff/experiments.py` |
| Experiment lifecycle/provider | `services/cowork_agent/experiments/runtime.py` |
| Backend capability seam | `services/cowork_agent/adapters/loader.py` |
| `.xo` materialization | `services/cowork_agent/visualizer/watcher.py` and `sinks/*` |

## 2. Shell, layout, and navigation

### Persistent UI

- XO mark, `Space` name, and “a workspace knowledge graph” tagline.
- Seven generated view buttons in registry order.
- Persistent Root picker and artifact search. These control Atlas and can switch
  the user back to Graph even when another tab is visible.
- Shared right-side detail drawer, pointer hover card, and transient toast.
- Footer with server liveness and graph metadata.

Desktop geometry is a 58 px header, flexible stage, and 42 px footer. Graph-like
views occupy absolute stacked panels. Sessions and Projects are centered,
scrollable dashboards up to 1,180 px wide. Experiment uses the same dashboard
width; Chat is a two-column workspace. The
352 px graph detail drawer becomes full-width below 760 px.

### View registry

| Order / key | Hash | View | Mount behavior |
|---:|---|---|---|
| 1 | `#/graph` | Graph | Lazy, once |
| 2 | `#/time` | Timeline | Lazy, once |
| 3 | `#/six` | Six Degrees | Lazy, once |
| 4 | `#/sessions` | Sessions | Lazy, once |
| 5 | `#/projects` | Projects | Lazy, once; section generated dynamically |
| 6 | `#/chat` | Chat | Lazy, once; section generated dynamically |
| 7 | `#/experiment` | Experiment | Lazy, once; section generated dynamically |

- An unknown hash falls back to Graph.
- Tab changes use `history.replaceState`; they do not add a browser-history entry.
- Each view's closure survives tab changes and resets only on full page reload.
- A mount failure is caught and rendered inside that view instead of blanking the
  shell. Window errors and unhandled rejections are logged.

### Shared request behavior

- Under `/space`, requests are same-origin. In a standalone UI context,
  API-prefixed calls fall back to `http://127.0.0.1:5002`.
- The page query string is forwarded to API requests for outer proxy/session
  compatibility.
- Callers receive classified `{ok, status, data/error}` results; fetch failures
  do not throw through view code.
- HTTP 501 is distinguished as an unavailable backend capability.
- Concurrent identical GETs share one promise. Writes are never deduplicated.
- Named intervals replace their predecessor to avoid timer multiplication.

## 3. `/space` HTTP surface

| Endpoint | Implemented behavior | Cache/failure behavior |
|---|---|---|
| `GET /space/server/status` | Returns `status: on`, PID, resolved UI directory, and directory-exists flag | No real downstream readiness check |
| `POST /space/server/stop` | Loopback-only; schedules process `SIGTERM` after 0.4 s | UI intentionally does not expose this action |
| `GET /space/data/space.json` | Synchronously builds Atlas from project folders and Git | Successful in-process cache, default 30 s; static file fallback; otherwise 503 |
| `GET /space/data/sessions.json` | Builds every discovered read-only `session_telemetry` provider in a worker thread and merges them | Separate successful cache, default 30 s; concurrent misses share one build; one source may degrade; no static fallback; all-source failure returns 503 |
| `/space/*` | Static mount of bundled `space_ui`, overridable with `SPACE_DIR` | Not mounted if the directory is missing at boot |
| `GET /api/experiments/options` | Checks SDK install, Docker/image readiness, and Agents API authorization | Never returns credentials; launch disabled when unavailable |
| `GET /api/experiments` | Returns up to 50 process-owned lifecycle snapshots | In-memory local-development state; polled every 2 s while visible |
| `POST /api/experiments` | Resolves one project ID and queues an isolated launch | 202; duplicate active project converges on the existing ID; 429 at capacity |
| `GET /api/experiments/{id}` | Returns one lifecycle snapshot | 404 for unknown process-local ID |
| `POST /api/experiments/{id}/stop` | Cancels launch and releases owned Docker/API resources | Cleanup handles retained with `cleanup_failed` when release cannot be verified |

The dynamic JSON routes are registered before the static mount, so they win over
same-named static files. Responses send `Cache-Control: no-store` to the browser
while still using the server's own in-memory cache. Failures are not cached, and
an expired successful value is not retained as a stale-last-good fallback.

The Atlas builder is synchronous inside its `async` handler and can occupy the
event-loop thread. Session collection is also synchronous internally, but the
route moves it to a worker thread because a cold Codex rollout scan is I/O-heavy.

## 4. Atlas backend: project-to-graph projection

### 4.1 Discovery and identity

- Root: `XO_PROJECTS_ROOT`; default `~/xo-projects`. Resolving the helper creates
  the root if absent.
- Atlas uses `list_projects()`, so only non-hidden immediate directories with an
  `.xo/project.json` are included.
- Malformed metadata degrades to the directory name and a minimal entry.
- A project produces one category and one hub. The hub label/description come
  from project metadata.
- Category colors rotate through an eight-color palette.
- Hub seed angle is:

  ```text
  -π/2 + project_index × 2π / discovered_project_count
  ```

The root node is always `xo` with label `xo-projects`. Its blurb reports the
number of discovered project records, even if a later deadline or read error
prevents all of them from being emitted.

### 4.2 Traversal and filtering

Traversal sorts names and prunes before descent:

- any dot-prefixed name;
- `node_modules`, `__pycache__`, `venv`, `dist`, `build`, and `target`;
- `.tmp`, `.swp`, `.swo`, `.bak`, and `.orig` suffixes;
- `~$` temporary-file prefixes.

`.git` and `.xo` are excluded by the dot-name rule. Pruning `os.walk` directory
names before descent means dependency/build trees do not consume the scan budget.

### 4.3 Groups and leaves

- Root-level files enter a `(root)` group.
- Each top-level directory begins as a group.
- A group over 40 files is split by the next path segment, recursively to depth
  four. Files located directly at a split level stay in that level's group.
- Empty groups are not emitted.
- Leaf IDs are `<project-id>:<relative-path>`; paths and blurbs expose the
  project-relative location.
- File shape is extension-based:

  | Kind | Extensions | Shape |
  |---|---|---|
  | Code/config | `.py`, `.js`, `.ts`, `.tsx`, `.jsx`, `.go`, `.rs`, `.java`, C/C++, shell, HTML/CSS, JSON/YAML/TOML/SQL, etc. | disc |
  | Document | `.md`, `.txt`, `.rst`, `.pdf`, `.docx` | ring |
  | Other | everything else | diamond |

- Tag is the uppercase extension without the dot, or `FILE` when there is no
  extension.

### 4.4 Dates and milestones

For each project, one oldest-first Git log supplies:

- the date on which every path first appeared;
- the project's first commit date;
- the list of files touched in each commit.

The Git subprocess has a five-second timeout. Any unavailable binary,
non-repository directory, empty history, command failure, or timeout causes file
dates to fall back to filesystem mtime and removes that project's Git-derived
milestone/co-change input.

First-appearance keeps the oldest date even after delete/re-add churn. A project
with a first commit contributes `<project> first commit` to `milestones`.

### 4.5 Boundedness and completeness

| Bound | Value | Consequence |
|---|---:|---|
| Files scanned per project | 2,000 | Remaining traversal is skipped |
| Leaves emitted per project | 400 | Newest 400 dates retained |
| Leaves emitted globally | 1,500 | Newest 1,500 retained; now-empty groups removed |
| Leaves before recursive group split | 40 | Large clusters subdivide |
| Group split depth | 4 | Very large deep buckets can remain dense |
| Whole build budget | 10 s | Remaining projects skipped at next between-project check |
| Derived ties | 60 | Strongest candidates retained |

The deadline is cooperative, not preemptive: a current Git command or file scan
can run beyond it before the next project boundary. On this machine the global
1,500 cap was reached, so Atlas must be treated as a recent map rather than a
complete file index.

### 4.6 Derived ties

The backend returns only derived ties; the browser constructs hierarchy edges.
Candidates are deduplicated, sorted by descending strength and stable endpoint
order, then capped.

1. **Co-change** — among the most recent 500 commits, ignore commits touching
   over 20 paths; connect retained paths appearing together at least three
   times. Label: `changed together ×N`; strength: `N`.
2. **Document reference** — scan at most 30 `.md`, `.rst`, or `.txt` documents
   per project, at most 64 KiB each; connect a document when its text contains
   another retained relative path. Label: `references`; strength: 2.
3. **Test pairing** — connect a retained `test_x` filename to retained files
   named `x` in the same project. Label: `tests`; strength: 2.

All three derivations operate inside a per-project path map. “Cross-tie” means a
non-hierarchy connection, often across groups, not a cross-project relationship.

### 4.7 Timeline range and response shape

With retained leaves:

```text
timeline.start = earliest retained leaf date − 7 days
timeline.end   = latest retained leaf date + 7 days
```

Without leaves, the range is today ± seven days. The response contains `meta`,
`categories`, `hubAngles`, `timeline`, `root`, `hubs`, `groups`, `leaves`,
`ties`, and `milestones`. It has no Pydantic response model or runtime schema
validation.

`materialize(path)` can atomically write this payload, but no code invokes it.
Atlas remains request-built rather than watcher-materialized.

## 5. Graph frontend

The three Atlas views share one fetched payload and one closure containing graph,
camera, selection, filter, and path state.

### 5.1 Browser graph construction

The frontend creates four edge types:

- global root → each project hub;
- project hub → each group;
- group → each leaf;
- each backend-derived tie.

With the observed live data, link count is calculated as:

```text
35 hubs + 152 groups + 1,500 leaves + 40 ties = 1,727 links
```

Adjacency and degree maps support layout, focus neighborhoods, radius, search
ranking, details, and pathfinding.

### 5.2 Node sizes

```text
root radius    = 17
hub radius     = 13
group radius   = 5.5 + min(5, adjacency_count × 0.22)
leaf radius    = 3.3 + min(4.2, (degree − 1) × 0.85)
```

### 5.3 Initial placement and force simulation

- Project hubs start on a radius-520 circle at backend-provided equal sectors.
- Groups fan within their project's sector and stagger radially.
- Leaves use a golden-angle-like seed: `index × 0.618033 × 2π`.
- Typed springs:

  | Edge | Target distance | Stiffness |
  |---|---:|---:|
  | root–hub | 520 | 0.02 |
  | hub–group | 175 | 0.05 |
  | group–leaf | 62 | 0.08 |
  | derived tie | 210 | 0.005 |

- Repulsion varies by type: root `−3400`, hub `−2600`, group `−1000`, leaf
  `−235`; pair work is limited by a 320-unit distance check.
- Collision correction separates overlapping node radii.
- Velocity damping is `0.7`; speed is capped at 60 to prevent divergent frames.
- Alpha decays by `0.9885` and stops below `0.003`.
- The initial view runs 260 ticks synchronously before fitting the camera.
- Camera fit uses `0.9 × min(viewWidth/boundsWidth,
  viewHeight/boundsHeight)`, clamped to `0.25..maxZoom`.

### 5.4 Graph interactions

- Drag a node.
- Drag background to pan.
- Wheel zoom around pointer, clamped to `0.22×..5×`.
- Click a node to select it, open details, and focus its one-hop neighborhood.
- Double-click a leaf/root-like node for two-hop focus.
- Double-click a group to expand/collapse its leaves.
- Double-click a hub to expand/collapse all of its groups.
- Department chips filter leaf visibility; structural nodes remain visible but
  unrelated portions dim.
- Hover card shows node-specific dates, paths, spans, and tie counts.
- Detail drawer shows description, up to 24 connections (ties first), Timeline,
  Path from, and Path to actions.
- The intro CTA enters the graph.
- `/` focuses search; Escape clears focus/path state.

### 5.5 Search and re-root

Search ranks matches by the first satisfied class:

1. label prefix;
2. label word boundary;
3. label substring;
4. tag substring;
5. blurb/path substring.

Ties then favor higher degree and shorter label. At most eight results render.
Selecting a result switches to Graph, opens its cluster, selects it, and fits the
camera.

Re-root runs unweighted BFS from the selected node. The chosen node is pinned at
the center and other nodes are encouraged onto concentric rings at roughly
`hop_depth × 110`. Reset restores the global root layout.

## 6. Timeline

### Rendering model

- One vertical lane per dynamic Atlas category.
- Monthly grid/tick labels across the backend range.
- Project first-commit milestone pips.
- Every retained leaf becomes a date-positioned dot.

Horizontal position is linear:

```text
x = left + (leaf_date − range_start) / (range_end − range_start) × drawable_width
```

A lane-local beeswarm avoids collisions: dates within 14 px are assigned rows
alternating above/below the center in 13 px increments. If the row budget is
exhausted, x shifts by 12 px and placement continues.

Dot radius is:

```text
3.2 + min(2.6, (degree − 1) × 0.5)
```

### Controls and state

- The range input maps `0..1000` linearly onto the date range.
- Leaves born on/before the selected date render at normal opacity; future leaves
  fade to `0.06`.
- Play advances `(range_duration)/(60 × 16)` **per animation frame**, targeting
  about 16 seconds at 60 Hz. It is refresh-rate/frame-drop dependent rather than
  elapsed-time based.
- A selected group/hub/leaf can create a chronological trace. Labels alternate
  above/below and expand outward to avoid close dates.
- Hover reuses the graph card.
- Clicking a timeline dot switches to Graph, expands/selects it, and pulses it.
- Timeline reconstructs on entry and resize. Playback can continue after leaving
  the tab because hide does not cancel it.

## 7. Six Degrees

### Inputs and controls

- From and To autocompletes over all nodes.
- Swap.
- Connect.
- Surprise me.
- Result chain with relation text.
- Trace on the graph.

Only choosing an autocomplete result changes the internal endpoint. Editing the
visible text after a previous choice can leave the displayed value and internal
node out of sync.

### Path calculation

The frontend runs weighted Dijkstra over the full graph:

| Edge | Cost |
|---|---:|
| Derived tie | 1.0 |
| Leaf ↔ group | 1.4 |
| Group ↔ project | 2.4 |
| Project ↔ global root | 4.5 |

The weighting prefers a semantic derived tie over a generic hierarchy route.
The displayed “N degrees” is the resulting number of edges, not the sum of
weights.

Because derived ties are intra-project, a route between different projects
normally climbs through project A → `xo-projects` → project B. The live test
connected `lovable.svg` to `transcript.jsonl` in six edges by exactly that route.

Surprise Me tries at most 30 random leaf pairs and prefers different projects
with at least five path nodes. Trace expands required clusters, clears conflicting
filters, switches to Graph, reveals one segment every 420 ms, and fits the path.

## 8. Sessions telemetry

### 8.1 Multi-provider sources

The core discovers adapter packages containing `session_telemetry.py`; it does
not branch on runtime names. A telemetry-only package does not need an
`adapter.py` and therefore does not become a chat backend.

**Claude Code / Argus**

- Database: `ARGUS_DB`, default `~/.argus/argus.db`.
- Opened as read-only SQLite with a two-second busy timeout.
- Missing optional `turns`, `tool_calls`, or `app_meta` data degrades those
  sections to empty.
- Prompts and alerts are intentionally never queried.
- The current DB reports schema 3 while code expects schema 6; this logs a
  warning and succeeds here because required fields remain compatible.

**Codex**

- Home: `CODEX_HOME`, default `~/.codex`; the newest schema-compatible
  `state_*.sqlite` supplies a
  whitelist of thread IDs, rollout paths, times, cwd, token total, CLI version,
  and model. Content-bearing title/message/preview columns are never selected.
- `thread_spawn_edges` provides recursive parent/subagent relationships.
- Referenced rollout JSONL is streamed to calculate daily cumulative-token
  deltas, cache/fresh/output breakdowns, turns, tool names, and structured
  failures. Paths are accepted only beneath `<CODEX_HOME>/sessions`.
- Stable rollout parses are cached by file size+mtime+model in a 1,024-entry
  process LRU; an active growing rollout is reparsed. Detailed scanning is
  bounded to 1,000 rollouts / 1 GiB per build. Rows beyond that budget retain
  exact totals with unclassified fallback attribution but omit turns/tools.
  Partial/malformed final lines degrade without losing prior complete
  observations.
- Because state and rollout writes can race, the newest eight zero-state roots
  receive a separate recovery probe capped at 64 MiB. A positive rollout can
  therefore appear before its state token counter advances without consuming
  the normal detail budget.
- Prompt/message text, reasoning, titles, tool arguments, and tool results are
  neither retained nor emitted.
- Codex has no authoritative cost field; rows carry `cost_known: false` and the
  UI renders unavailable/partial cost instead of `$0`.

Providers fail independently. If at least one succeeds, the endpoint returns
200 plus `meta.sources` availability details. Only an all-provider failure
returns 503.

### 8.2 Server-side aggregation and identity

Claude's classified total is:

```text
fresh_input + output + cache_read + cache_write
```

For Codex, the exact per-thread total is the greater of the state DB total and
the latest parsed cumulative rollout total. Any gap between that total and the
four classified fields is explicit `unclassified` usage with
`breakdown_known: false`; unknown usage is never mislabeled as fresh input.
Top-level session `tokens`/`total_tokens` are tree-inclusive for both sources;
`own_tokens` excludes the subagents listed on that row. Codex aggregates token
classes across the same tree so the breakdown and total share one scope.

- Meaningless zero-usage roots are omitted, but a zero-token orchestration
  parent is retained when a descendant has usage.
- Claude parent totals cover every parent row; Codex usage totals include parent
  and child threads so subagent token burn is not lost.
- Each provider ships at most its newest 500 parent session records.
- Per-session tools are capped at the top 10.
- `daily_models` and `daily_tools` are not capped to those parent rows.
- Subagent IDs shaped like `parent/agent-…` are removed from the top-level count,
  nested under the parent, and attributed to that parent in daily-session totals.
- Codex spawn edges are recursively flattened under their root session and their
  daily token deltas are attributed to that root.
- Daily sessions are filtered to parents included in the shipped session list.
- Every session and rollup carries canonical `agent`; session joins use a
  collision-safe `<agent>:<native-id>` key while the native ID remains visible.
- Output sections: `meta`, `totals`, `daily_models`, `daily_sessions`, `sessions`,
  and `daily_tools`.

### 8.3 Client-side windows

Overview and Tools support 7 days by default plus Today, 30 days, and All. The
cutoff subtracts `N × 24h`, converts to a UTC `YYYY-MM-DD`, and compares date
strings inclusively. Therefore Today can include yesterday+today, 7 days can
cover eight calendar dates, and 30 days can cover 31.

Sessions, Models, and Trends are all-time views even when a window was selected
elsewhere.

Independent native checkboxes for Claude Code and Codex are both enabled on
first load and persist while switching Sessions subviews. Both off is valid and
renders an explicit “No session sources selected” state. Filtering happens at the raw `sessions`,
`daily_sessions`, `daily_models`, and `daily_tools` boundaries, so every metric
and table is recalculated rather than merely hiding list rows.

### 8.4 Subviews and formulas

#### Overview

- Window token total.
- Approximate cost, deliberately formatted with `~$`; marked partial whenever
  a selected source does not report cost.
- Distinct session count in the daily window.
- `tokens/session = window tokens / distinct window sessions`.
- Token area chart. Y maximum is the largest day; x points are uniformly spaced
  by row index rather than exact elapsed-day distance.
- Last-16-calendar-week all-time heatmap with intensity:

  ```text
  0.15 + 0.85 × sqrt(day_tokens / maximum_day_tokens)
  ```

- Top eight models by tokens.
- Top 10 sessions in the selected window.

#### Sessions

- Sortable, source-labelled all-time table.
- Detail: tree-inclusive total plus own-only total, four tree-scoped token
  classes, explicit unclassified usage and breakdown status when needed,
  estimated/unavailable cost, turns,
  duration, project/path, model, agent version, top tools, and subagents.

#### Tools

- Windowed call and error totals.
- `error rate = 100 × errors / calls`.
- Top 20 tools.
- MCP grouping parses `mcp__<server>__<tool>` and reports calls, errors, and
  distinct tools per server.

#### Models

- All-time tokens, share of total, and known/partial cost.
- Top 10 visual bars.

#### Trends

- All-time ISO-week token and cost totals.
- Top model for each week.

### 8.5 Refresh/render behavior

- Dataset loads only on first view mount.
- Refresh explicitly bypasses the current client value, but the server can still
  return its value from the 30-second cache.
- A ResizeObserver rerenders the active Sessions view after a width change over
  four pixels.
- Charts are local canvas/SVG-style primitives with no chart dependency.

## 9. Projects and commit relay

### 9.1 Project inventory

`GET /api/xo-projects` merges:

- scaffolded immediate directories with `.xo/project.json`;
- bare immediate directories without that file.

It removes absolute paths, marks `unscaffolded`, filters system leaves, and sorts
newest `created_at` first with alphabetical tie-breaking and null dates last.
There is no ID deduplication between the two input lists.

Live behavior: 36 rows are returned but only 35 IDs are unique. `git and its
history` appears once as scaffolded and once as bare. Both rows route operations
through the same normalized project ID, so the cards/options are ambiguous.

After loading the inventory and relay status, the view starts both a commit
request and a member request for every scaffolded row with no concurrency limit.
The live list has 35 scaffolded rows, so it generates 70 follow-up requests on
first open or manual Refresh. It does this even when the relay is parked and
sharing cannot succeed; the audit observed the expected member 404s plus swarm
502s for folders that had an origin but no usable swarm authentication.

### 9.2 Commit feed

For each card the UI calls:

```text
GET /api/xo-projects/{project_id}/commits?limit=15
```

The backend clamps limit to `1..50` and asks Git for `origin/<watch-branch>`,
falling back to `HEAD`. Rows contain full hash, subject, author, and committer ISO
date. The UI shows an eight-character hash.

Pending count is:

```text
git rev-list --count HEAD..origin/<watch-branch>
```

It means commits fetched into the local object/ref database but not applied to
HEAD; the relay never merges or checks out those commits. The UI labels the first
N returned rows as new, assuming Git's newest-first ordering matches the behind
range.

Git is invoked with `git -C <project-directory>`, which searches ancestor
directories for a repository. In the current workspace many project folders are
nested below one Git worktree, so their cards repeat that ancestor's commit feed.
The endpoint checks only that the folder exists, not that it owns a `.git` entry.

### 9.3 Sharing and members

- Origin URL is normalized to a repository identity.
- The browser talks only to the local BFF; the BFF carries the swarm token and
  workspace identity.
- Members are fetched from the swarm service.
- Share requires configured `PROJECT_ID`, recipient workspace ID, local origin,
  and valid swarm authentication.
- Revoke has the same requirements and is exposed only for owner-authorized
  rows.
- Buttons disable during the write; only that member section reloads afterward.

### 9.4 Relay engine

| State | Condition | Normal interval |
|---|---|---:|
| parked | `RELAY_ENABLED=false` or no `PROJECT_ID` | no network; wakes on dormant interval |
| dormant | workspace configured, no shared repo cloned here | ~600 s |
| active | at least one shared repo cloned here | ~50 s |
| drain | partial event page / missing fetched event | 5 s |

Normal intervals use ±20% jitter by default. A tick:

1. Enumerates immediate child directories that own `.git`.
2. Normalizes origins; duplicate clones of one repo are skipped as ambiguous.
3. Polls the swarm commit ledger once with per-repo cursors.
4. Fetches origins that have events.
5. Advances a cursor only through commits verified present locally.
6. Publishes newly pushed local hashes after the fresh membership check.
7. Records volatile in-memory status and recent events for the UI.

Relay status is lost on process restart and repopulates after a poll. The Projects
strip refreshes on tab show and every 60 seconds. Manual Refresh reloads inventory,
status, commits, and members. Relay-only polling continues while the view is
hidden.

### 9.5 Project card state precedence

1. unscaffolded;
2. no relay record → solo;
3. pending GitHub credentials/action;
4. last sync error;
5. shared, optionally with last-fetch age;
6. solo.

The live relay is enabled but parked because `PROJECT_ID` is absent, so the strip
states that sharing is disabled and cards mostly show `solo`.

## 10. Chat

### 10.1 UI structure

- New chat button.
- Latest-session sidebar, 50-item request.
- Session-title search, debounced 300 ms; fewer than two characters reload the
  normal list.
- Transcript area.
- Project selector used only when starting a new session.
- Multiline composer: Enter sends, Shift+Enter inserts a newline.

Selecting a session loads messages from its owning adapter. The request uses
`limit=50&offset=-1`; backend `offset=-1` currently returns the full transcript,
not only the final 50, to keep the rendered message set stable.

### 10.2 Transcript rendering

- User text is escaped/plain.
- Assistant text uses an escape-first Markdown subset: fenced and inline code,
  emphasis, links, headings, flat lists, task lists, quotes, rules, and tables.
- Stored reasoning is collapsible.
- Stored tool inputs/outputs are collapsible and truncated to 2,000/4,000
  characters.
- Tool and reasoning details are canonical transcript content, not live stream
  events in the current UI.

### 10.3 Prompt routing

Existing session:

```json
{"text": "...", "session_id": "..."}
```

New project-bound session:

```json
{"text": "...", "agent_id": "<project-id>"}
```

The backend detects the owning adapter for an existing session. New prompts use
the active `AGENT_NAME` unless explicitly resolved otherwise. An adapter may own
the prompt and SSE generator; otherwise the shared `AgentDispatcher` streams it.
This is the primary future Agent SDK seam.

### 10.4 Progressive stream

Flow:

1. `POST /api/chat/prompt` validates text and returns a stream/session ID.
2. Browser opens `GET /api/chat/stream/{stream_id}` with EventSource.
3. Supported named events:
   - `session-created`;
   - `text-delta`;
   - `model-loading` coarse progress;
   - `heartbeat`;
   - `agent-error`;
   - `error`;
   - `done`.
4. Text deltas append as plain text.
5. `done` closes the stream and reloads the canonical session/transcript, which
   re-enables Markdown, reasoning, and stored tools.

The shared dispatcher emits a heartbeat after 20 seconds of silence. The browser
checks every 10 seconds and declares failure after 45 seconds with no event.
Unknown event types are ignored, providing a compatible extension seam.

There is a 10-minute recently-started record to make immediate EventSource
reconnects end cleanly rather than show a spurious missing-stream error.

### 10.5 Stop and recovery limits

- Stop closes the browser stream and posts `/api/chat/abort`.
- The backend abort route removes an entry from the pending stream map. The
  dispatcher path removes that entry when SSE starts, so abort does not hold a
  cancellation handle for an already-running producer. The UI correctly warns
  that the agent may still finish server-side.
- Refreshing during a stream cannot reattach to and reconstruct the live partial
  response.
- Live tool/permission events are not exposed as first-class UI events.
- `/api/chat/active` is currently an empty-list stub.
- `/api/sessions/{id}/todos` and `/files` are currently empty stubs and are not
  used by this Chat view.

## 11. Experiment sandbox lifecycle

Experiment lists the existing `/api/xo-projects` inventory and sends only a
selected project ID. The backend resolves the immediate child under
`XO_PROJECTS_ROOT`; traversal, hidden/control-character IDs, files, and symlink
aliases are rejected.

The first provider is `local_docker`, explicitly marked local-development only:

1. preflight the SDK, Docker daemon/image, and a one-item Agents API list call;
2. create a self-hosted Agents API session for
   `/workspace/xo-projects/<selected-project>`;
3. independently clone/snapshot the selected project and current
   `xo-cowork-api` worktree into temporary staging;
4. recursively omit dotenv files, credential stores, dependencies, caches,
   build output, symlinks, and special files, and remove every Git remote;
5. mount only staging read-only, copy both trees into tmpfs-backed writable
   sandbox paths, and start sandbox Space plus `codex exec-server`;
6. bind Space to a dynamic loopback host port and verify `/health`, `/space/`,
   and an inventory containing exactly the selected project;
7. run a read-only boot turn, require terminal session status `idle`, verify the
   container is running, and then publish `ready` plus `space_url`;
8. accept serialized follow-up turns on the same Agents API session and expose
   a bounded, redacted transcript in the outer Space workbench;
9. delete staging, then release the container, Space URL, and session on Stop,
   one-hour default TTL, or normal API shutdown.

Containers carry ownership/reconciliation labels, run as an unprivileged user
with a read-only root filesystem, tmpfs workspace, all Linux capabilities
dropped, and `no-new-privileges`. Defaults are two CPUs, 4 GiB, 512 PIDs, two
concurrent experiments, and a one-hour TTL. Credentials enter Docker through
inherited environment values, never command arguments or API responses. Agent
output, transcripts, and public errors are bounded and credential-redacted.
The launcher explicitly states that the OpenAI-hosted agent can inspect both
filtered copies; filtering is defense in depth, not a proof that project
content is non-sensitive.

Lifecycle status is `starting → ready → stopping → stopped`; launch errors clean
up before becoming `failed`, while unverifiable cleanup becomes retryable
`cleanup_failed`. One active/resource-owning record per project is idempotently
reused. The UI preserves focused controls and drafts across changed poll
payloads, serializes Send/Stop writes, announces readiness, links to the
sandbox-local Space, and shows persistent lifecycle and turn errors.

This provider still assumes a trusted local user, a single API process, a
long-lived project API key inside the executor container, and Docker access. A
production Coder/XO deployment needs a workspace-native provider, shared durable
state, application-level ownership/auth, and scoped short-lived executor
credentials.

## 12. Visualizer watcher and `.xo` state touched by the wider system

The watcher starts as a non-fatal FastAPI lifespan task. It executes a blocking
tick in a worker thread, then sleeps one second; cadence is `tick duration + 1 s`,
not an exact 1 Hz.

The active agent adapter supplies the event source. In this audit it is
`claude_code`, which tails Claude JSONL and polls presence. Other adapters can
supply their own source without changing watcher core.

Per-project sinks maintain:

- `.xo/project.json` identity;
- `.xo/sessions/sessions-augment.json` counts/activity;
- `.xo/todos.json`;
- `.xo/stats.json`;
- `.xo/timeline.jsonl`;
- `.xo/activity.json`.

Workspace-tier aggregation maintains corresponding `.xo/workspace.json`, stats,
activity, session lists/augment, and timeline files. JSON rewrites use a temporary
file plus atomic replace; timelines append and fsync. Project timelines rotate at
8 MiB with five rotations; workspace timeline currently does not rotate.

Selected watcher calculations:

- 7d/30d rolls in an entire session when its **last** event falls inside the
  window; it is not event-exact.
- Daily stats retain the newest 35 UTC dates.
- Duration is last event minus first event, not active work time.
- `files_edited` is unique per session and then summed.
- User-to-next-assistant usage latency is capped at 10 minutes; daily approximate
  p95 uses a 100-sample reservoir.
- A live activity session is omitted until its model is known.

Atlas does not read these stats/timeline files. Its only indirect watcher
dependency is discovery: once the watcher creates `.xo/project.json`,
`list_projects()` can include that folder.

## 13. Refresh, polling, and lifetime matrix

| Area | Refresh model | Continues while hidden? |
|---|---|---|
| Footer liveness | immediate + every 5 s | yes |
| Atlas data | one browser fetch per page lifetime; server value cached 30 s | retained, no refetch |
| Graph draw | continuous RAF while Graph active; physics decays | RAF lives, draw/sim gated by active lens |
| Timeline | rebuild on entry/resize; optional playback RAF | playback can continue |
| Six Degrees | local calculation only | state retained |
| Sessions | lazy once; manual Refresh; 30 s server cache | ResizeObserver gates redraw to active view |
| Projects | full initial/manual load; relay status on show + every 60 s | interval continues |
| Chat list | initial/manual consequences only; no polling | in-flight EventSource continues unless closed |
| Chat stream watchdog | every 10 s; 45 s silence limit | tied to stream |
| Experiment | initial load + every 2 s while visible | interval cleared on hide |
| Visualizer watcher | tick + 1 s sleep | server background task |
| Commit relay | parked/dormant/active adaptive loop | server background task |

## 14. Live validation record

Validated through the running server and in-app browser, not only by reading
source:

- `/space/` loaded from port 5002 and footer reported the server online.
- Graph initially rendered 35 category controls and footer counts of 1,500
  artifacts, 152 clusters, 1,713 links, and 26 ties. After these documentation
  files added literal path references to the workspace, a fresh API build
  produced 40 ties and 1,727 links while the existing page retained its first
  dataset. This verified both the reference-tie rule and page-lifetime cache.
- Timeline rendered the 13 June–25 July range and a first-commit milestone.
- Surprise Me produced a real six-edge cross-project path and exposed the root
  bridge behavior.
- The live Sessions API returned 116 parent sessions: 58 Claude Code and 58
  Codex, with 4,867,409,623 combined tokens across 33 project paths. Known
  Claude cost was about $414.90; Codex cost remained correctly unavailable.
- In the browser, both checkboxes started checked and All rendered 4.8B tokens,
  a partial ~$415 estimate, 116 sessions, and rows from both sources. Claude-only
  and Codex-only each rendered 58 rows; neither-selected rendered the explicit
  empty state. Tools, Models, Trends, Overview, and session detail were each
  exercised, with correct source-specific cost behavior and no console errors.
- Projects rendered 36 cards, relay parked state, commits, behind counts, and
  sharing failure/degraded states.
- Chat rendered three current sessions, loaded a stored user/assistant exchange,
  and showed the project-binding composer. No prompt was sent during this audit.
- Experiment preflight verified SDK import, Docker/image readiness, and live
  Agents API authorization. A generated non-sensitive fixture was launched once
  through the API and once through the browser: both reached `ready` with an idle
  `gpt-5.5` session and READY boot summary in about ten seconds. Browser Stop
  cleared both resource IDs; no labelled Docker container remained; the fixture
  was removed. An existing private project was deliberately not transmitted.

## 15. Known drift, correctness risks, and test gaps

### Product/documentation drift

- Intro says “four departments” while live Atlas has 35 project hubs.
- Timeline says “thirteen months” while the retained live range is roughly six
  weeks including padding.
- Footer says `data: local file` even though the source is a dynamic JSON route.

### Correctness/scale risks

- Atlas caps and deadline silently make navigation incomplete except for console
  messages.
- Derived relationships cannot connect projects semantically.
- Dijkstra selects the minimum unvisited node with a full scan, making it
  quadratic in node count; acceptable at the enforced graph cap but not beyond it.
- Force repulsion iterates node pairs before applying its distance cutoff; the
  1,500-leaf cap is part of its practical performance envelope.
- Frame-based Timeline playback changes speed with display refresh rate.
- Six Degrees visible text can diverge from a previously selected internal node.
- Project inventory can emit duplicate IDs.
- Nested folders can inherit an ancestor Git feed.
- Projects performs an unbounded two-request-per-card fan-out and contacts the
  member/swarm path even when sharing is globally parked.
- Cold Atlas builds can block other API work; session collection is offloaded
  and concurrent cache misses are single-flight, but the first bounded Codex
  scan can still perform substantial local I/O in its worker.
- Cache freshness is TTL-only; Atlas frontend does not reload even after TTL.
- Absolute workspace/project/DB paths are returned to the browser.
- `/space` routes have no route-level auth dependency.
- Hidden view DOM can remain mounted. Live accessibility snapshots exposed the
  Chat project-select options while non-Chat views were active, suggesting the
  hidden-state/accessibility behavior needs a focused audit.

### Tests

- Twenty-eight backend regression tests cover Experiment lifecycle/snapshot/
  command safety plus source discovery/degradation,
  malformed-provider isolation, mixed-agent Argus filtering and all-time project
  counts, loader import failures, successful and failed single-flight route
  waves, Codex cumulative-token/state reconciliation, daily attribution,
  namespaced tools, zero-token parent trees and state-lag recovery,
  compatible-schema fallback, invalid-path fallback, malformed numeric rows,
  and exclusion of content-bearing fields.
- No dedicated frontend unit/component harness exists for `space_ui`; source
  toggle behavior is verified through the running UI.
- There is still no single test spanning browser → Atlas/Sessions/Projects/Chat/Experiment.
- The most valuable next regression suite would fixture the three data planes,
  assert formulas/counts, navigate all seven routes, and validate one real
  streamed agent turn with progress and text events.

## 16. Extension seams for Agent SDK work

- **Registry seam:** Experiment demonstrates that a seventh view can be added
  without changing shell routing internals.
- **API seam:** all UI calls already pass through one fetch boundary.
- **Agent seam:** `/api/chat/prompt` already delegates through an adapter
  capability or shared dispatcher and emits generic SSE events.
- **Event seam:** unknown SSE events are safely ignored; add typed tool lifecycle
  events without breaking existing clients.
- **Transcript seam:** canonical messages already represent assistant text,
  reasoning, and tool parts.
- **Project seam:** `agent_id` already binds a new session to a project folder.
- **Atlas tool seam:** deterministic read-only functions can expose artifact
  search, neighbors, path, and readiness context to an SDK agent without giving
  it filesystem write authority.
- **Observability seam:** SDK traces can be connected to the existing watcher,
  Argus projection, or a new trace-specific panel while keeping Atlas purely
  deterministic.

The implemented first increment boot-verifies a provider-neutral sandbox/session
lifecycle. The next useful increment is a prompt/stream surface for a ready
experiment with typed `tool-start/tool-result` progress and persisted canonical
transcript. After that, Graph selections can become structured agent context
without coupling the agent runtime into visualization code.
