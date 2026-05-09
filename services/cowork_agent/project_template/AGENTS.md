# AGENTS.md — operating contract for this folder

> You are an agent (Claude, Codex, Cursor, Aider, or other) working inside a well harness engineered folder. This file is the contract every agent reads first. It is short on purpose.
>
> Ignore it and you will duplicate work, lose context, and corrupt the human's externalized memory. Don't.

---

## 1. What this folder is

A working folder shared between a human and any number of AI agents. It contains the actual project work, plus two persistence layers:

- **`memory/`** — shared cognition. Committed to git. Distilled facts, past episodes, reusable procedures. Visible to every teammate and every agent.
- **`.xo/`** — ephemeral per-machine state. Gitignored. Identity, sessions, sync, peers, stats, todos, timeline.

### Who writes what

|                                                          | Agent writes? | Notes |
|----------------------------------------------------------|:-:|---|
| `PROJECT.md`, `OBJECTIVES.md`, `PLAN.md`, `PROGRESS.md`, `TASKS.json` | yes | Co-edited with the human. |
| `memory/{semantic,episodic,procedural,working}/` | yes | The agent's externalized cognition. |
| `.xo/project.json` (first boot only)             | yes | One-time template fill. After that, read-only. |
| `.xo/{todos,stats,activity,timeline,sync,peers}.json[l]` | **no** | A background **watcher service** maintains these by tailing runtime logs (Claude Code's `~/.claude/projects/…`, OpenClaw's `~/.openclaw/agents/…`, etc.). The agent only reads. |
| `.xo/sessions/sessionslist.json`                  | **no** | Watcher-maintained index. 
If the agent needs something not listed as agent-writable, it almost certainly needs a different tool (a tool call that updates state) — not a direct edit.

Every agent that works here is expected to leave the folder in a **better state than it found it**: more accurate memory, cleaner plan, honest progress log.

---

## 2. File map (this folder *is* the spec — don't invent new top-level files)

```
<project>/
├── AGENTS.md           ← this file. The contract. Read first.
├── CLAUDE.md           ← imports AGENTS.md for Claude Code (`@AGENTS.md`).
├── PROJECT.md          ← what this is for. Stable.
├── OBJECTIVES.md       ← OKRs / north-star outcomes. Stable, weeks.
├── PLAN.md             ← current plan. Agent-maintained, days.
├── PROGRESS.md         ← running narrative of work done. Append-only.
├── TASKS.json          ← machine-readable task list. Project-meaningful IDs.
│
├── memory/                  ← shared cognition. Committed.
│   ├── semantic/            distilled facts (preferences, project-facts, constraints)
│   ├── episodic/            what happened, with context (one file per episode)
│   ├── procedural/          how to do recurring things (validated twice)
│   └── working/             session-scoped scratch (wiped at close)
│
├── .xo/                     ← ephemeral state. Gitignored.
│   ├── project.json         identity: pid, name, owner_user_id, created_at
│   ├── todos.json           aggregated todos across active sessions
│   ├── stats.json           rolling 7d/30d: tokens, models, files, sessions, time
│   ├── timeline.jsonl       append-only event log (sessions, todos, edits, syncs)
│   ├── peers.json           who this folder is shared with
│   ├── sync.json            last-sync state per peer
│   ├── activity.json        live: which sessions are open right now
│   ├── sessions/
│       └── sessionslist.json    index of past sessions — read this for history
└── ... (the actual project work files)
```

---

## 3. First-boot behaviour (template detection)

This folder ships as a **template**. On the very first session, before any real work:

1. Read `.xo/project.json`. If `_template: true`, this folder has never been booted. Do this **once**, then never again:
   - Generate a UUID v4 for `pid`.
   - Set `name` from the folder name (or ask the user).
   - Set `owner_user_id` from the harness env / user identity.
   - Set `created_at` to the current ISO 8601 timestamp.
   - Remove the `_template` field.

   This is the **only** time an agent writes to `.xo/`.
2. Look for `[TEMPLATE]` markers in `PROJECT.md`, `OBJECTIVES.md`, `PLAN.md`, `PROGRESS.md`. If any are present, the folder is fresh. Acknowledge that to the user and ask them to clarify scope and objectives **before** doing real work.

The watcher will detect the new `project.json` and write a `project.created` event to `.xo/timeline.jsonl` on its own. After first boot, jump to §4.

---

## 4. Boot ritual — every session

Read these in order, **before answering**:

1. `AGENTS.md` (this file)
2. `PROJECT.md` — what we're building
3. `OBJECTIVES.md` — why
4. `PLAN.md` — current plan
5. `memory/semantic/*.md` — distilled facts (3 short files)
6. `PROGRESS.md` — **last ~30 lines only**
7. `TASKS.json` — open tasks
8. `.xo/sessions/sessionslist.json` — **last 3 entries only**, to know what was worked on most recently
9. `.xo/activity.json` — is anyone else working here right now?

You don't need to "announce yourself." The watcher sees your runtime open a new native session log and writes the corresponding `session.started` event, the `sessionslist.json` entry, and the `activity.json` heartbeat on your behalf.

**Do not read** `memory/episodic/`, `memory/procedural/`, the full `.xo/sessions/sessionslist.json`, or the full `.xo/timeline.jsonl` from the main thread. They grow without bound. To inspect past session history, follow the rule in §10.

---

## 5. During work

Keep these living:

- **`PLAN.md`** — when the plan changes, edit it. A stale plan misleads the next agent.
- **`TASKS.json`** — mark tasks `in_progress` / `done` as you go. New work? Add a task with the next `T-NNN` id, bump `next_id`.
- **`memory/working/`** — scratchpad. Whatever you'd write on a whiteboard. Wiped at close.

**Do not** edit `PROGRESS.md` mid-work — it is written once at session close.

Use your runtime's **native** todo tool (e.g. Claude Code's TaskCreate/TaskUpdate) for in-session todos — the watcher will mirror those into `.xo/todos.json` automatically. Don't try to keep `.xo/todos.json` in sync yourself.

---

## 6. Closing ritual — when the user signals done

When the human says "done", "wrap up", "good for today", or you detect a natural close, do these in order:

1. **`memory/episodic/{YYYY-MM-DD}-{slug}.md`** — write **only if** the session contained a non-trivial decision, a hard problem solved, an unexpected failure, or strong user feedback. Routine work does not deserve an episode. Format: see §8.
2. **`PROGRESS.md`** — append one paragraph (newest at the bottom). See format in §7.
3. **`memory/semantic/*.md`** — distill any new facts that meet both criteria: (a) observed twice or explicitly stated by the user, (b) true regardless of context. One claim per line. No narrative.
4. **`memory/procedural/{slug}.md`** — write **only if** a workflow has now succeeded ≥2 times. One success is not a pattern. Format: see §8.
5. **`PLAN.md`** — if scope shifted, update. Move the superseded plan to "Recently superseded" as a one-liner.
6. **`TASKS.json`** — mark closed tasks `done` with `completed_at`.
7. **`memory/working/`** — wipe (`rm -f memory/working/*` except `.gitkeep`).

Do all seven. Skipping for "the session was short" is how folders rot.

Everything in `.xo/` (`sessionslist.json`, `timeline.jsonl`, `activity.json`, `stats.json`, `todos.json`, `sync.json`) is updated by the watcher service from your runtime's native logs. **Do not write to those files** — your edits will conflict with the watcher and will be overwritten.

---

## 7. The three logs (don't mix them up)

| Log                              | Format                          | Purpose                                            | Read by                              |
|----------------------------------|---------------------------------|----------------------------------------------------|--------------------------------------|
| `PROGRESS.md`                    | append-only paragraphs          | human-readable progress, scrolled by humans        | every agent at boot (last ~30 lines) |
| `.xo/sessions/sessionslist.json` | append-only array               | one entry per session — the **index** of history   | every agent at boot (last 3 entries) |
| `.xo/timeline.jsonl`             | one JSON event per line         | machine-readable firehose (audit, sync, dashboards)| watcher writes; agents read only via §10        |
| `memory/episodic/*.md`           | one file per noteworthy episode | distilled context for future recall                | memory subagent (never main thread)  |

**`PROGRESS.md` paragraph format:**
```
## YYYY-MM-DD — [outcome] one-line headline
agent: <model id>

3–6 sentences: what was attempted, what shipped, what's blocked, what's next.
```
`[outcome]` ∈ `shipped | progress | blocked | pivoted | cleanup | research`.

**`.xo/timeline.jsonl` event shape:**
```json
{"ts": "2026-05-09T14:33:00Z", "type": "session.start", "session": "2026-05-09", "agent": "claude-opus-4-7", "user": "tools@kosh.network"}
```
Common types: `project.created`, `session.start`, `session.close`, `task.created`, `task.completed`, `plan.updated`, `file.edited`, `episode.written`, `peer.sync`.

---

## 8. Memory discipline

`memory/` has four flavours. The discipline of *which* is the difference between a useful folder and a cluttered one.

**Semantic — `memory/semantic/`** — distilled facts. One claim per line. No narrative. No timestamps. Only update when a fact is observed twice or stated by the user. Three files only: `preferences.md`, `project-facts.md`, `constraints.md`. Do not add new files in this directory.

**Episodic — `memory/episodic/`** — append-only. One file per episode, named `YYYY-MM-DD-{slug}.md`:
```markdown
---
date: YYYY-MM-DD
tags: [tag1, tag2]
outcome: success | failure | partial | abandoned
---

## What
One sentence.

## Why it mattered
One or two sentences.

## How it went
Raw narrative. Do not summarise at write time — summarisation destroys episodic signal.
```
Never edit an episode after writing. If context changed, write a new episode that references the old one by filename.

**Procedural — `memory/procedural/`** — only after a workflow has succeeded ≥2 times:
```markdown
---
name: skill-name
trigger_when: human-readable trigger condition
---

## Steps
1. ...
2. ...

## Gotchas
- ...

## Last validated
YYYY-MM-DD
```
Procedural memory is the highest-leverage kind — it converts experience into reusable capability. It is also the most dangerous to fabricate. Never write a procedural skill from a single success.

**Working — `memory/working/`** — live scratchpad for the current session. Wiped at close. Use it for mid-session reasoning you want to preserve across tool calls.

---

## 9. Hard rules

- **Never write to `.xo/`** with one exception: filling in `.xo/project.json` once on first boot (§3). Every other `.xo/` file is maintained by the watcher service; your edits will conflict with it and be overwritten.
- **Never delete** anything in `memory/` outside the rules in §8 (and even then, only `working/` gets wiped). Memory loss is irreversible.
- **Never edit** an episodic memory file after it is written. Append-only.
- **Never** write narrative text to `memory/semantic/*`. That folder is for distilled claims only.
- **Never** dump tool output, full file contents, or raw logs into any memory file. Memory is *distilled*; raw logs live in `.xo/timeline.jsonl`.
- **Never claim work as done** without verifying it (run the test, open the page, read the diff).
- **Never put secrets** in `memory/` (it is committed) or `.xo/` (it may be synced to peers).
- **Never invent** peer/sync state. If `.xo/peers.json` is empty, you are working solo.
- **Stop and ask** if `PLAN.md` and the user's request disagree. Don't silently re-plan.

---

## 10. Looking up past sessions (read-only)

`.xo/sessions/sessionslist.json` and `.xo/timeline.jsonl` are **read-only for agents** — the watcher service maintains them. You consult them; you never edit them.

When the user references prior work ("continue the auth thing", "the bug from yesterday", "what we discussed"), or whenever you need history older than the last 3 sessions:

1. **Start at the index, not the log.** Open `.xo/sessions/sessionslist.json` and find the relevant `id` by `started_at`, `summary`, or `outcome`. This is a small file — scanning it is cheap.
2. **Pull only that session's events.** Filter `.xo/timeline.jsonl` by `session_id` (e.g. `grep '"session_id":"ses_abc123"' .xo/timeline.jsonl`). Don't read the full log.
3. **For narrative detail**, look at the `episode_refs` on that session's entry — those point into `memory/episodic/`. Have a subagent read them; never main-thread.
4. **For raw artefact recovery**, the entry's `source_file` points at the runtime's native session log (e.g. `~/.claude/projects/.../ses_abc123.jsonl`).

If the question is open-ended ("what have we been working on lately?"), read the last 5–10 entries of `sessionslist.json` and summarise — do not load the whole timeline.

> **Why two files?** `sessionslist.json` is the human/agent-readable index; `timeline.jsonl` is the firehose. They are joined on `session_id`. Most lookups need only the index.

---

## 11. If you're a new agent and lost

Run §3 (if `.xo/project.json` has `_template: true`) or §4 (otherwise). By the time you finish you'll know:

- What this project is (`PROJECT.md`)
- What success looks like (`OBJECTIVES.md`)
- The current plan (`PLAN.md`, `TASKS.json`)
- What has already been done (`PROGRESS.md` last 30 lines)
- What facts are settled (`memory/semantic/`)

That is enough to be useful. Ask the human if anything contradicts.
