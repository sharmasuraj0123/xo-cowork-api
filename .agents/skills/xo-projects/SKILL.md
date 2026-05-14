---
name: xo-projects
description: Use whenever an agent creates, operates inside, or manages an xo-project. Covers scaffolding a new xo-project via the xo-cowork-api; the boot ritual, canonical file map, and closing conventions for a project folder (AGENTS.md is the operating contract); recording and updating todos across a session; and backing up, restoring, syncing, snapshotting, or migrating xo-projects through the GitHub-backed sync API. Trigger this for any mention of xo-projects, .xo/, xo-cowork-api, or AGENTS.md, and for requests to back up / restore / sync / snapshot / migrate project state — even when the user doesn't name the skill explicitly. Two hard constraints hold from the moment the folder exists: new xo-projects are created only via the xo-cowork-api, and the agent never writes to .xo/ (a watcher service owns that directory).
---

# xo-projects

This skill covers the whole lifecycle of an xo-project. It does three things:

1. **Two hard constraints** (enforced outside the agent's judgment):
   - New xo-projects are created via the xo-cowork-api only.
   - The agent does not write to `.xo/` — a watcher service tails runtime logs and owns the entire directory.
2. **A guide to every file in an xo-project:** what it's for, how it lives across the session, and the reasoning behind the conventions — so the agent can apply judgment when an edge case appears.
3. **Pointers to two reference files** for situational, API-heavy detail — the todos HTTP API (only non-Claude-Code runtimes need it) and the backup/restore API — so they load only when the task actually calls for them.

## Base URL

```
http://${HOST:-localhost}:${PORT:-5002}
```

## How this skill is organized

Read this file top to bottom on first contact — the four parts below are all here in full, including todo discipline, which every session needs. Two reference files hold detail you only reach for situationally:

- **`references/todos-http-api.md`** — the todo HTTP endpoint schemas. Only non-Claude-Code runtimes need this; read it when you reach the "other runtimes → HTTP API" branch of Part 3.
- **`references/backup-restore.md`** — the GitHub-backed backup/restore/sync API. Read it when the user asks to back up, save, snapshot, sync, push, restore, pull, download, recover, or migrate projects.

Keeping these out of the main file means a routine session doesn't drag endpoint schemas or the entire backup API into context.

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

## Part 2 — Once inside an xo-project

`AGENTS.md` (or `CLAUDE.md`, which is just `@AGENTS.md`) is the operating contract for every xo-project folder, and the first thing the agent reads every session. **Read it before doing anything else.** It covers:

- **§2** — the canonical file map (top-level docs + `memory/` + `.xo/`)
- **§3** — first-boot behaviour: replacing `[TEMPLATE]` markers in `PROJECT.md` / `OBJECTIVES.md` / `PLAN.md` / `PROGRESS.md`
- **§4** — the boot ritual (what to read, in what order, before answering)
- **§5** — during-work conventions (and why there is no project-level `TASKS.json`)
- **§6** — the six-step closing ritual
- **§7** — `PROGRESS.md` format and the three logs
- **§8** — memory discipline (semantic / episodic / procedural / working)
- **§9** — hard rules
- **§10** — the index → filter pattern for looking up past sessions

The skill defers to AGENTS.md for all of these — read the section there rather than paraphrasing here.

Two guardrails to internalize **before** opening AGENTS.md, since they apply from the moment the folder exists:

1. **Never write to `.xo/`.** A background watcher service tails the runtime's native session logs (Claude Code's `~/.claude/projects/…`, OpenClaw's `~/.openclaw/agents/…`, etc.) and your in-flight todos, then writes session events, list/timeline entries, todos, stats, and activity heartbeats on your behalf. Any agent write conflicts with the watcher, gets overwritten, or corrupts sync state. This applies even to `.xo/project.json` on first boot — the watcher clears its `_template: true` flag itself.
2. **Work lives in the project folder.** Outputs scattered into `~/`, `/tmp/`, or sibling projects become orphaned from the project's memory, plan, and history — they don't get narrated in `PROGRESS.md`, don't get committed, and the next agent can't find them. If something genuinely must live outside, surface that to the user instead of doing it silently.

---

## Filesystem fallbacks (no HTTP endpoint yet)

- **List existing projects** — enumerate directories under the root from `/api/config/workspace`.
- **Read project metadata** — read `<project>/.xo/project.json`.
- **Rename or delete a project** — no API; do it via filesystem.

---

## Part 3 — Recording todos throughout the session

`<project>/.xo/todos.json` is the live, cross-session view of what's in flight. The frontend, the watcher, and peers all read from it, so **every agent keeps it accurate continuously** — not just once at boot. Create todos as the plan takes shape; flip status as work moves; close each one deliberately.

**Two write paths — pick the one for your runtime, never mix them:**

- **Claude Code agents → native todo tool.** Use the runtime's native `TaskCreate` / `TaskUpdate` / `TaskList`. The watcher tails Claude Code's session log and mirrors those todos into `.xo/todos.json` for you. Do **not** call the HTTP endpoints — you'd write every todo twice and the mirrored entries would conflict.
- **OpenClaw, Hermes, Codex, and every other runtime → HTTP API.** See **`references/todos-http-api.md`** for the endpoint schemas, the `runtime` / `session_id` conventions, and the conventional runtime values. Read it once when you hit this branch.

**Lifecycle (both paths):**

- **At session start** — list todos. Open work from previous sessions is the backlog you inherit.
- **As you plan** — one todo per concrete step; keep the content line short.
- **Starting work** — flip the next step to `in_progress`. Only one `in_progress` per agent at a time; the UI assumes that discipline.
- **Finishing** — mark it `completed` before moving on.
- **Stopping early** — `cancelled` (decided not to) or `blocked` (waiting on something). Keep the record; don't delete.

The todo list is the **live** view of in-flight work. Past, finished work belongs in `PROGRESS.md` at session close, not as `completed`-but-still-listed todos — see AGENTS.md §6.

---

## Part 4 — Backup & restore (GitHub-backed)

xo-cowork-api ships endpoints for encrypted, GitHub-backed backups of xo-projects, all under `/api/xo-projects-sync/`. The user never needs to open GitHub manually — the backend creates and manages the repo.

**Read `references/backup-restore.md`** when the user asks to back up, save, snapshot, sync, push, upload, restore, pull, download, recover, or migrate projects — or when they say they're moving to a new workspace. It covers the first-run setup flow (passphrase + repo), token resolution, per-project and bulk backup/restore, listing snapshots, what gets backed up, and the error table.