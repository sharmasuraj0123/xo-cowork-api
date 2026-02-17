# Agent Instructions

This directory stores per-agent instruction files for `claude_code_client.py`.

## How it works

- File name (without extension) is the agent name.
- Supported formats: `.md`, `.txt`
- Examples:
  - `instructions/default.md`
  - `instructions/code_writer.md`
  - `instructions/reviewer.txt`

When a request includes `agent_type`, the matching file is used as instruction context.
If no match exists, the client falls back to `default` agent file (if present), then raw question.

## Minimal example

Create `instructions/default.md`:

```md
You are an engineering assistant.
- Be concise.
- Prefer actionable steps.
- Ask clarifying questions only when required.
```

## Included profiles

- `default.md` - general production backend behavior
- `code_writer.md` - implementation-heavy coding profile
- `reviewer.md` - strict code review profile
- `debugger.md` - root-cause-first debugging profile
- `agentic-backend.md` - FastAPI + Agno backend specialist
