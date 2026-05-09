# AGENTS.md

You are running inside a properly harness engineered folder. This folder is your externalized cognition. Treat it as a first-class part of yourself: read from it on boot, write to it on close, delegate heavy reads to subagents.

The folder layout is fixed. Do not invent new top-level files or new top-level `.xo/` subdirectories. Add content inside the existing structure.

---

## Stable prefix (auto-loaded)

The following files are inlined into your context every session. They form the cache-warm prefix — keep edits to them small and targeted so the cache stays hot.

@OBJECTIVES.md
@WORKSPACE.md
@.xo/state/SOUL.md
@.xo/memory/semantic/preferences.md
@.xo/memory/semantic/project-facts.md
@.xo/memory/semantic/constraints.md

If any of these files contain `[TEMPLATE]` markers, this is a freshly-copied cowork folder. On your first user turn, acknowledge it and ask the user to clarify objectives before doing real work.

---

## Boot ritual

On the first turn of a new session:

1. The stable prefix above is already in context — do not re-read those files.
2. Read **only** the last 3 lines of `.xo/sessions/index.md` to know what was worked on most recently. Do not load the index in full.
3. If the user's first request references prior work ("continue", "the bug from yesterday", "what we discussed"), dispatch the **memory-archivist** subagent to retrieve relevant episodes. Do **not** grep `.xo/memory/episodic/` or `.xo/sessions/` from the main thread — the volume will rot your context.
4. If the user's first request triggers a known recurring workflow (deploy, release, migration, review), dispatch the **skill-finder** subagent before starting.

---

## During-session rules

**Artifact destinations.** All outputs you produce go inside `.xo/artifacts/`:
- Work-in-progress → `.xo/artifacts/drafts/{date}-{slug}/`
- Finished deliverables (only after user accepts) → `.xo/artifacts/final/{date}-{slug}/`

Never write outputs to the project root unless the artifact *is* a project file (e.g. source code in a code project). Drafts and notes always go in `drafts/`.

**Working memory.** Use `.xo/memory/working/scratch.md` for mid-session reasoning that you want to preserve across tool calls but don't need long-term. This file is wiped on session close.

**Subagent dispatch — the one rule that matters most.** When you need to read anything inside `.xo/memory/episodic/`, `.xo/memory/procedural/`, `.xo/sessions/`, `.xo/artifacts/`, or `.xo/skills/`, dispatch the appropriate subagent. These directories accumulate fast. Reading them directly poisons your working context with stale tokens.

| Need                                              | Subagent          |
|---------------------------------------------------|-------------------|
| Recall a past episode, decision, or session       | memory-archivist  |
| Find a procedural skill or recipe                 | skill-finder      |
| Locate a past artifact (draft or final)           | memory-archivist  |
| Close out the current session                     | session-closer    |

You may read `.xo/memory/semantic/*` directly — those files are small, distilled, and already in your stable prefix.

---

## Memory writing discipline

You write three kinds of memory. The discipline of *which* is the difference between a useful folder and a cluttered one.

### Semantic — `.xo/memory/semantic/`
Distilled, de-contextualized facts. One claim per line. No narrative. No timestamps. Examples:
- "User prefers DD/MM/YYYY date format."
- "This project uses pnpm, not npm."
- "Never use em-dashes in user-facing copy."

Update semantic memory only when a fact has been **observed twice** or **explicitly stated by the user**. One-off observations go to episodic, not semantic. When you update semantic memory, edit the existing file in place — do not create new files.

### Episodic — `.xo/memory/episodic/`
What happened, with context. One file per episode, named `YYYY-MM-DD-{slug}.md`. Format:
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
Raw narrative. Do not summarize at write time — summarization destroys episodic signal.
```

Write an episode when: a non-trivial decision was made, a hard problem was solved, a workflow failed unexpectedly, or the user gave strong feedback. **Do not** write an episode for routine work — that's session-log territory.

### Procedural — `.xo/memory/procedural/`
How to do recurring things. Write a procedural skill when the same workflow has succeeded **at least twice**. Format:
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

Procedural memory is the highest-leverage kind — it converts experience into reusable capability. But it is also the most dangerous to fabricate. Never write a procedural skill from a single success.

---

## Session close ritual

When the user signals end-of-session (says "done", "let's wrap", "stop", "good for today", or you detect a natural close), **dispatch the session-closer subagent before your final response.**

The session-closer will:
1. Write `.xo/sessions/{date}/session.md` (narrative) and append a one-liner to `.xo/sessions/index.md`.
2. Distill any new semantic facts into `.xo/memory/semantic/*`.
3. Promote completed artifacts from `drafts/` to `final/` (only with explicit user accept — otherwise leave in drafts).
4. Update `WORKSPACE.md` with current active/blocked/next state.
5. If a workflow repeated successfully, write a procedural skill.
6. Wipe `.xo/memory/working/`.
7. Run cheap compression (see below).

Do not perform these steps in the main thread. Always dispatch the subagent — it has the full ritual encoded.

---

## Compression policy (cheap and predictable)

At session-close, the session-closer runs this rule and nothing more:

> If `.xo/sessions/` contains more than **20** date-stamped session folders, fold the oldest **5** into a single file `.xo/sessions/compressed/{YYYY-MM}.md` containing one paragraph per folded session. Delete the folded folders only after the compressed file is written and verified non-empty.

That's the entire policy. No semantic compression, no token-counting, no thresholds. Predictable trigger, bounded work, easy to reason about.

---

## Forbidden

- **Never delete** anything in `.xo/sessions/` outside the compression policy. Session loss is irreversible.
- **Never edit** an episodic memory file after it is written. Episodes are append-only — if context changed, write a new episode that references the old one.
- **Never** write narrative text to `.xo/memory/semantic/*`. That folder is for distilled claims only.
- **Never** dump tool output, full file contents, or raw logs into any memory file. Memory is *distilled*; logs live in `sessions/`.
- **Never** auto-promote drafts to final. Only the user accepts work into `final/`.
- **Never** commit `.xo/`. It is in `.gitignore` for a reason — it is personal state, not project config.

---

## Self-check before responding

If you are about to:
- Read more than ~50 lines from anywhere in `.xo/` → stop, dispatch a subagent instead.
- Write to `.xo/memory/semantic/*` based on a one-off observation → stop, write to episodic instead.
- Edit `WORKSPACE.md` mid-session → stop, that file is updated only at session close.
- Skip the session-close ritual because "the session was short" → stop, run it anyway. Even a short session may have produced a fact worth distilling.

The folder works only if you follow the lifecycle. A cowork folder neglected for one session loses one session of memory. A cowork folder neglected for a month loses the agent.
