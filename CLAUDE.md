# XO Cowork API - Project Memory

## Project overview

- FastAPI backend that brokers chat and auth flows.
- Uses local `claude` CLI for coding/assistant responses.
- Primary API behavior should remain backward compatible.

## Architecture conventions

- Keep endpoint handlers in `routers/` via `APIRouter`.
- Keep business logic in focused clients/services (thin route handlers).
- Preserve request/response contracts unless explicitly asked to change.

## Coding standards

- Prefer clear, maintainable code over clever abstractions.
- Add robust error handling with actionable error messages.
- Use async patterns for network and subprocess operations.
- Avoid logging sensitive values (tokens, secrets, credentials).

## Validation and safety

- Validate behavior after edits (lint, compile/tests where feasible).
- Keep changes minimal and targeted.
- Maintain session behavior and existing auth flow semantics.

## Agent behavior preferences

- Start with concise implementation-oriented output.
- Call out assumptions and risks explicitly.
- Prefer production-safe defaults.
