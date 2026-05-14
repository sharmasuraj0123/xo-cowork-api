# Backup & restore (GitHub-backed)

xo-cowork-api ships endpoints for encrypted, GitHub-backed backups of xo-projects. All routes are under `/api/xo-projects-sync/` on the same base URL as the rest of cowork-api (`http://${HOST:-localhost}:${PORT:-5002}`). **The user never needs to open GitHub manually** — the backend creates and manages the repo for them.

## Contents

- When to use
- Placeholders used in this section
- Endpoints
- First-run flow
- Token resolution
- Backup
- List remote snapshots
- Restore
- What gets backed up
- Common error responses

---

## When to use

Triggers: user asks to *back up*, *save*, *snapshot*, *sync*, *push*, *upload*, *restore*, *pull*, *download*, *recover*, or *migrate* their projects. Also when the user says they're moving to a new workspace.

## Placeholders used in this section

The examples below use slot names that you substitute at call time:

| Slot | Meaning |
|---|---|
| `{project_id}` | URL path parameter — the directory name under `~/xo-projects/`. Example real value: `research`. |
| `<project_id>` | The same value when it appears inside a JSON body or response. |
| `<snapshot_id>` | A snapshot identifier in `YYYYMMDD-HHMMSS` UTC format. Example: `20260511-153000`. |
| `<owner>` | The GitHub login the connected token belongs to. Discovered by the backend; never set by the caller. |
| `<xo-projects-root>` | The local xo-projects directory, usually `/home/coder/xo-projects`. |

## Endpoints

```
POST   /api/xo-projects-sync/setup
GET    /api/xo-projects-sync/status
GET    /api/xo-projects-sync/projects
POST   /api/xo-projects-sync/projects/{project_id}
POST   /api/xo-projects-sync/all
POST   /api/xo-projects-sync/projects/{project_id}/restore
POST   /api/xo-projects-sync/all/restore
```

## First-run flow

Always start with `GET /api/xo-projects-sync/status`:

```json
GET /api/xo-projects-sync/status
→ {
    "configured": false,
    "repo_name": null,
    "token_source": "connector" | "env" | null,
    "gpg_available": true
  }
```

If `configured: false`:

1. Ask the user for a **passphrase** they'll remember. Tell them: "Write this down. Without it, none of your backups can ever be restored — not even by me."
2. Ask for a **repo name**, suggest the default `xo-projects-backup`.
3. Call setup:

```json
POST /api/xo-projects-sync/setup
{ "repo_name": "<repo_name>", "passphrase": "<from user>" }
→ {
    "configured": true,
    "repo_owner": "<owner>",
    "repo_name": "<repo_name>",
    "repo_url": "https://github.com/<owner>/<repo_name>.git",
    "repo_created": true | false,
    "token_source": "connector" | "env"
  }
```

Setup persists `BACKUP_REPO_NAME` + `BACKUP_PASSWORD` into `xo-cowork-api/.env`, ensures the GitHub repo exists (creates as private if missing — using `gh` CLI first, REST API fallback), and updates the running process's env in place. It's idempotent: re-running with the same values is a no-op.

## Token resolution

If `token_source: null` or any endpoint returns **401**:

1. Tell the user one of:
   - "Complete the GitHub connector flow in xo-cowork UI", or
   - "Add `GITHUB_PAT=<your-token>` to `~/xo-cowork-api/.env`. If you'll also run `gh` directly, put it in your shell env too."
2. Do **not** write the PAT to either file for them — they must do that step manually.
3. After they confirm it's set, retry the original call.

The token needs `repo` scope to create + push to a private repo.

## Backup

```json
POST /api/xo-projects-sync/projects/{project_id}
{ "note": "<optional short note>" }
→ {
    "project_id": "<project_id>",
    "snapshot_id": "<snapshot_id>",
    "size_bytes": 423618,
    "sha256": "…",
    "parts": 1,
    "ok": true,
    "error": null
  }
```

For all projects in one go:

```json
POST /api/xo-projects-sync/all
{ "note": "<optional short note>" }
→ [
    { "project_id": "<project_a>", "snapshot_id": "<snapshot_id>", "size_bytes": 423618, "ok": true, "error": null },
    { "project_id": "<project_b>", "ok": false, "error": "git ls-files failed: ..." }
  ]
```

Bulk backup is independent-per-project: a failure on one project does NOT abort the others. Each entry carries its own `ok` and optional `error`.

## List remote snapshots

```json
GET /api/xo-projects-sync/projects
→ [
    {
      "project_id": "<project_id>",
      "snapshots": [
        { "id": "<snapshot_id_newer>", "created_at": "2026-05-11T15:30:00+00:00", "size_bytes": 423618 },
        { "id": "<snapshot_id_older>", "created_at": "2026-05-10T10:00:00+00:00", "size_bytes": 421104 }
      ]
    }
  ]
```

Snapshots are sorted newest-first. The backend keeps the last 10 per project — older ones are auto-pruned on the next backup.

## Restore

Default behavior **refuses** to overwrite an existing local project — restoring blindly would clobber uncommitted local work:

```json
POST /api/xo-projects-sync/projects/{project_id}/restore
{}                          // body optional; defaults to latest snapshot
→ 409 {
    "detail": {
      "error": "project_exists",
      "detail": "Project folder already exists at <xo-projects-root>/<project_id>.",
      "suggestion": "Pass force=true in the body to overwrite; existing local data will be lost."
    }
  }
```

On 409, **present the message verbatim to the user and ask them to confirm**. Only retry with `force: true` after explicit user confirmation:

```json
POST /api/xo-projects-sync/projects/{project_id}/restore
{ "force": true }
→ {
    "project_id": "<project_id>",
    "restored_from": "<snapshot_id>",
    "target": "<xo-projects-root>/<project_id>",
    "ok": true,
    "error": null,
    "error_code": null
  }
```

Pin a specific snapshot with `snapshot_id`:

```json
{ "snapshot_id": "<snapshot_id>", "force": true }
```

Bulk restore:

```json
POST /api/xo-projects-sync/all/restore
{ "force": true, "snapshot_id_map": { "<project_a>": "<snapshot_id>" } }
→ [
    { "project_id": "<project_a>", "restored_from": "<snapshot_id>", "target": "...", "ok": true },
    { "project_id": "<project_b>", "ok": false, "error_code": "exists", "error": "..." }
  ]
```

`snapshot_id_map` is optional and per-project; missing entries use the latest snapshot for that project. `force` applies to every project. Each project's result is independent — a 409-equivalent on one doesn't abort the rest.

## What gets backed up

- Project is tarred + gzipped → encrypted with `gpg --symmetric --cipher-algo AES256` using `BACKUP_PASSWORD` → split into ≤95 MB parts to stay under GitHub's 100 MB file limit.
- If the project is a git repo, `.gitignore` is respected (via `git ls-files --cached --others --exclude-standard`). Non-git projects skip only the mandatory excludes (their `.gitignore` is NOT consulted in v1).
- Mandatory excludes regardless of `.gitignore`: `.env`, `.env.*`, `.git/`, `node_modules/`, `.venv/`, `__pycache__/`, `*.sock`.

## Common error responses

| Status | When | Surface to user |
|---|---|---|
| 400 `not_configured` | `/setup` hasn't been called yet | Run setup first; ask for passphrase + repo name |
| 401 `github_auth_missing` | No connector token AND no `GITHUB_PAT` | Set up auth (UI flow OR env var); see Token resolution above |
| 404 `project_not_found` | Local project doesn't exist (backup) | The project hasn't been created yet — list projects or scaffold first |
| 404 `snapshot_not_found` | Remote has no snapshot for that project / snapshot_id wrong | Check `GET /projects` for valid ids |
| 409 `project_exists` | Restore target exists locally | Ask user to confirm overwrite, then retry with `force: true` |
| 500 `gpg_missing` | `gpg` not installed on host | Run `sudo apt-get install -y gnupg` (host responsibility, not user-fixable from chat) |
| 502 `verify_failed` | Snapshot sha256 doesn't match manifest | Snapshot is corrupted; try a different snapshot id |
| 502 `repo_create_failed` | Token lacks `repo` scope or GitHub rejected | Regenerate PAT with `repo` scope |