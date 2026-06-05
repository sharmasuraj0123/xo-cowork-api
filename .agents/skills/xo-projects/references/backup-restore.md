# Backup & restore (GitHub-backed)

> **Hard constraint — read this before anything else in this file.**
> Backups and restores happen *only* through the `/api/xo-projects-sync/*` endpoints documented below. Never tar, zip, `cp`, `rsync`, or `git push` the project to satisfy a "back up" or "save" request. The API does gpg encryption, secret exclusion, manifest + sha256 generation, chunking under GitHub's 100 MB limit, and the GitHub push as one atomic operation — none of which a local archive replicates. A local archive also won't be picked up by `GET /projects` and can't be restored by `POST /restore`. If the user explicitly wants a tarball or local copy (not a backup), confirm it's a one-off and tell them it isn't a restorable backup.

xo-cowork-api ships endpoints for encrypted, GitHub-backed backups of xo-projects. All routes are under `/api/xo-projects-sync/` on the same base URL as the rest of cowork-api (`http://${HOST:-localhost}:${PORT:-5002}`). **The user never needs to open GitHub manually** — the backend creates and manages each repo for them.

## Repo model

**One private GitHub repo per xo-project**, named `xo-project-<project_id>`. The prefix is fixed (`xo-project-`) and used both as a creation convention and as the discovery filter when listing what's backed up.

Repos are created **lazily** — the first backup of a given project is what creates `xo-project-<project_id>`. `/setup` only persists the passphrase; it never creates repos.

Inside each per-project repo, snapshots sit at the repo root:

```
<owner>/xo-project-<project_id>/        (private)
├── 20260514-153000/
│   ├── manifest.json
│   ├── part-000.gpg
│   └── part-001.gpg
├── 20260514-090000/
│   ├── manifest.json
│   └── part-000.gpg
└── ...
```

There is no shared `xo-projects-backup` repo anymore.

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
    "token_source": "connector" | "env" | null,
    "gpg_available": true
  }
```

If `configured: false`:

1. Ask the user for a **passphrase** they'll remember. Tell them: "Write this down. Without it, none of your backups can ever be restored — not even by me." There is no repo name to ask for; per-project repos are auto-named `xo-project-<project_id>`.
2. Call setup:

```json
POST /api/xo-projects-sync/setup
{ "passphrase": "<from user>" }
→ {
    "configured": true,
    "repo_owner": "<owner>",
    "token_source": "connector" | "env"
  }
```

Setup persists `BACKUP_PASSWORD` into `xo-cowork-api/.env`, verifies that gpg is installed and the GitHub token resolves, and confirms which account is configured (via `repo_owner`). It does **not** create any GitHub repos — those are created lazily on first backup of each project.

> **Guardrail — re-running `/setup` is destructive.**
> If `status.configured` is already `true`, do **not** call `/setup` again unless the user has explicitly asked to rotate the backup passphrase. `/setup` *replaces* the configured passphrase; every existing snapshot was encrypted with the previous one and becomes permanently unrecoverable. This is the same shape as the `force: true` confirmation on restore — and strictly more dangerous, because there's no preview. Before re-running:
> 1. Tell the user, plainly: *"This will make every existing backup unrecoverable. Snapshots can't be re-decrypted with the new passphrase."*
> 2. Ask them to confirm they don't need to restore anything from existing snapshots.
> 3. Only then proceed.
> If they want to rotate the passphrase *and* keep their old backups available, the answer is: restore everything first under the current passphrase, then re-setup, then back up again.

## Token resolution

If `token_source: null` or any endpoint returns **401**:

1. Tell the user one of:
   - "Complete the GitHub connector flow in xo-cowork UI", or
   - "Add `GITHUB_PAT=<your-token>` to `~/xo-cowork-api/.env`. If you'll also run `gh` directly, put it in your shell env too."
2. Do **not** write the PAT to either file for them — they must do that step manually.
3. After they confirm it's set, retry the original call.

The token needs `repo` scope to create + push to private repos.

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

On first call for a given `project_id`, the backend creates `xo-project-<project_id>` as a private repo. Subsequent calls skip straight to clone-and-push.

For all projects in one go:

```json
POST /api/xo-projects-sync/all
{ "note": "<optional short note>" }
→ [
    { "project_id": "<project_a>", "snapshot_id": "<snapshot_id>", "size_bytes": 423618, "ok": true, "error": null },
    { "project_id": "<project_b>", "ok": false, "error": "git ls-files failed: ..." }
  ]
```

Bulk backup is independent-per-project: a failure on one project does NOT abort the others. Each entry carries its own `ok` and optional `error`. Per-project repos are independent, so one project's push failure can't break another's history.

## List remote snapshots

```json
GET /api/xo-projects-sync/projects
→ [
    {
      "project_id": "<project_id>",
      "snapshots": [
        { "id": "<snapshot_id_newer>", "created_at": "2026-05-11T15:30:00+00:00", "size_bytes": 423618 },
        { "id": "<snapshot_id_older>", "created_at": "2026-05-10T10:00:00+00:00", "size_bytes": 421104 }
      ],
      "error": null
    },
    {
      "project_id": "<unreachable_project_id>",
      "snapshots": [],
      "error": "RuntimeError: git clone --depth=1 ... failed: <reason>"
    }
  ]
```

The backend discovers projects by listing the user's GitHub repos and filtering names that start with `xo-project-`. Snapshots are sorted newest-first. The backend keeps the last 10 per project — older ones are auto-pruned on the next backup.

Per-project `error` is `null` for healthy entries. If a repo can't be cloned or read (transient network, lost `repo` scope on the token, corrupted history), the project still appears in the list with `snapshots: []` and `error` set — **don't silently drop these from what you tell the user**. Surface "1 backup is unreachable" and offer to retry or check the token.

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

`snapshot_id_map` is optional and per-project; missing entries use the latest snapshot for that project. `force` applies to every project. Each project's result is independent — a 409-equivalent on one doesn't abort the rest. The list of projects to restore is discovered by enumerating `xo-project-*` repos on GitHub, so this works on a fresh machine with an empty `~/xo-projects/`.

## What gets backed up

- Project is tarred + gzipped → encrypted with the configured passphrase → split into parts to stay under GitHub's file size limit.
- If the project is a git repo, `.gitignore` is respected (via `git ls-files --cached --others --exclude-standard`). Non-git projects skip only the mandatory excludes (their `.gitignore` is NOT consulted in v1).
- Mandatory excludes regardless of `.gitignore`: `.env`, `.env.*`, `.git/`, `node_modules/`, `.venv/`, `__pycache__/`, `*.sock`.
- The destination is `<owner>/xo-project-<project_id>/<snapshot_id>/` — one repo per project, snapshots at the repo root.

## Staging

Every backup / restore / list operation uses an **ephemeral shallow clone** in a tempdir that's deleted on completion. No persistent staging directory exists between operations — disk usage when idle is zero. This is invisible to the caller; it's just useful to know that operations are stateless on the local side.

## Common error responses

| Status | When | Surface to user |
|---|---|---|
| 400 `not_configured` | `/setup` hasn't been called yet | Run setup first; ask the user for a passphrase |
| 401 `github_auth_missing` | No connector token AND no `GITHUB_PAT` | Set up auth (UI flow OR env var); see Token resolution above |
| 404 `project_not_found` | Local project doesn't exist (backup) | The project hasn't been created yet — list projects or scaffold first |
| 404 `snapshot_not_found` | No `xo-project-<id>` repo on GitHub, OR repo exists but has no valid manifests, OR pinned `snapshot_id` is wrong | Check `GET /projects` for valid ids |
| 409 `project_exists` | Restore target exists locally | Ask user to confirm overwrite, then retry with `force: true` |
| 500 `gpg_missing` | `gpg` not installed on host | Run `sudo apt-get install -y gnupg` (host responsibility, not user-fixable from chat) |
| 502 `verify_failed` | Snapshot sha256 doesn't match manifest | Snapshot is corrupted; try a different snapshot id |
| 502 `repo_create_failed` | Token lacks `repo` scope or GitHub rejected | Regenerate PAT with `repo` scope |
| 502 `git_failed` | A git op (clone, push) failed against a project's repo | Surface the error; usually transient (retry) or auth-related |