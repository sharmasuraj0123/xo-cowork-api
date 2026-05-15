<div align="center">

<a href="https://xo.builders">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/xo-logo.svg">
    <source media="(prefers-color-scheme: light)" srcset="brand/xo-logo-light.svg">
    <img src="brand/xo-logo-light.svg" alt="XO" width="96" height="96">
  </picture>
</a>

# xo-cowork-api

**The local control plane for AI coding agents.**
One workspace, many runtimes — Claude Code, OpenClaw, Codex, Hermes, and whatever comes next.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109%2B-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-A0A0A0?style=flat-square)](#license)
[![Wiki](https://img.shields.io/badge/docs-wiki-2C2C2C?style=flat-square&logo=github)](https://github.com/sharmasuraj0123/xo-cowork-api/wiki)

</div>

---

`xo-cowork-api` is the FastAPI service that powers an **XO Cowork workspace**: a local control plane that runs inside every workspace, brokers chat to whichever coding agent runtime you've installed (Claude Code, OpenClaw, Codex), and owns the on-disk project model that travels with your work.

It does **not** train models, run inference, or compete with the agents — it stitches them together, adds the boring-but-critical glue (sessions, files, secrets, OAuth flows, usage reporting), and exposes one cohesive HTTP/SSE surface that the Tauri UI and any B2B client can build on.

```
                  ┌────────────────────────────────────────────┐
                  │              xo-cowork (Tauri UI)           │
                  │           or any HTTP/SSE consumer          │
                  └──────────────────────┬─────────────────────┘
                                         │ http://localhost:5002
                                         ▼
       ┌─────────────────────────────────────────────────────────────────┐
       │                       xo-cowork-api  (FastAPI)                   │
       │                                                                  │
       │   /api/chat/*         /api/sessions/*       /api/files/*         │
       │   /api/agents/*       /api/projects/*       /api/secrets/*       │
       │   /api/usage          /api/connectors/*     /xo-auth/*           │
       │                                                                  │
       │   ┌─────────────────────┐    ┌─────────────────────────────┐   │
       │   │  Runtime adapters   │    │  Connector services         │   │
       │   │   • Claude Code     │    │   • Google Drive (rclone)   │   │
       │   │   • OpenClaw        │    │   • OneDrive (rclone)       │   │
       │   │   • Hermes          │    │   • GitHub (PAT + gh CLI)   │   │
       │   │   • Codex (partial) │    │   • Vercel (OAuth + DCR)    │   │
       │   │   • + plug your own │    │   • Manus (API key)         │   │
       │   └─────────────────────┘    └─────────────────────────────┘   │
       └─────┬─────────────────────────────────────────────┬───────────┘
             │                                             │
             ▼                                             ▼
       runtimes on disk                              xo-swarm-api (cloud)
       ~/.claude/  ~/.openclaw/                     Clerk auth + usage sync
       ~/.hermes/  ~/.codex/
```

---

## Why it exists

Every coding agent ships with its own session store, its own auth, its own todo list, its own way of organising a workspace. The moment you want to **combine** them — or share a project, or measure usage across all of them, or just see a single chat history — you hit five incompatible filesystems and three half-baked CLIs.

`xo-cowork-api` is the part of the [XO Cowork](https://xo.builders) stack that puts a uniform API in front of all of them, keeps the project folder portable and sharing-safe by construction, and gives you back something you can build a product on.

- 🧠 **Pluggable runtimes** — one `BaseAgentAdapter` contract, one `/api/chat/*` surface. Claude Code, OpenClaw, and Hermes are first-class; Codex is partial; new runtimes plug in without router changes.
- 🗂️ **Sharing-safe project model** — chat content stays in the runtime's own storage (`~/.claude/`, `~/.openclaw/`). The project folder at `~/xo-projects/<id>/` is pure metadata + work files, structurally safe to share, fork, or rebase.
- 📡 **SSE streaming with sane reconnects** — `event: text-delta` / `done` / `heartbeat` / `agent-error`, React-Strict-Mode-safe via a 600 s reconnect window, server-side single-flight on conflicts.
- 🔌 **Connector hub** — Google Drive, OneDrive, GitHub (PAT + `gh` device flow), Vercel (OAuth 2.1 PKCE + Dynamic Client Registration), Manus. Each is dropped into `mcp-tokens.json` or `rclone.conf` and survives restarts.
- 🔐 **Clerk-backed identity** — browser poll-token flow with cowork-api as the trusted intermediary; tokens never reach the frontend.
- 📈 **Unified usage** — `/api/usage` reads JSONL from every runtime, returns one normalised shape with tokens, cost, model breakdowns, and response-time percentiles.
- 🛰️ **Local-first** — runs entirely on your machine. The only cloud call is to `xo-swarm-api` for identity verification and a daily usage sync. No telemetry, no exfiltration.

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/sharmasuraj0123/xo-cowork-api.git
cd xo-cowork-api

# 2. Install dependencies (Python 3.12+)
pip install -r requirements.txt

# 3. Configure
cp .env.example .env       # then edit — see Configuration below

# 4. Boot a runtime (pick at least one)
claude /login              # Claude Code
# or
bash openclaw.sh           # OpenClaw gateway on :18789

# 5. Run
python server.py           # http://localhost:5002

# or with auto-reload for development:
uvicorn server:app --host 0.0.0.0 --port 5002 --reload
```

Verify it's up:

```bash
curl http://localhost:5002/health
```

```jsonc
{
  "status":      "healthy",
  "stage":       "local",
  "ai_provider": "claude",
  "auth":        { "authenticated": true, "user_id": "user_2bX9...", "token_source": "session" },
  ...
}
```

### Process management

`cowork-api.sh` wraps the server with PID-file management and log redirection:

```bash
./cowork-api.sh start      # daemon
./cowork-api.sh status
./cowork-api.sh logs       # tail -f
./cowork-api.sh restart
./cowork-api.sh stop
```

---

## A turn, end to end

Every chat turn is two HTTP calls:

```bash
# 1. Prepare — returns {stream_id, session_id} fast
curl -sX POST http://localhost:5002/api/chat/prompt \
  -H 'Content-Type: application/json' \
  -d '{"text":"Refactor the auth flow to use Clerk"}'
# → {"stream_id":"8f3a...", "session_id":"9d4e..."}

# 2. Consume the SSE stream
curl -N http://localhost:5002/api/chat/stream/8f3a...
```

```
event: session-created   data: {"session_id":"9d4e..."}
event: text-delta        data: {"text":"Sure, "}
event: text-delta        data: {"text":"I can do that..."}
event: heartbeat         data: {}
event: done              data: {"finish_reason":"stop","session_id":"9d4e..."}
```

Full event vocabulary, reconnect semantics, and TypeScript example: see the [Frontend Chat API guide](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Frontend-Chat-Api).

---

## Pluggable runtimes

Adapters live under `services/cowork_agent/adapters/`. Each one implements [`BaseAgentAdapter`](services/cowork_agent/adapters/base.py): `run`, `stream`, `sessions_root`, `adapter_name`, `health`.

| Runtime | Status | Storage root | Transport |
|---|---|---|---|
| **Claude Code** | ✅ first-class | `~/.claude/projects/<encoded>/<sid>.jsonl` | `claude` CLI subprocess + `--output-format stream-json` |
| **OpenClaw** | ✅ first-class | `~/.openclaw/agents/<a>/sessions/<sid>.jsonl` | HTTP gateway on `:18789` (OpenAI-compatible SSE) |
| **Hermes** | ✅ first-class | `~/.hermes/profiles/<name>/` (or `~/.hermes/` for `default`) | `hermes` CLI subprocess + profile-based provider routing |
| **Codex** | 🟡 partial — auth + legacy chat | `~/.codex/...` | `codex` CLI subprocess (via `/ask_question*` legacy path) |
| **Your runtime** | 🔧 fork friendly | wherever you like | implement `BaseAgentAdapter`, register in `_REGISTRY`, drop a `commands.json` |

The router layer (`routers/cowork_agent/chat.py`) doesn't know which adapter it's talking to. It picks based on either an explicit `agent_name` in the request, on-disk session-ownership detection (`find_session_backend`), or the `AGENT_NAME` env var fallback. Adding a new runtime is **one file** + a registry entry.

Deep dive: [Claude Code vs OpenClaw](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Claude-Vs-Openclaw), [Streaming protocols compared](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Streaming-Claude-Vs-Openclaw).

---

## API surface at a glance

Roughly 100 endpoints. Every guide below is a full integration spec — request schemas, response shapes for every status code, edge cases, TypeScript examples.

| Family | Routes | Wiki guide |
|---|---|---|
| **Chat** | `/api/chat/{prompt,stream/{id},abort,respond}` + legacy `/ask_question*` | [Chat API](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Frontend-Chat-Api) |
| **Files** | `/api/files/{upload,list-directory,content,content-binary,save,mkdir}` | [Files API](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Frontend-Files-Api) |
| **Sessions** | `/api/sessions/*`, `/api/messages/{id}` | [Sessions & messages](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Frontend-Sessions-Messages-Api) |
| **Agents** | `/api/agents/*`, `/api/models`, `/api/config/*` | [Agents & config](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Frontend-Agents-Config-Api) |
| **Auth** | `/xo-auth/*`, `/claude/setup-token`, `/codex/setup`, `/openclaw/usage/*` | [Auth & setup](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Frontend-Auth-Api) |
| **Connectors** | `/api/connectors/{gdrive,onedrive,github,vercel,manus}/*` | [Connectors](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Frontend-Connectors-Api) |
| **Secrets & misc** | `/api/secrets/*`, `/api/usage`, `/api/onboarding/*`, `/api/channels/add` | [Misc](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Frontend-Misc-Api) |
| **Server** | `/health`, `/sessions`, `/gateway/restart`, `/app/{restart,update}` | [Server & lifecycle](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Frontend-Server-Api) |

📚 **Full wiki:** [github.com/sharmasuraj0123/xo-cowork-api/wiki](https://github.com/sharmasuraj0123/xo-cowork-api/wiki)

---

## Connectors

| Connector | Method | Where credentials live |
|---|---|---|
| **Google Drive** | `rclone authorize drive.file` + manual code paste; folder mgmt + 500 MiB streaming uploads | `rclone.conf` |
| **OneDrive** | `rclone authorize` Microsoft Graph | `rclone.conf` |
| **GitHub** | Personal Access Token paste **or** `gh auth login --web` device flow | `mcp-tokens.json` |
| **Vercel** | API token paste **or** OAuth 2.1 PKCE (Dynamic Client Registration on first use) | `mcp-tokens.json` |
| **Manus** | API key paste | `mcp-tokens.json` |

Each connector exposes `connect`, `status`, `disconnect`, `reconnect` plus per-service extras (`/sessions/{id}/submit` for rclone OAuth code paste; `/oauth/start` for Vercel; `/cli/{start,poll,cancel}` for GitHub device flow). The Drive connector additionally ships folder management (`mkdir`, `rmdir`, `folders`) and streaming uploads with no disk spool or RAM buffer.

A `:53682`-shared single-flight lock between Drive and OneDrive prevents concurrent rclone OAuth flows from colliding on the callback port. See the [Connectors guide](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Frontend-Connectors-Api).

---

## The xo-projects model

Every shared project is a folder under `~/xo-projects/<id>/` with a canonical layout:

```
~/xo-projects/blackhole/
├── AGENTS.md           ← agent operating contract (read first by agents)
├── CLAUDE.md           ← single line: "@AGENTS.md"
├── PROJECT.md          ← what this project is for
├── OBJECTIVES.md       ← OKRs
├── PLAN.md             ← current plan
├── PROGRESS.md         ← running narrative
├── memory/             ← semantic / episodic / procedural / working
└── .xo/                ← metadata-only — safe to share
    ├── project.json
    ├── sessions/sessionslist.json   ← sessionId ↔ runtime, NO message content
    ├── todos.json, stats.json, timeline.jsonl, activity.json
    └── sync.json, peers.json, policy.json
```

**The structural confidentiality guarantee:** no code path writes chat content into `~/xo-projects/`. Conversations live in the runtime's own home (`~/.claude/`, `~/.openclaw/`, `~/.codex/`), which never leaves the machine. A project folder can be `tar`'d, sync'd, or pushed to git without leaking session history or credentials.

Create a project with the scaffolding endpoint:

```bash
PROJECTS_ROOT=$(curl -s http://localhost:5002/api/config/workspace | jq -r '.roots[.default]')

curl -sX POST http://localhost:5002/api/files/mkdir \
  -H 'Content-Type: application/json' \
  -d "{\"path\":\"${PROJECTS_ROOT}/blackhole\",\"scaffold\":true,\"display_name\":\"Blackhole\",\"description\":\"Internal research\"}"
```

The bundled `project_template/` materialises every file above; subsequent invocations are idempotent (existing files are never overwritten).

---

## Configuration

Full reference in [`.env.example`](.env.example). Most useful knobs:

| Variable | Purpose | Default |
|---|---|---|
| `HOST`, `PORT` | Bind address | `0.0.0.0:5002` |
| `STAGE` | `local` (dev: discover CLI via `which`) or `beta` (container: `/home/coder/...`) | `beta` |
| `AGENT_NAME` | Active backend for `/api/agents` & `/api/models` | `openclaw` |
| `XO_PROJECTS_ROOT` | Canonical projects root | `~/xo-projects` |
| `CLAUDE_CLI_PATH` | `claude` binary location | autodiscovered if `STAGE=local` |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude CLI auth | from `claude /login` |
| `OPENCLAW_API_URL` | OpenClaw gateway endpoint | `http://127.0.0.1:18789/v1/chat/completions` |
| `OPENCLAW_GATEWAY_TOKEN` | OpenClaw bearer | required for OpenClaw path |
| `CHAT_API_BASE_URL` | xo-swarm-api upstream | `https://api-swarm-beta.xo.builders` |
| `XO_API_KEY` | Long-lived Clerk PAT (skips the consume flow) | unset |
| `USAGE_SYNC_HOUR_UTC` | Daily usage sync time | `02` |

Auth flow: if `XO_API_KEY` is set, it's used as Bearer for every outbound call. Otherwise, run the `/xo-auth/start` → browser → `/xo-auth/consume` flow (or set `XO_AUTH_SESSION_ID` + `XO_POLL_TOKEN` to consume once at startup).

---

## Documentation

The wiki is the canonical reference, kept in sync with the code:

- 🏗️ [Architecture](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Architecture) — snapshot of the current state, route inventory, vision scorecard
- 📑 [Frontend API index](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Frontend-Api-Index) — start here for integration
- 🛠️ [Visualizer + peer-sync plan](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Visualizer-And-Sync-Plan) — the active roadmap
- 🔒 [RBAC plan](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Rbac-Plan) — multi-user authorization design
- 📊 [OpenClaw usage sync flow](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Openclaw-Usage-Sync-Flow)

19 pages in total. Every guide is a full integration spec, not a quick-start.

---

## Status

Maturity, honestly assessed (full breakdown in the [Architecture scorecard](https://github.com/sharmasuraj0123/xo-cowork-api/wiki/Architecture#10-vision-achievement-scorecard)):

| Capability | Status |
|---|---|
| Multi-runtime adapter layer (Claude Code + OpenClaw + Hermes) | ✅ ~90% — Codex still legacy-only |
| Project ↔ agent decoupling + scaffold | ✅ ~90% |
| Sharing-safe project folder | ✅ ~95% |
| Memory subsystem (semantic / episodic / procedural / working) | ✅ ~90% |
| Connectors (Drive, OneDrive, GitHub, Vercel, Manus) | ✅ ~88% |
| Unified usage / dashboard | 🟡 ~40% — watcher service still pending |
| BFF layer for projects + secrets | 🟡 fresh — added 2026-05-12 |
| Peer-sync between cowork instances | 🔴 0% — Phase 1 spec is in the wiki |
| B2B chat-proxy hardening | 🔴 0% |
| RBAC across instances | 🔴 0% — Clerk identity ready, no peer/grant code yet |

---

## Project structure

```
xo-cowork-api/
├── server.py                       FastAPI app — lifespan, CORS, router mounts
├── routers/
│   ├── auth.py                     /xo-auth/* — Clerk poll-token + token store
│   ├── claude_setup_token.py       /claude/* — Claude CLI OAuth bootstrap
│   ├── codex_setup.py              /codex/* — Codex device-auth
│   ├── openclaw_usage.py           /openclaw/usage/* — analytics
│   └── cowork_agent/               /api/* — the cowork frontend-facing surface
│       ├── chat.py     sessions.py  agents.py  config.py  files.py
│       ├── secrets.py  channels.py  workspace_memory.py   fts.py
│       ├── usage.py    misc.py      onboarding.py
│       ├── bff/        ←── projects + secrets BFF layer
│       └── {gdrive,onedrive,github,vercel,manus}.py
├── services/
│   ├── cowork_agent/
│   │   ├── dispatcher.py            AgentDispatcher — what routers import
│   │   ├── adapter_registry.py      {"openclaw": …, "claude_code": …}
│   │   ├── adapters/                pluggable runtime plug-ins
│   │   │   ├── base.py              BaseAgentAdapter (abstract)
│   │   │   ├── openclaw/{adapter,streaming,usage}.py
│   │   │   ├── claude_code/{adapter,streaming,models_status,providers_status}.py
│   │   │   └── hermes/{adapter,models_status,dump,…}.py
│   │   ├── project_layout.py        ~/xo-projects/ layout + scaffold_project
│   │   ├── project_template/        bundled scaffold tree (AGENTS.md, .xo/, memory/, …)
│   │   ├── sessions_io.py           cross-runtime session lookup
│   │   ├── streaming.py             OpenClaw direct-streaming bridge
│   │   ├── *_connector.py           rclone, gh, GitHub, Vercel, Manus glue
│   │   └── …                        helpers, settings, manifests
│   └── usage_sync.py                daily background → /usage/report on swarm
├── config/agents/{openclaw,claude_code}/{commands,settings}.json
├── cowork-api.sh                    process manager (start|stop|restart|status|logs)
├── cowork-update.sh                 git pull + restart in background
├── openclaw.sh                      installs + launches OpenClaw gateway on :18789
├── hermes.sh                        hermes config setup
├── docs/                            local copies of every wiki page (gitignored)
├── Dockerfile
└── requirements.txt
```

---

## Contributing

Issues and PRs welcome on the [`development` branch](https://github.com/sharmasuraj0123/xo-cowork-api/tree/development). The codebase is deliberately small (a few thousand lines of Python); changes that touch the adapter contract, the session model, or the project-folder layout deserve a wiki update too.

Conventions:

- **Endpoints live in `routers/`** (thin handlers). Logic lives in `services/`. Top-level `server.py` is the only file that imports both.
- **Adapters subclass `BaseAgentAdapter`** and register in `services/cowork_agent/adapter_registry.py`. No router changes needed to add a runtime.
- **The project folder is sacred.** Don't write chat content, runtime credentials, or anything else that wouldn't survive a git push into `~/xo-projects/<id>/`.

---

## License

MIT. See [LICENSE](LICENSE) (forthcoming) or treat the badge above as authoritative for now.

---

<div align="center">

Built for <a href="https://xo.builders">XO Cowork</a> · Part of the <a href="https://github.com/sharmasuraj0123">XO</a> stack

</div>
