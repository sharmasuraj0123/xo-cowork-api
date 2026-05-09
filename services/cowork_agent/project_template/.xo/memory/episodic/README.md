# Episodic memory

What happened, with context. Append-only. Read only via the `memory-archivist` subagent.

## Format

One file per episode, named `YYYY-MM-DD-{slug}.md`:

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

## When to write

- Non-trivial decision was made (with reasoning worth preserving)
- Hard problem was solved (the *how* matters for next time)
- Workflow failed unexpectedly (the failure mode is the lesson)
- User gave strong feedback (positive or corrective)

## When *not* to write

- Routine work — that's session-log territory.
- A workflow that succeeded for the first time — write it once, then if it succeeds again, distill into procedural memory.
- A one-off observation — that's semantic territory (or skip entirely if not durable).

## Editing

**Episodes are append-only.** Never edit a written episode. If context changed, write a new episode that references the old one by filename.
