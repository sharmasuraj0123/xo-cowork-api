---
name: xo-projects
description: Use when an agent needs to create a new xo-project, or when an agent is operating inside one. Project creation goes through the xo-cowork-api and .xo/ is backend-owned. Everything else is described as principles with the reasoning behind them, so the agent has judgment for edge cases — not a list of rules to obey.
---

# xo-projects

This skill does two things:

1. **Two hard constraints** (enforced outside the agent's judgment):
   - New xo-projects are created via the xo-cowork-api only.
   - The agent does not write to `.xo/` — a watcher service tails runtime logs and owns the entire directory.
2. **A guide to every file in an xo-project:** what it's for, how it lives across the session, and the reasoning behind the conventions — so the agent can apply judgment when an edge case appears.

## Base URL

```
http://${HOST:-localhost}:${PORT:-5002}
```

---

## Part 1 — Creating a project (hard constraint)

```
GET /api/config/workspace
→ {"roots": {"openclaw": "/home/coder/xo-projects"}, "default": "openclaw"}
```
Always call this first. Never hard-code `~/xo-projects`.

```
POST /api/files/mkdir
{ "path": "<projects_root>/<id>",
  "scaffold": true,
  "display_name": "My App",     // optional → seeds template
  "description": "..." }        // optional → seeds template
→ 200 {"path": "...", "name": "<id>", "copied": []}
→ 400 if path.parent ≠ projects_root
→ 409 if folder already exists
```

The backend scaffolds the canonical tree (top-level docs + `memory/` + `.xo/`) from a template. Top-level docs (`PROJECT.md`, `OBJECTIVES.md`, `PLAN.md`, `PROGRESS.md`) start with `[TEMPLATE]` markers — the agent fills those in on first boot (ask the user when scope is unclear). `.xo/project.json` starts with `_template: true`; the watcher service clears that flag once identity is resolved — the agent never touches it. Hand-rolling the layout means missing files or drift from the structure the backend expects.

---

## Part 2 — The workspace

Once operating in an xo-project, the project folder is where the work lives. Outputs scattered into `~/`, `/tmp/`, or sibling projects become orphaned from the project's memory, plan, and history — they don't get narrated in `PROGRESS.md`, don't get committed, and the next agent can't find them. If something genuinely must live outside the folder, surface that to the user instead of doing it silently.

---

## Part 3 — The files

### Top-level docs

Each has a purpose. The agent's job is to keep them honest as work happens.

**`AGENTS.md`** — the operating contract for the folder. Read first every session. Authored by the project template and rarely edited; if the contract genuinely needs to evolve for *this* project, edit it deliberately and note why in `PROGRESS.md`. Most sessions will not touch it. **`CLAUDE.md`** is a one-line `@AGENTS.md` import for Claude Code — never edit.

**`PROJECT.md`** — what this folder is for: scope, audience, stack. Stable; edit when scope actually changes, not per session. Starts with `[TEMPLATE]` markers — fill those in on first boot (ask the user if anything is unclear).

**`OBJECTIVES.md`** — north-star outcomes (OKR-style). Stable on the order of weeks. Edit when objectives genuinely shift, not when tasks shift — current plan lives in `PLAN.md`, in-flight todos in your runtime's native todo tool (mirrored to `.xo/todos.json`).

**`PLAN.md`** — the current ~1–5 day plan; the bridge between OBJECTIVES and in-flight todos. When the plan changes, edit it. A stale plan is the most common way an agent misleads the next agent. Move superseded plans to "Recently superseded" as a one-liner — don't delete.

**`PROGRESS.md`** — the running narrative of finished sessions. Future agents read this as ground truth at boot (last ~30 lines only). Append-only at session close, paragraph format `## YYYY-MM-DD — [outcome] headline` where `[outcome]` ∈ `shipped | progress | blocked | pivoted | cleanup | research`. Writing mid-session captures work before it has a coherent shape; editing prior entries silently rewrites history other agents rely on. If a past entry turns out wrong, append a new entry that supersedes it instead of editing.

> There is **no project-level `TASKS.json`** in this template. In-session todos go through the runtime's native todo tool (e.g. Claude Code's `TaskCreate` / `TaskUpdate`); the watcher mirrors them into `.xo/todos.json`. To see in-flight todos across all active sessions, *read* `.xo/todos.json` — never write to it.

### `memory/` — shared cognition

Committed to git. Distilled, not raw. Four flavors, each with a specific job.

**`semantic/`** — distilled facts, one claim per line, no narrative. Three files by convention (`preferences.md`, `project-facts.md`, `constraints.md`) because semantic memory is auto-loaded into every session's prefix; new files would balloon that prefix. Update only when a fact has been observed twice or stated explicitly by the user — that's what separates a settled fact from a passing impression. If a class of fact genuinely doesn't fit the three, surface that to the user rather than quietly adding a new file.

**`episodic/`** — append-only narrative of noteworthy events, one file per episode (`YYYY-MM-DD-{slug}.md`). Write only for non-trivial decisions, hard problems solved, unexpected failures, or strong user feedback. Routine work doesn't deserve an episode. Episodes are append-only because summarizing or editing destroys the raw signal that made them worth writing — if the context changed or the original turned out wrong, write a *new* episode that references the old one by filename. That preserves both the history and the correction.

**`procedural/`** — reusable how-to skills. Write a skill **only after a workflow has succeeded ≥2 times**. One success is an episode, not a pattern. Procedurals get trusted blindly by future agents; a skill written from a single success is fabricated procedural knowledge — the most dangerous form of memory pollution.

**`working/`** — session scratchpad. Anything you'd write on a whiteboard. Wipe at session close (`rm -f memory/working/*` except `.gitkeep`); leaving it accrues noise that drowns out the durable memory layers.

### `.xo/` — watcher-owned (hard constraint)

```
.xo/                          ← ephemeral state. Gitignored.
├── project.json              identity: pid, name, owner_user_id, created_at
├── todos.json                aggregated todos across active sessions
├── stats.json                rolling 7d/30d: tokens, models, files, sessions, time
├── timeline.jsonl            append-only event log (sessions, todos, edits, syncs)
├── peers.json                who this folder is shared with
├── sync.json                 last-sync state per peer
├── activity.json             live: which sessions are open right now
├── sessions/
│   └── sessionslist.json     index of past sessions — read this for history
└── schema/                   JSON Schemas for every file above (read-only reference)
```

A background **watcher service** owns the entire directory. It tails the runtime's native session logs (Claude Code's `~/.claude/projects/…`, OpenClaw's `~/.openclaw/agents/…`, etc.) and your in-flight todos, then writes session events, list/timeline entries, todos, stats, and activity heartbeats on your behalf. The agent **only reads** `.xo/`. Any agent write will conflict with the watcher, get overwritten, or corrupt sync state — there's no exception, not even for `.xo/project.json` on first boot.

**First boot:** when `.xo/project.json` has `_template: true`, the watcher generates a UUID, fills `pid` / `name` / `owner_user_id` / `created_at` from the harness, and removes the flag. If it's still in template state when you boot, either wait briefly or read identity from the harness env — don't write.

**Looking up past sessions** (when the user references prior work, or you need history older than the last 3 sessions):

1. Open `.xo/sessions/sessionslist.json` and find the relevant `id` by `started_at`, `summary`, or `outcome`. Small file, cheap to scan.
2. Filter `.xo/timeline.jsonl` by that `session_id` (e.g. `grep '"session_id":"ses_abc123"' .xo/timeline.jsonl`). Never pull the full timeline into the main thread.
3. For narrative detail, follow the entry's `episode_refs` into `memory/episodic/` — dispatch a subagent; never main-thread.
4. Every JSON file has a schema in `.xo/schema/`; if a shape is unfamiliar, read the schema before guessing.

---

## Part 4 — Lifecycle in one page

**At session start (boot):** read `AGENTS.md` for the full ritual. Minimum order: `AGENTS.md` → `PROJECT.md` → `OBJECTIVES.md` → `PLAN.md` → `memory/semantic/*.md` → last ~30 lines of `PROGRESS.md` → `.xo/todos.json` → last 3 entries of `.xo/sessions/sessionslist.json` → `.xo/activity.json` (is anyone else here right now?). Don't pull `memory/episodic/`, `memory/procedural/`, the full `sessionslist.json`, or `.xo/timeline.jsonl` into the main thread — they grow without bound; use the index → filter pattern in Part 3 (also `AGENTS.md §10`) when you need them. No need to announce yourself; the watcher logs `session.start` from your runtime's native session log.

**During work:** keep `PLAN.md` reflecting the current state. Use `memory/working/` as scratchpad. For in-session todos, use the runtime's native todo tool (e.g. Claude Code's `TaskCreate` / `TaskUpdate`) — the watcher mirrors them into `.xo/todos.json`. `PROGRESS.md` stays untouched until close; `.xo/` is never touched at all.

**At session close (six steps, from `AGENTS.md §6`):**

1. If the session contained a non-trivial decision, hard problem solved, unexpected failure, or strong user feedback → write `memory/episodic/YYYY-MM-DD-{slug}.md`. Routine work does not deserve an episode.
2. Append one paragraph to `PROGRESS.md` (newest at the bottom), using the `## YYYY-MM-DD — [outcome] headline` format.
3. Distill any new semantic facts into `memory/semantic/*.md` — one claim per line, only if observed twice or explicitly stated by the user.
4. If a workflow has now succeeded ≥2 times, promote it to `memory/procedural/{slug}.md`. One success is not a pattern.
5. If scope shifted, update `PLAN.md`; move the superseded plan to "Recently superseded" as a one-liner.
6. Wipe `memory/working/` (`rm -f memory/working/*` except `.gitkeep`).

All `.xo/` updates (`sessionslist.json`, `timeline.jsonl`, `activity.json`, `stats.json`, `todos.json`, `sync.json`) are written by the watcher from your runtime's native logs — no manual step.

---

## Filesystem fallbacks (no HTTP endpoint yet)

- **List existing projects** — enumerate directories under the root from `/api/config/workspace`.
- **Read project metadata** — read `<project>/.xo/project.json`.
- **Rename or delete a project** — no API; do it via filesystem.

---

## Part 5 — Backup & restore (GitHub-backed)

xo-cowork-api ships endpoints for encrypted, GitHub-backed backups of xo-projects. All routes are under `/api/xo-projects-sync/` on the same base URL as the rest of cowork-api. **The user never needs to open GitHub manually** — the backend creates and manages the repo for them.

### When to use

Triggers: user asks to *back up*, *save*, *snapshot*, *sync*, *push*, *upload*, *restore*, *pull*, *download*, *recover*, or *migrate* their projects. Also when the user says they're moving to a new workspace.

### Placeholders used in this section

The examples below use slot names that you substitute at call time:

| Slot | Meaning |
|---|---|
| `{project_id}` | URL path parameter — the directory name under `~/xo-projects/`. Example real value: `research`. |
| `<project_id>` | The same value when it appears inside a JSON body or response. |
| `<snapshot_id>` | A snapshot identifier in `YYYYMMDD-HHMMSS` UTC format. Example: `20260511-153000`. |
| `<owner>` | The GitHub login the connected token belongs to. Discovered by the backend; never set by the caller. |
| `<xo-projects-root>` | The local xo-projects directory, usually `/home/coder/xo-projects`. |

### Endpoints

```
POST   /api/xo-projects-sync/setup
GET    /api/xo-projects-sync/status
GET    /api/xo-projects-sync/projects
POST   /api/xo-projects-sync/projects/{project_id}
POST   /api/xo-projects-sync/all
POST   /api/xo-projects-sync/projects/{project_id}/restore
POST   /api/xo-projects-sync/all/restore
```

### First-run flow

Always start with `GET /api/xo-projects-sync/status`:

```json
GET /api/xo-projects-sync/status
→ {
    "configured": false,
    "repo_name": null,
    "token_source": "connector" | "env" | null,
    "gpg_available": true
  }
```

If `configured: false`:

1. Ask the user for a **passphrase** they'll remember. Tell them: "Write this down. Without it, none of your backups can ever be restored — not even by me."
2. Ask for a **repo name**, suggest the default `xo-projects-backup`.
3. Call setup:

```json
POST /api/xo-projects-sync/setup
{ "repo_name": "<repo_name>", "passphrase": "<from user>" }
→ {
    "configured": true,
    "repo_owner": "<owner>",
    "repo_name": "<repo_name>",
    "repo_url": "https://github.com/<owner>/<repo_name>.git",
    "repo_created": true | false,
    "token_source": "connector" | "env"
  }
```

Setup persists `BACKUP_REPO_NAME` + `BACKUP_PASSWORD` into `xo-cowork-api/.env`, ensures the GitHub repo exists (creates as private if missing — using `gh` CLI first, REST API fallback), and updates the running process's env in place. It's idempotent: re-running with the same values is a no-op.

### Token resolution

If `token_source: null` or any endpoint returns **401**:

1. Tell the user one of:
   - "Complete the GitHub connector flow in xo-cowork UI", or
   - "Add `GITHUB_PAT=<your-token>` to `~/xo-cowork-api/.env`. If you'll also run `gh` directly, put it in your shell env too."
2. Do **not** write the PAT to either file for them — they must do that step manually.
3. After they confirm it's set, retry the original call.

The token needs `repo` scope to create + push to a private repo.

### Backup

```json
POST /api/xo-projects-sync/projects/{project_id}
{ "note": "<optional short note>" }
→ {
    "project_id": "<project_id>",
    "snapshot_id": "<snapshot_id>",
    "size_bytes": 423618,
    "sha256": "…",
    "parts": 1,
    "ok": true,
    "error": null
  }
```

For all projects in one go:

```json
POST /api/xo-projects-sync/all
{ "note": "<optional short note>" }
→ [
    { "project_id": "<project_a>", "snapshot_id": "<snapshot_id>", "size_bytes": 423618, "ok": true, "error": null },
    { "project_id": "<project_b>", "ok": false, "error": "git ls-files failed: ..." }
  ]
```

Bulk backup is independent-per-project: a failure on one project does NOT abort the others. Each entry carries its own `ok` and optional `error`.

### List remote snapshots

```json
GET /api/xo-projects-sync/projects
→ [
    {
      "project_id": "<project_id>",
      "snapshots": [
        { "id": "<snapshot_id_newer>", "created_at": "2026-05-11T15:30:00+00:00", "size_bytes": 423618 },
        { "id": "<snapshot_id_older>", "created_at": "2026-05-10T10:00:00+00:00", "size_bytes": 421104 }
      ]
    }
  ]
```

Snapshots are sorted newest-first. The backend keeps the last 10 per project — older ones are auto-pruned on the next backup.

### Restore

Default behavior **refuses** to overwrite an existing local project — restoring blindly would clobber uncommitted local work:

```json
POST /api/xo-projects-sync/projects/{project_id}/restore
{}                          // body optional; defaults to latest snapshot
→ 409 {
    "detail": {
      "error": "project_exists",
      "detail": "Project folder already exists at <xo-projects-root>/<project_id>.",
      "suggestion": "Pass force=true in the body to overwrite; existing local data will be lost."
    }
  }
```

On 409, **present the message verbatim to the user and ask them to confirm**. Only retry with `force: true` after explicit user confirmation:

```json
POST /api/xo-projects-sync/projects/{project_id}/restore
{ "force": true }
→ {
    "project_id": "<project_id>",
    "restored_from": "<snapshot_id>",
    "target": "<xo-projects-root>/<project_id>",
    "ok": true,
    "error": null,
    "error_code": null
  }
```

Pin a specific snapshot with `snapshot_id`:

```json
{ "snapshot_id": "<snapshot_id>", "force": true }
```

Bulk restore:

```json
POST /api/xo-projects-sync/all/restore
{ "force": true, "snapshot_id_map": { "<project_a>": "<snapshot_id>" } }
→ [
    { "project_id": "<project_a>", "restored_from": "<snapshot_id>", "target": "...", "ok": true },
    { "project_id": "<project_b>", "ok": false, "error_code": "exists", "error": "..." }
  ]
```

`snapshot_id_map` is optional and per-project; missing entries use the latest snapshot for that project. `force` applies to every project. Each project's result is independent — a 409-equivalent on one doesn't abort the rest.

### What gets backed up

- Project is tarred + gzipped → encrypted with `gpg --symmetric --cipher-algo AES256` using `BACKUP_PASSWORD` → split into ≤95 MB parts to stay under GitHub's 100 MB file limit.
- If the project is a git repo, `.gitignore` is respected (via `git ls-files --cached --others --exclude-standard`). Non-git projects skip only the mandatory excludes (their `.gitignore` is NOT consulted in v1).
- Mandatory excludes regardless of `.gitignore`: `.env`, `.env.*`, `.git/`, `node_modules/`, `.venv/`, `__pycache__/`, `*.sock`.

### Common error responses

| Status | When | Surface to user |
|---|---|---|
| 400 `not_configured` | `/setup` hasn't been called yet | Run setup first; ask for passphrase + repo name |
| 401 `github_auth_missing` | No connector token AND no `GITHUB_PAT` | Set up auth (UI flow OR env var); see Token resolution above |
| 404 `project_not_found` | Local project doesn't exist (backup) | The project hasn't been created yet — list projects or scaffold first |
| 404 `snapshot_not_found` | Remote has no snapshot for that project / snapshot_id wrong | Check `GET /projects` for valid ids |
| 409 `project_exists` | Restore target exists locally | Ask user to confirm overwrite, then retry with `force: true` |
| 500 `gpg_missing` | `gpg` not installed on host | Run `sudo apt-get install -y gnupg` (host responsibility, not user-fixable from chat) |
| 502 `verify_failed` | Snapshot sha256 doesn't match manifest | Snapshot is corrupted; try a different snapshot id |
| 502 `repo_create_failed` | Token lacks `repo` scope or GitHub rejected | Regenerate PAT with `repo` scope |
