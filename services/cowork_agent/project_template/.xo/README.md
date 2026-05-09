# .xo — the agent's externalized cognition

This folder is the agent's memory, sessions, artifacts, and skills. It is **personal state** — gitignored by default, rebuilt from sessions if lost.

The full lifecycle is documented in `../AGENTS.md`. This README is just the map.

```
.xo/
├── state/
│   └── SOUL.md              # Agent identity & personality. Stable.
├── memory/
│   ├── semantic/            # Distilled facts. One claim per line. Auto-loaded into prefix.
│   │   ├── preferences.md
│   │   ├── project-facts.md
│   │   └── constraints.md
│   ├── episodic/            # What happened, with context. Append-only. Subagent-mediated.
│   ├── procedural/          # How to do recurring things. Reusable workflows.
│   └── working/             # Live scratchpad. Wiped at session close.
├── sessions/
│   ├── index.md             # Manifest — one line per session
│   ├── {YYYY-MM-DD}/
│   │   └── session.md       # Narrative for that session
│   └── compressed/          # Folded summaries of sessions older than ~20 raw entries
├── artifacts/
│   ├── drafts/              # Work in progress
│   └── final/               # Accepted deliverables (user-promoted only)
├── skills/
│   ├── user-built/          # Skills you write
│   └── learned/             # Skills the agent synthesizes
└── context/
    ├── config.json          # Declarative load manifest (what's prefix, what's on-demand)
    └── cache.md             # Stable cache anchor — keep identical across sessions
```

## Loading rules at a glance

- **Auto-loaded into stable prefix** (cache-warm every session): `state/SOUL.md`, all of `memory/semantic/*`.
- **Read on-demand by main agent only**: `context/config.json`, `sessions/index.md` (last 3 lines).
- **Read only via subagent dispatch**: `memory/episodic/`, `memory/procedural/`, `sessions/{date}/`, `sessions/compressed/`, `artifacts/`, `skills/`.

The "subagent-only" rule is what keeps the main agent's context clean as the folder grows. Violate it and you reintroduce the context rot the folder was built to prevent.
