# Todo HTTP API

This file is the endpoint reference for the **non-Claude-Code path** of recording todos. If you're a Claude Code agent, stop — use your native `TaskCreate` / `TaskUpdate` / `TaskList` instead; the watcher mirrors those into `.xo/todos.json` for you, and calling these endpoints would double-write.

The write-path decision and the todo lifecycle (list at boot, one `in_progress` at a time, `cancelled` vs `blocked`, finished work → `PROGRESS.md`) live in SKILL.md Part 3. This file covers only the HTTP schemas.

## Contents

- Endpoints
- Create
- Update (the common case: status transitions)
- List
- Delete

---

## Endpoints

Same base URL as the rest of cowork-api (`http://${HOST:-localhost}:${PORT:-5002}`). Writes go through a flock shared with the watcher, so concurrent updates don't tear.

```
GET    /api/xo-projects/{project_id}/todos
POST   /api/xo-projects/{project_id}/todos
GET    /api/xo-projects/{project_id}/todos/{todo_id}
PATCH  /api/xo-projects/{project_id}/todos/{todo_id}
DELETE /api/xo-projects/{project_id}/todos/{todo_id}
```

`{project_id}` is the folder name under `<projects_root>` (from `GET /api/config/workspace`).

## Create

```json
POST /api/xo-projects/{project_id}/todos
{
  "runtime": "<your-runtime-identifier>",   // required; see "Choosing `runtime`" below
  "content": "Implement /metrics endpoint",
  "description": "...",                     // optional, ≤4000 chars
  "active_form": "Implementing the /metrics endpoint",  // optional, shown while in_progress
  "session_id": "<your-session-id>",        // optional; defaults to "_project"
  "status": "pending"                       // optional; defaults to "pending"
}
→ 201 { "id": "a1b2c3d4", "content": "...", "status": "pending", "description": "...", "active_form": "..." }
→ 400 invalid_runtime | invalid_session_id | invalid_value | invalid_status
```

**Choosing `runtime`.** It's a stable string that identifies the agent or runtime writing the todo, so the watcher and UI can tell whose todo is whose. The agent picks the value. Both `runtime` and `session_id` must match the regex `[A-Za-z0-9_:\-\.]{1,200}` — alphanumeric plus `_ : - .` as separators (so compound identifiers like `<runtime>:<profile>:<instance>` are fine). The codebase already uses these values, which are safe defaults if your agent has no preference:

| Runtime | Conventional value |
|---|---|
| OpenClaw | `openclaw` |
| Hermes | `hermes` |
| Codex | `codex` |
| Cursor | `cursor` |
| Aider | `aider` |

If your agent is something else, pick a short stable identifier (your agent's name, or `<agent-name>:<instance>` if you run multiple instances). Whatever you pick, use the **same** value for every todo your agent writes in the project — switching mid-session makes the UI show two separate sources for one logical agent.

`session_id` is your runtime's session/conversation id when you have one; omit the field (or send `null`) to use the `"_project"` pseudo-session bucket. `content` is required and ≤1000 chars. `id` is 8 hex chars, server-generated.

## Update (the common case: status transitions)

```json
PATCH /api/xo-projects/{project_id}/todos/{todo_id}
{ "status": "in_progress" }      // any field may be sent; only those provided are touched
→ 200 { "id": "a1b2c3d4", "content": "...", "status": "in_progress", ... }
→ 404 todo_not_found
```

Valid statuses: `pending | in_progress | completed | cancelled | blocked`. Only one `in_progress` per agent at a time — the UI assumes that discipline.

## List

```json
GET /api/xo-projects/{project_id}/todos
→ {
    "project_id": "<id>",
    "updated_at": "2026-05-14T...Z",
    "sessions": {
      "<your-session-id>": {
        "runtime": "<your-runtime-identifier>",
        "source_file": null,
        "session_started_at": "2026-05-14T...Z",
        "todos": [ { "id": "a1b2c3d4", "content": "...", "status": "in_progress" }, ... ]
      },
      "_project": { ... }
    }
  }
```

List at boot to inherit open work from previous sessions; list again whenever you need a cross-session view.

## Delete

```json
DELETE /api/xo-projects/{project_id}/todos/{todo_id}
→ 200 { "project_id": "<id>", "todo_id": "<id>", "deleted": true }
```

Idempotent — returns `deleted: false` (not 404) when the todo wasn't present. Prefer `status: "cancelled"` over delete when the todo was real but you decided not to do it.