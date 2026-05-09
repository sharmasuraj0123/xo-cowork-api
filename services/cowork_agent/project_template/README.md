# cowork

A folder anatomy that turns your filesystem into an agent's externalized cognition. Drop this folder anywhere, launch Claude inside it, and the agent boots with a memory architecture designed to stay coherent across long-running work.

## How to use

1. Copy this folder to wherever your project lives:
   ```sh
   cp -r cowork/ my-new-project/
   cd my-new-project
   ```
2. Edit `OBJECTIVES.md` — replace the `[TEMPLATE]` content with what winning looks like.
3. Launch Claude Code (or any AGENTS.md-aware agent) inside the folder.
4. Work normally. The agent handles the rest.

## What's in here

| Path                        | Purpose                                                              | Committed? |
|-----------------------------|----------------------------------------------------------------------|:----------:|
| `AGENTS.md`                 | Instructions, lifecycle, rules. The agent's operating system.        | yes        |
| `CLAUDE.md`                 | Pointer to AGENTS.md for Claude Code compatibility.                  | yes        |
| `OBJECTIVES.md`             | North star — what winning looks like. Stable for weeks.              | yes        |
| `WORKSPACE.md`              | Current state of play. Rewritten at end of every session.            | yes        |
| `.claude/agents/`           | Subagent definitions for offloading heavy reads from main thread.    | yes        |
| `.xo/`                      | The agent's memory, sessions, artifacts, skills. Personal state.     | **no**     |

## The split

- **Outside `.xo/`** → committed config. The contract between you, your team, and the agent.
- **Inside `.xo/`** → ephemeral state. The agent's accumulated memory, personal to your machine.

If you want to back up `.xo/` separately, archive it. Don't commit it to the same repo as the project.

## Design

The full design rationale lives in `AGENTS.md`. The short version:

- **Stable prefix** (top-level files + `.xo/memory/semantic/*`) gets cache-warm and inlined every session.
- **Heavy reads** (sessions, episodes, skills) go through subagents so the main context stays clean.
- **Memory has three flavors**: semantic (distilled facts), episodic (what happened, with context), procedural (how to do recurring things). Each has a strict write discipline.
- **Sessions are append-only**, compressed predictably (oldest 5 folded when count > 20). Never deleted.
- **Compression policy is dumb on purpose** — predictable, bounded, easy to reason about.

## Not invented here

Inspired by Hermes (procedural memory), OpenClaw (compression and SOUL files), the AGENTS.md spec (Sourcegraph/OpenAI/Google/Cursor 2025), and Claude's own subagent system.
