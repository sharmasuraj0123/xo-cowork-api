---
name: xo-projects
description: Use when an agent needs to create a new xo-project, or when an agent is operating inside one. Project creation goes through the xo-cowork-api and .xo/ is backend-owned. Everything else is described as principles with the reasoning behind them, so the agent has judgment for edge cases — not a list of rules to obey.
---

# xo-projects

This skill does two things:

1. **Two hard constraints** (enforced outside the agent's judgment):
   - New xo-projects are created via the xo-cowork-api only.
   - The agent does not write to `.xo/` — it's backend-owned and the endpoints will be added later.
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

The backend scaffolds the canonical tree (top-level docs + `memory/` + `.xo/`) from a template. Every doc starts with `[TEMPLATE]` markers and `.xo/project.json` starts with `_template: true`. Hand-rolling the layout means missing files or drift from the structure the backend expects.

---

## Part 2 — The workspace

Once operating in an xo-project, the project folder is where the work lives. Outputs scattered into `~/`, `/tmp/`, or sibling projects become orphaned from the project's memory, plan, and history — they don't get narrated in `PROGRESS.md`, don't get committed, and the next agent can't find them. If something genuinely must live outside the folder, surface that to the user instead of doing it silently.

---

## Part 3 — The files

### Top-level docs

Each has a purpose. The agent's job is to keep them honest as work happens.

**`AGENTS.md` / `CLAUDE.md`** — the operating contract for the folder. Read first every session. Authored by the project template and rarely edited; if the contract genuinely needs to evolve for *this* project, edit it deliberately and note why in `PROGRESS.md`. Most sessions will not touch them.

**`PROJECT.md`** — what this folder is for: scope, audience, stack. Stable; edit when scope actually changes, not per session. Starts with `[TEMPLATE]` markers — fill those in on first boot (ask the user if anything is unclear).

**`OBJECTIVES.md`** — north-star outcomes (OKR-style). Stable on the order of weeks. Edit when objectives genuinely shift, not when tasks shift — tasks belong in `PLAN.md` / `TASKS.json`.

**`PLAN.md`** — the current ~1–5 day plan; the bridge between OBJECTIVES and TASKS. When the plan changes, edit it. A stale plan is the most common way an agent misleads the next agent.

**`PROGRESS.md`** — the running narrative of finished sessions. Future agents read this as ground truth at boot. The convention is append-only at session close: writing mid-session captures the work before the session has a coherent shape, and editing prior entries silently rewrites history other agents are relying on. If a past entry turns out wrong, append a new entry that supersedes it instead of editing.

**`TASKS.json`** — machine-readable task list (`{$schema, next_id, tasks[]}`). Mark tasks `in_progress`/`done` as you go; new work → add a `T-NNN` row and bump `next_id`. The monotonic `next_id` is what keeps task IDs stable for cross-session references.

### `memory/` — shared cognition

Committed to git. Distilled, not raw. Four flavors, each with a specific job.

**`semantic/`** — distilled facts, one claim per line, no narrative. Three files by convention (`preferences.md`, `project-facts.md`, `constraints.md`) because semantic memory is auto-loaded into every session's prefix; new files would balloon that prefix. Update only when a fact has been observed twice or stated explicitly by the user — that's what separates a settled fact from a passing impression. If a class of fact genuinely doesn't fit the three, surface that to the user rather than quietly adding a new file.

**`episodic/`** — append-only narrative of noteworthy events, one file per episode (`YYYY-MM-DD-{slug}.md`). Write only for non-trivial decisions, hard problems solved, unexpected failures, or strong user feedback. Routine work doesn't deserve an episode. Episodes are append-only because summarizing or editing destroys the raw signal that made them worth writing — if the context changed or the original turned out wrong, write a *new* episode that references the old one by filename. That preserves both the history and the correction.

**`procedural/`** — reusable how-to skills. Write a skill **only after a workflow has succeeded ≥2 times**. One success is an episode, not a pattern. Procedurals get trusted blindly by future agents; a skill written from a single success is fabricated procedural knowledge — the most dangerous form of memory pollution.

**`working/`** — session scratchpad. Anything you'd write on a whiteboard. Wipe at session close (`rm -f memory/working/*` except `.gitkeep`); leaving it accrues noise that drowns out the durable memory layers.

### `.xo/` — backend-owned (hard constraint)

```
.xo/
├── project.json     identity: pid, name, owner_user_id, created_at
├── todos.json       aggregated todos across active sessions
├── stats.json       rolling 7d/30d: tokens, models, files, sessions, time
├── timeline.jsonl   append-only event log (sessions, todos, edits, syncs)
├── peers.json       who this folder is shared with
├── sync.json        last-sync state per peer
└── activity.json    live: which sessions are open right now
```

The agent does not write to any file under `.xo/` — it's backend-owned. Reading is fine for orientation. All mutations (finalize identity from template, append timeline events, update todos / activity / stats, manage peers / sync) will happen through xo-cowork-api endpoints that don't exist yet. Until they do, the agent cannot persist `.xo/`-shaped state changes — note that honestly to the user when relevant, and continue the work that doesn't require it.

---

## Part 4 — Lifecycle in one page

**At session start (boot):** read `AGENTS.md` for the full ritual. Minimum: `AGENTS.md` → `PROJECT.md` → `OBJECTIVES.md` → `PLAN.md` → `memory/semantic/*.md` → last ~30 lines of `PROGRESS.md` → `TASKS.json`. Don't pull `memory/episodic/`, `memory/procedural/`, or `.xo/timeline.jsonl` into the main thread — they grow without bound. Dispatch a subagent with a narrow query if you need them.

**During work:** keep `PLAN.md` and `TASKS.json` reflecting the current state. Use `memory/working/` as scratchpad. `PROGRESS.md` is normally untouched until close; `.xo/` cannot be touched until the endpoints exist.

**At session close:** if the session was noteworthy, write a `memory/episodic/YYYY-MM-DD-{slug}.md`. Append one paragraph to `PROGRESS.md`. Distill any new semantic facts. Promote a procedural skill if a workflow has now succeeded twice. Update `PLAN.md` if scope shifted. Mark closed `TASKS.json` rows `done`. Wipe `memory/working/`. The full closing checklist (eleven steps, including the `.xo/` updates that aren't possible yet) is in `AGENTS.md §6` — follow the ones that don't require touching `.xo/`.

---

## Filesystem fallbacks (no HTTP endpoint yet)

- **List existing projects** — enumerate directories under the root from `/api/config/workspace`.
- **Read project metadata** — read `<project>/.xo/project.json`.
- **Rename or delete a project** — no API; do it via filesystem.
