# Procedural memory

How to do recurring things. The agent's reusable how-to knowledge. Read only via the `skill-finder` subagent.

## Format

One file per skill, named `{slug}.md`:

```markdown
---
name: slug-matching-filename
trigger_when: human-readable trigger condition (when should the agent reach for this?)
---

## Steps
1. ...
2. ...

## Gotchas
- ...

## Last validated
YYYY-MM-DD
```

## When to write

A skill is written **only** when a workflow has succeeded at least twice. One success is not a pattern — it's an episode. The session-closer enforces this rule.

Writing a skill from a single success creates fabricated procedural knowledge — the most dangerous kind of memory pollution. The skill-finder will trust whatever is here.

## How a skill graduates from learned to user-built

Skills synthesized by the agent live in `.xo/skills/learned/`. If the user reviews and endorses one, they may move it to `.xo/skills/user-built/` — those are treated as authoritative.

## Editing

Skills *can* be edited, unlike episodes. When you re-validate a skill on a fresh success, update the `Last validated` field. If a step changes, edit it and note the change in the most recent session's `session.md`.
