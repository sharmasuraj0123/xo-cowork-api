# XO Cowork API - Project Memory

## Project overview

- FastAPI backend that brokers chat and auth flows.
- Uses local `claude` CLI for coding/assistant responses.
- Primary API behavior should remain backward compatible.

## Architecture conventions

- Keep endpoint handlers in `routers/` via `APIRouter`.
- Keep business logic in focused clients/services (thin route handlers).
- Preserve request/response contracts unless explicitly asked to change.
- Routes that serve the xo-cowork frontend live under `routers/cowork_agent/`, with shared helpers under `services/cowork_agent/`.

## Agent-modular architecture (read before touching core)

- This is a **broker**: core code never names a specific agent. The active
  backend is resolved from `AGENT_NAME` through one seam — the capability loader
  `services/cowork_agent/adapters/loader.py` (`load_capability` /
  `try_load_capability`). A capability is a module `adapters/<name>/<cap>.py`; a
  missing one means the router returns its empty/501 shape, never a crash.
- **Agent-specific code lives in exactly three trees:**
  `services/cowork_agent/adapters/<name>/`, `config/agents/<name>/`, and
  `config/models/<name>/` (legacy Plane-A model clients). Nowhere else may name
  an agent (`openclaw`/`hermes`/`claude_code`/`codex`/`antigravity`) in code.
- Adapters are **auto-discovered** (`registry/adapter_registry.py`); there is no
  registry dict. Adding an agent = drop those folders, zero core edits.
- **Two planes:** Plane A = legacy `/ask_question*` via `config/models/<name>/`,
  selected by `AI_PROVIDER`. Plane B = `/api/*` via `AGENT_NAME` adapters.
- The one sanctioned core agent-literal is the `openclaw` **safe-boot default**
  in `registry/agent_registry.py` (deliberate — boots with no env configured).
- Full engineering guide: `DEVELOPING.md`.

## Two on-disk tiers (read before touching any project path)

Per-project state is split by *where it lives*, and that split is a share-safety
invariant, not a convention:

- **SYNCED** — `<project>/.xo/`, exactly `project.json`, `todos.json`,
  `peers.json`, `agent.json`. Bounded, meaningful to a collaborator on another
  machine, safe to commit. This is the A2A channel.
- **RUNTIME** — `~/.xo/<pid>/` (env `XO_RUNTIME_ROOT`), keyed by
  `project.json:pid`: `activity.json`, `stats.json`, `timeline*.jsonl`,
  `sessions/`. Machine-local, never synced. Plus `~/.xo/workspace/` for
  cross-project aggregation and `~/.xo/registry.json` as the pid↔folder map.

Runtime lives **outside every project tree** so it cannot be `git add`-ed,
tarred, or leaked — the template ships no `.gitignore` and the backup tarball
only honours one for git projects, so this could not rest on policy.

- **All tier/path math lives in `services/cowork_agent/project_layout.py`** and
  nowhere else. Never hand-build a `.xo` or `sessions` path: go through
  `sessions_dir()` / `project_runtime_dir()` / `xo_dir()`. Adapters pass a folder
  name and never learn about pids or tiers.
- `tests/test_path_chokepoint_guard.py` enforces this by AST scan. Keep it green
  by fixing paths, not by widening its allowlist — hand-built paths have already
  shipped four times across two adapters, because forking an adapter forks its
  paths.
- In the watcher, `project_json.fill_identity` **must** run before
  `project_runtime_dir` — the runtime root is keyed by the pid that call mints.
- Design + rationale: `docs/xo-runtime-tier-restructure.md` (local, gitignored).

## Coding standards

- Prefer clear, maintainable code over clever abstractions.
- Add robust error handling with actionable error messages.
- Use async patterns for network and subprocess operations.
- Avoid logging sensitive values (tokens, secrets, credentials).

## Validation and safety

- The project venv is `venv/bin/python` (system `python3` lacks fastapi).
- Test suite: `venv/bin/python -m pytest -q` (deps: `requirements-dev.txt`).
- After touching core, uphold the modularity invariant (no agent name in core
  code; see DEVELOPING.md §6). The AST guard for it is committed as
  `tests/test_modularity.py`.
- Import gate + route parity, expect **unique paths / openapi paths**:
  `claude_code 159/151`, `openclaw 159/151`, `hermes 183/175`, `codex 156/148`,
  `antigravity 158/150`. Use one fresh interpreter per agent
  (`AGENT_NAME=<a> venv/bin/python -c "import server; …"`) — `AGENT_NAME` is
  resolved once and frozen at import, so looping inside one process just tests
  the first agent five times. Count unique paths by recursing
  `original_router` (DEVELOPING.md §5): under Starlette 1.x `app.routes` holds
  `_IncludedRouter` proxies with no `.path`, so the naive
  `{r.path for r in server.app.routes if hasattr(r,'path')}` returns 15 for
  every agent — it fails OPEN. The whole matrix is pinned by
  `tests/test_import_parity.py`.
- Validate behavior after edits (lint, compile/tests where feasible).
- Keep changes minimal and targeted; behavior-preserving (no path/request/
  response changes unless explicitly asked).
- Maintain session behavior and existing auth flow semantics.

## Agent behavior preferences

- Start with concise implementation-oriented output.
- Call out assumptions and risks explicitly.
- Prefer production-safe defaults.
