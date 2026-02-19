# XO Cowork API - Codex Project Instructions

## Project overview

- FastAPI backend that brokers chat and auth flows.
- Uses local coding CLIs (`claude` or `codex`) for assistant responses.
- Keep API behavior backward compatible by default.

## Architecture conventions

- Put endpoint modules in `routers/` via `APIRouter`.
- Keep route handlers thin; move logic to clients/services.
- Preserve request/response contracts unless explicitly requested.

## Code and safety standards

- Prefer clear, maintainable code.
- Add robust error handling and actionable messages.
- Avoid logging secrets, tokens, and credentials.
- Use async patterns for network and subprocess operations.

## Validation

- Validate changes with lints/tests/compile where feasible.
- Keep edits minimal and targeted to the requested task.

## Agent behavior

- Provide concise implementation-focused output.
- Explicitly call out assumptions and risks.
- Prefer production-safe defaults.
