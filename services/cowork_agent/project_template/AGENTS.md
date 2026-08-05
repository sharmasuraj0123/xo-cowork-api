# AGENTS.md — operating contract for this folder

> You are an agent (Claude, Codex, Cursor, Aider, or other) working inside a well harness engineered folder. This file is the contract every agent reads first. It is short on purpose.
>
> Ignore it and you will duplicate work, lose context, and corrupt the human's externalized memory. Don't.

---

## 1. What this folder is

A working folder shared between a human and any number of AI agents. It contains the actual project work, plus two persistence layers:

- **`memory/`** — shared cognition. Committed to git. Distilled facts, past episodes, reusable procedures. Visible to every teammate and every agent.
- **`.xo/`** — the coordination contract. Committed alongside `memory/`, so it travels with the folder to a collaborator. Exactly four files: identity, todos, peers, agent record. **Not gitignored.**

Machine-local runtime — sessions, activity, stats, timeline — is **not in this folder at all**. It lives outside every project tree at `~/.xo/<pid>/`, keyed by `.xo/project.json:pid`. That is what makes `.xo/` safe to commit: the machine-specific noise physically cannot get into the project. You read that tier over the API (§7, §10), not by path.

### Who writes what

|                                                          | Agent writes? | Notes |
|----------------------------------------------------------|:-:|---|
| `PROJECT.md`, `OBJECTIVES.md`, `PLAN.md`, `PROGRESS.md` | yes | Co-edited with the human. |
| `memory/{semantic,episodic,procedural,working}/`        | yes | The agent's externalized cognition. |
| `.xo/**` — **everything** under `.xo/`                  | **no** | A background **watcher service** owns this directory: identity, todos, peers, agent record. It tails runtime logs (Claude Code's `~/.claude/projects/…`, OpenClaw's `~/.openclaw/agents/…`, etc.) and your in-flight todos. Agents only **read** `.xo/`. Never write — your edits will be overwritten and may corrupt sync state. |
| `~/.xo/<pid>/**` — machine-local runtime                | **no** | Same watcher, outside the project tree: sessions index, activity, stats, timeline. Don't read it by path either — it is pid-keyed and machine-local. Go through the endpoints in §7 and §10. |

If the agent needs something not listed as agent-writable, it almost certainly needs a different tool (a tool call that mutates state) — not a direct edit.

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
│
├── memory/                  ← shared cognition. Committed.
│   ├── semantic/            distilled facts (preferences, project-facts, constraints)
│   ├── episodic/            what happened, with context (one file per episode)
│   ├── procedural/          how to do recurring things (validated twice)
│   └── working/             session-scoped scratch (wiped at close)
│
├── .xo/                     ← the SYNCED contract. Committed with the folder. NOT gitignored.
│   ├── project.json         identity: pid, name, owner_user_id, created_at
│   ├── todos.json           aggregated todos across active sessions
│   ├── peers.json           who this folder is shared with
│   └── agent.json           backend's record of this folder: id, name, description, backend
│
└── ... (the actual project work files)
```

Those four files are the whole of `.xo/` (`agent.json` appears once a backend registers the folder). If you find `activity.json`, `stats.json`, `timeline.jsonl` or `sessions/` in there, they are leftovers from before the split — the watcher relocates them on its next startup. Don't read them; they are already stale.

Machine-local runtime is **outside** this folder, keyed by `.xo/project.json:pid`:

```
~/.xo/<pid>/                 ← machine-local. Never committed, never synced.
├── activity.json            live: which sessions are open right now
├── stats.json               rolling 7d/30d: tokens, models, files, sessions, time
├── timeline.jsonl           append-only event log (+ rotations)
└── sessions/
    └── sessionslist.json    index of past sessions
```

Shown so you know where the data went — **not so you can open it**. The `<pid>` is machine-local and this store does not travel with the folder, so a path you build today is wrong on the next machine. Read this tier through the endpoints in §7 and §10.

---

## 3. First-boot behaviour (template detection)

This folder ships as a **template**. On the very first session, before any real work, look for `[TEMPLATE]` markers in `PROJECT.md`, `OBJECTIVES.md`, `PLAN.md`, and `PROGRESS.md`. If any are present, the folder is fresh — the human has not yet defined scope or objectives. **Ask them** to clarify before doing real work, then replace the markers with their answers.

`.xo/project.json` (identity: pid, name, owner, created_at) is initialised **by the watcher service**, not by you. The watcher detects the `_template: true` flag, generates a UUID, fills in identity from the harness, and removes the flag. By the time you boot, `.xo/project.json` is either still a template (watcher hasn't run yet — wait or read identity from the harness env) or fully populated. Either way, **don't edit it**.

After scope is clarified and template markers are gone, jump to §4.

---

## 4. Boot ritual — every session

Read these in order, **before answering**:

1. `AGENTS.md` (this file)
2. `PROJECT.md` — what we're building
3. `OBJECTIVES.md` — why
4. `PLAN.md` — current plan
5. `memory/semantic/*.md` — distilled facts (3 short files)
6. `PROGRESS.md` — **last ~30 lines only**
7. `.xo/todos.json` — open todos across active sessions
8. `GET /api/xo-projects/<project>/usage/sessions` — **the 3 most recent entries only** (the response is newest-first by `lastActivity`), to know what was worked on most recently
9. `GET /api/xo-projects/<project>/activity` — is anyone else working here right now?

Steps 8 and 9 read the machine-local runtime tier, which is not in this folder — so you read it over HTTP, not from disk. Base URL `http://${HOST:-localhost}:${PORT:-5002}`; `<project>` is this folder's name (the `name` field in `.xo/project.json`). An empty `sessions` or `open_sessions` array means genuinely no history / nobody else here — the watcher hasn't written anything yet. It does **not** mean the call failed, and it is never a reason to go looking for the files by path.

You don't need to "announce yourself." The watcher sees your runtime open a new native session log and writes the corresponding `session.started` event, the sessions-index entry, and the activity heartbeat on your behalf.

**Do not read** `memory/episodic/`, `memory/procedural/`, the full sessions index, or the full timeline from the main thread. They grow without bound. To inspect past session history, follow the rule in §10.

---

## 5. During work

Keep these living:

- **`PLAN.md`** — when the plan changes, edit it. A stale plan misleads the next agent.
- **`memory/working/`** — scratchpad. Whatever you'd write on a whiteboard. Wiped at close.

**Do not** edit `PROGRESS.md` mid-work — it is written once at session close.

Use your runtime's **native** todo tool (e.g. Claude Code's TaskCreate/TaskUpdate) for in-session todos — the watcher mirrors those into `.xo/todos.json` automatically. There is no project-level `TASKS.json`; project todos and session todos are the same list, surfaced through the watcher. If you need to see all in-flight todos across sessions, read `.xo/todos.json`.

---

## 6. Closing ritual — when the user signals done

When the human says "done", "wrap up", "good for today", or you detect a natural close, do these in order:

1. **`memory/episodic/{YYYY-MM-DD}-{slug}.md`** — write **only if** the session contained a non-trivial decision, a hard problem solved, an unexpected failure, or strong user feedback. Routine work does not deserve an episode. Format: see §8.
2. **`PROGRESS.md`** — append one paragraph (newest at the bottom). See format in §7.
3. **`memory/semantic/*.md`** — distill any new facts that meet both criteria: (a) observed twice or explicitly stated by the user, (b) true regardless of context. One claim per line. No narrative.
4. **`memory/procedural/{slug}.md`** — write **only if** a workflow has now succeeded ≥2 times. One success is not a pattern. Format: see §8.
5. **`PLAN.md`** — if scope shifted, update. Move the superseded plan to "Recently superseded" as a one-liner.
6. **`memory/working/`** — wipe (`rm -f memory/working/*` except `.gitkeep`).

Do all six. Skipping for "the session was short" is how folders rot.

Both tiers — `.xo/` (`project.json`, `todos.json`, `peers.json`, `agent.json`) and the machine-local runtime under `~/.xo/<pid>/` (`activity.json`, `stats.json`, `timeline.jsonl`, `sessions/`) — are updated by the watcher service from your runtime's native logs. **Do not write to those files** — your edits will conflict with the watcher and will be overwritten.

---

## 7. The three logs (don't mix them up)

| Log                              | Format                          | Purpose                                            | Read by                              |
|----------------------------------|---------------------------------|----------------------------------------------------|--------------------------------------|
| `PROGRESS.md`                    | append-only paragraphs          | human-readable progress, scrolled by humans        | every agent at boot (last ~30 lines) |
| `GET …/usage/sessions`           | newest-first array              | one entry per session — the **index** of history   | every agent at boot (3 most recent)  |
| `GET …/timeline`                 | newest-first JSON events        | machine-readable firehose (audit, sync, dashboards)| watcher writes; agents read only via §10        |
| `memory/episodic/*.md`           | one file per noteworthy episode | distilled context for future recall                | memory subagent (never main thread)  |

The middle two are the runtime tier: on disk they are `~/.xo/<pid>/sessions/sessionslist.json` and `~/.xo/<pid>/timeline.jsonl`, outside this folder. The endpoints (`/api/xo-projects/<project>/…`, §4) are how you reach them.

**`PROGRESS.md` paragraph format:**
```
## YYYY-MM-DD — [outcome] one-line headline
agent: <model id>

3–6 sentences: what was attempted, what shipped, what's blocked, what's next.
```
`[outcome]` ∈ `shipped | progress | blocked | pivoted | cleanup | research`.

**Timeline event shape** (one element of the endpoint's `events[]`):
```json
{"ts": "2026-05-09T14:33:00Z", "type": "session.started", "session_id": "ses_abc123", "runtime": "claude_code", "user_id": "tools@kosh.network"}
```
Types: `project.created`, `session.started`, `session.closed`, `todo.added`, `todo.completed`, `plan.written`, `file.created`, `file.edited`, `episode.written`, `peer.sync.started`, `peer.sync.applied`, `peer.sync.conflict`. `session_id` is set on all but the project-wide ones.

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

- **Never write to `.xo/`** — or to `~/.xo/<pid>/`. No exceptions, not even `.xo/project.json` on first boot. The watcher service owns both tiers; your edits will conflict with it, be overwritten, or corrupt sync state.
- **Never build a path into `~/.xo/`.** The runtime tier is machine-local and pid-keyed — read it through the endpoints in §7 and §10. A hand-built path is the one mistake that silently returns nothing and reads as "no history."
- **Never delete** anything in `memory/` outside the rules in §8 (and even then, only `working/` gets wiped). Memory loss is irreversible.
- **Never edit** an episodic memory file after it is written. Append-only.
- **Never** write narrative text to `memory/semantic/*`. That folder is for distilled claims only.
- **Never** dump tool output, full file contents, or raw logs into any memory file. Memory is *distilled*; raw logs live in the runtime timeline (§7).
- **Never claim work as done** without verifying it (run the test, open the page, read the diff).
- **Never put secrets** in `memory/` or `.xo/` — both are committed and both travel to peers.
- **Never invent** peer/sync state. If `.xo/peers.json` is empty, you are working solo.
- **Stop and ask** if `PLAN.md` and the user's request disagree. Don't silently re-plan.

---

## 10. Looking up past sessions (read-only)

Session history is the machine-local runtime tier: it is not in this folder, and it is **read-only for agents** — the watcher service maintains it. You consult it over HTTP; you never edit it and never open it by path. Base URL and `<project>` as in §4.

When the user references prior work ("continue the auth thing", "the bug from yesterday", "what we discussed"), or whenever you need history older than the 3 most recent sessions:

1. **Start at the index, not the firehose.** `GET /api/xo-projects/<project>/usage/sessions` returns every session newest-first; find the relevant one by `lastActivity`, `messageCount`, or `sessionId`. This is a small response — scanning it is cheap.
2. **Then that one session's detail.** `GET /api/xo-projects/<project>/usage/sessions/<sessionId>` — tokens, duration, activity dates, message and tool counts. It accepts either the composite `sessionId` from step 1 or the bare native session id.
3. **For its events**, `GET /api/xo-projects/<project>/timeline?limit=100` and keep the events whose `session_id` matches. The endpoint pages by `before` (an ISO timestamp) and filters by `types` — use those to bound the fetch rather than pulling the whole log.
4. **For narrative detail**, read `memory/episodic/` — episodes are named `YYYY-MM-DD-{slug}.md`, so the session's `firstActivity` (epoch milliseconds) is what you match the date on. Have a subagent read them; never main-thread.
5. **For raw artefact recovery**, the entry's `sessionFile` is the native log's filename (`<nativeSessionId>.jsonl`); the directory is your runtime's own (e.g. `~/.claude/projects/…`). The API deliberately never returns absolute paths.

If the question is open-ended ("what have we been working on lately?"), read the 5–10 most recent entries from step 1 and summarise — do not load the timeline at all.

> **Why two endpoints?** `usage/sessions` is the human/agent-readable index; `timeline` is the firehose. They are joined on the session id. Most lookups need only the index.

---

## 11. If you're a new agent and lost

Run §3 (if there are `[TEMPLATE]` markers anywhere) or §4 (otherwise). By the time you finish you'll know:

- What this project is (`PROJECT.md`)
- What success looks like (`OBJECTIVES.md`)
- The current plan (`PLAN.md`)
- What has already been done (`PROGRESS.md` last 30 lines)
- What facts are settled (`memory/semantic/`)
- What's in flight (`.xo/todos.json`)

That is enough to be useful. Ask the human if anything contradicts.
