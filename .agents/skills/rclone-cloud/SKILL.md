---
name: rclone-cloud
description: Manage files on Google Drive and OneDrive via the rclone CLI. Provides list, upload, download, sync, mkdir, delete, and inspection operations. Restricted to gdrive and onedrive remotes; refuses any other rclone backend (S3, Dropbox, etc.). Assumes the remote is already authenticated in rclone.conf — does not perform OAuth.
version: 1.0.0
providers: [gdrive, onedrive]
backend: rclone-cli
---

# rclone-cloud

A multi-agent-friendly skill that drives `rclone` against **only Google Drive and OneDrive**. Every script emits JSON on stdout so any agent can parse results uniformly.

OAuth is **out of scope** — the user authenticates separately (via `xo-cowork-api` or `rclone config`), and this skill consumes the resulting `rclone.conf`.

---

## When to use

| Use this skill | Refuse and redirect |
|---|---|
| "list my Google Drive remotes" | "connect my S3 bucket" → unsupported backend |
| "upload `report.pdf` to my Drive" | "authenticate my OneDrive" → use `xo-cowork-api` first |
| "what's in `<remote>:/Inbox`?" | "mount my Drive locally" → out of scope (no `rclone mount`) |
| "sync my project folder to OneDrive" | "set up a Dropbox remote" → unsupported backend |
| "how much space is left in my Drive?" | "serve files via WebDAV" → out of scope (no `rclone serve`) |

The skill enforces this restriction at the `_common.sh:require_supported_remote` choke-point: any remote whose `type =` in `rclone.conf` is not `drive` or `onedrive` exits 4.

---

## Prerequisites

- `rclone` ≥ 1.65 on `$PATH`
- `jq` and `bash` ≥ 4.0
- `RCLONE_CONFIG` resolves to a readable file containing at least one `[name]` section with `type = drive` or `type = onedrive`. Default: `/home/coder/.config/xo-cowork/rclone.conf`.

If no supported remote is configured, the user must authenticate elsewhere before this skill is useful.

---

## First-call protocol

**Always run `status.sh` first.** It validates the environment and tells you which remotes are available per provider.

```bash
$ ./scripts/status.sh
{"ok":true,"rclone_version":"rclone v1.74.1","config":"/home/coder/.config/xo-cowork/rclone.conf","remotes":{"gdrive":[{"name":"my-gdrive","type":"drive"}],"onedrive":[{"name":"my-onedrive","type":"onedrive"}]}}
```

Three outcomes to handle:

| Result | What to do |
|---|---|
| `ok:true` and remotes for the requested provider | Continue to the chosen operation. |
| `ok:true` but `remotes.<provider>` is empty | Tell the user to authenticate via `xo-cowork-api` (`POST /api/connectors/<provider>/remotes`) and stop. |
| `ok:false` with `exit:3` | Environment failure (rclone or jq missing, config unreadable). Surface the message and stop. |

---

## Invocation

All scripts are at `<skill-root>/scripts/<name>.sh`. Examples below assume the working directory is the skill root.

Universal contract:

- **First positional arg** is the provider — `gdrive` or `onedrive`. Anything else → exit 2.
- **Long flags only** — `--remote`, `--path`, `--src`, `--dst`, `--filter`, `--depth`, `--tree`, `--recursive`, `--subcommand`, `--flag K=V`, `--apply`.
- **stdout is JSON, stderr is human prose.** Pipe stdout to `jq`; show stderr to the user.
- **Destructive ops require `--apply`.** Without it: dry-run preview + exit 6.

---

## Scripts

### `status.sh` — environment health + supported-remote inventory

```bash
./scripts/status.sh
```

Success:
```json
{"ok":true,"rclone_version":"rclone v1.74.1","config":"/home/coder/.config/xo-cowork/rclone.conf","remotes":{"gdrive":[{"name":"my-gdrive","type":"drive"}],"onedrive":[{"name":"my-onedrive","type":"onedrive"}]}}
```

Failure (config unreadable):
```json
{"ok":false,"error":"rclone config not readable at: /bad/path","exit":3}
```

---

### `ls.sh` — list contents of a remote path

```bash
./scripts/ls.sh gdrive --remote my-gdrive --path /
./scripts/ls.sh gdrive --remote my-gdrive --path /Inbox --filter '+ *.pdf' --filter '- **'
./scripts/ls.sh gdrive --remote my-gdrive --path / --tree --depth 2
```

List mode (default):
```json
{"ok":true,"mode":"list","remote":"my-gdrive","path":"/","entries":[
  {"Path":"Inbox","Name":"Inbox","Size":0,"MimeType":"inode/directory","IsDir":true,"ID":"<folder-id>"},
  {"Path":"report.pdf","Name":"report.pdf","Size":102400,"MimeType":"application/pdf","IsDir":false,"ID":"<file-id>"}
]}
```

Tree mode:
```json
{"ok":true,"mode":"tree","remote":"my-gdrive","path":"/","text":"/\n├── Inbox\n│   └── report.pdf\n└── Projects\n"}
```

---

### `size.sh` — bytes + count for a path, plus account quota

```bash
./scripts/size.sh gdrive --remote my-gdrive                 # account totals via about
./scripts/size.sh gdrive --remote my-gdrive --path /Inbox    # folder totals
```

```json
{"ok":true,"remote":"my-gdrive","path":"/","bytes":2566402,"count":4,"about":{"free":5480851091484,"used":11201743183,"total":5497558138880,"trashed":157}}
```

`about` is `null` when the backend doesn't support it (e.g. some OneDrive contexts).

---

### `upload.sh` — local → remote (idempotent)

```bash
./scripts/upload.sh gdrive --remote my-gdrive \
  --src ./report.pdf \
  --dst /Inbox/report.pdf
```

```json
{"ok":true,"remote":"my-gdrive","src":"./report.pdf","dst":"/Inbox/report.pdf","transferred":1,"bytes":102400,"errors":0,"checks":0}
```

Re-running with unchanged content is a no-op (`transferred:0`). Wraps `rclone copy`.

---

### `download.sh` — remote → local (idempotent)

```bash
./scripts/download.sh gdrive --remote my-gdrive \
  --src /Inbox/report.pdf \
  --dst ./downloads/
```

Returns the same shape as `upload.sh`. Creates `--dst` if missing.

---

### `mkdir.sh` — create folder (idempotent)

```bash
./scripts/mkdir.sh gdrive --remote my-gdrive --path /Projects/2026
```

```json
{"ok":true,"remote":"my-gdrive","path":"/Projects/2026","created":true}
```

Existing path → `created:false`, exit 0. Never errors on "already exists".

---

### `sync.sh` — one-way sync (DESTRUCTIVE)

`sync` deletes files on the destination that don't exist on the source. Always dry-run first.

```bash
# Dry-run (default, no --apply): shows what would change, exit 0
./scripts/sync.sh gdrive --src ./project --dst my-gdrive:/Backup/project
# {"ok":true,"applied":false,"src":"./project","dst":"my-gdrive:/Backup/project",
#  "hint":"this is a dry-run preview; re-run with --apply to commit",
#  "preview":{"would_copy":12,"would_delete":3}}

# After user confirmation, commit:
./scripts/sync.sh gdrive --src ./project --dst my-gdrive:/Backup/project --apply
# {"ok":true,"applied":true,"src":"./project","dst":"my-gdrive:/Backup/project",
#  "stats":{"transferred":12,"bytes":4823901,"deletes":3,"errors":0}}
```

Direction is inferred from which side has the `<remote>:` prefix. Both sides may be remotes (cross-cloud sync) as long as their types are gdrive/onedrive.

---

### `delete.sh` — delete a file or folder (DESTRUCTIVE)

```bash
# File, dry-run (default)
./scripts/delete.sh gdrive --remote my-gdrive --path /Inbox/old.pdf
# {"ok":false,"error":"dry-run only; pass --apply to delete","exit":6,
#  "remote":"my-gdrive","path":"/Inbox/old.pdf","kind":"file","would_delete":{"bytes":102400,"files":1}}

# File, commit
./scripts/delete.sh gdrive --remote my-gdrive --path /Inbox/old.pdf --apply
# {"ok":true,"remote":"my-gdrive","path":"/Inbox/old.pdf","deleted":true,"kind":"file","removed":{"bytes":102400,"files":1}}

# Folder requires --recursive
./scripts/delete.sh gdrive --remote my-gdrive --path /Inbox
# exit 2: "path '/Inbox' is a directory; pass --recursive to purge it"

./scripts/delete.sh gdrive --remote my-gdrive --path /Inbox --recursive --apply
```

Missing path → `{"ok":true,"deleted":false,"kind":"missing"}` (idempotent).

---

### `exec.sh` — allowlisted rclone passthrough (the long tail)

For rclone subcommands without dedicated scripts. **Closed allowlist** — anything not on the list exits 2.

**Read-only subcommands** (no `--apply` needed):

`version`, `lsf`, `lsd`, `lsl`, `lsjson`, `cat`, `md5sum`, `sha1sum`, `hashsum`, `check`, `cleanup`, `dedupe`

**Mutating subcommands** (require `--apply`, both sides must be supported remotes — used for cross-cloud transfer):

`copy`, `move`

```bash
# Plain filenames at a path
./scripts/exec.sh gdrive --subcommand lsf --remote my-gdrive --path / --flag max-depth=1

# File content
./scripts/exec.sh gdrive --subcommand cat --remote my-gdrive --path /notes.txt

# Cross-cloud copy (gdrive → onedrive)
./scripts/exec.sh gdrive --subcommand copy \
  --src my-gdrive:/Inbox/ --dst my-onedrive:/Inbox/ --apply
```

**Flag allowlist** (used with `--flag K=V`):

`max-depth`, `max-age`, `min-size`, `include`, `exclude`, `filter`, `transfers`, `checkers`, `bwlimit`, `fast-list`, `no-traverse`

Unknown subcommand or flag → exit 2 with the allowlist printed in the JSON.

---

## Common workflows

### 1. Show me what's in my Drive

```bash
./scripts/status.sh                                          # confirm remote exists
./scripts/ls.sh gdrive --remote my-gdrive --path /             # list root
./scripts/size.sh gdrive --remote my-gdrive                    # free/used/quota
```

### 2. Back up a local folder to Drive

```bash
./scripts/status.sh
./scripts/mkdir.sh gdrive --remote my-gdrive --path /Backups
./scripts/sync.sh gdrive --src ./mydir --dst my-gdrive:/Backups/mydir       # dry-run
# Show user the preview, get confirmation, then:
./scripts/sync.sh gdrive --src ./mydir --dst my-gdrive:/Backups/mydir --apply
```

### 3. Pull a single file off OneDrive

```bash
./scripts/status.sh
./scripts/ls.sh onedrive --remote my-onedrive --path /Reports        # confirm the path
./scripts/download.sh onedrive --remote my-onedrive \
  --src /Reports/Q1.xlsx --dst ./
```

### 4. Move a folder from Google Drive to OneDrive

```bash
./scripts/status.sh                                          # both providers must list a remote
./scripts/exec.sh gdrive --subcommand copy \
  --src my-gdrive:/Projects/X/ --dst my-onedrive:/Projects/X/ --apply
# After verifying the copy succeeded:
./scripts/delete.sh gdrive --remote my-gdrive --path /Projects/X --recursive --apply
```

### 5. Free up Drive space — list, compare, delete

```bash
./scripts/size.sh gdrive --remote my-gdrive --path /Archive    # find a big folder
./scripts/ls.sh gdrive --remote my-gdrive --path /Archive --tree --depth 2
./scripts/delete.sh gdrive --remote my-gdrive --path /Archive/old --recursive
# preview returned, confirm with user, then --apply
```

---

## Output contract

Every script writes a single JSON object to stdout. Success and failure share these top-level keys:

```jsonc
// Success
{ "ok": true, /* operation-specific fields */ }

// Failure
{ "ok": false, "error": "<human-readable>", "exit": <code>, /* optional: "kind", "hint", "subcommand", … */ }
```

`error` strings are passed through `redact()` to strip OAuth tokens, codes, and client secrets before being emitted.

Failures from `rclone` include a `kind` field classifying the cause:

| `kind` | Meaning | Recovery |
|---|---|---|
| `auth` | Token expired or revoked | Re-authenticate via `xo-cowork-api`. |
| `quota` | API rate limit or storage full | Wait + retry, or free space. |
| `not_found` | Path doesn't exist on remote | Verify with `ls.sh`. |
| `network` | Timeout / connection refused | Retry; check connectivity. |
| `other` | Unclassified | Read the redacted rclone stderr printed alongside. |

---

## Exit codes

| Code | Meaning | Action |
|---|---|---|
| `0` | success | — |
| `2` | bad usage / unknown flag / unsupported subcommand / dir without `--recursive` | Read JSON `error` + `hint`; fix command. |
| `3` | env failure (rclone missing, jq missing, config unreadable) | Install deps; check `RCLONE_CONFIG`. |
| `4` | provider or remote error (not found, wrong type) | Run `status.sh` to see supported remotes. |
| `5` | timeout | Increase `RCLONE_TIMEOUT`; check network. |
| `6` | destructive op without `--apply` | Review preview embedded in JSON; re-run with `--apply` after user confirms. |
| `7` | rclone runtime error (passes through rclone's exit code) | Read `kind` field for category; check redacted stderr. |

---

## Safety rules

1. **Destructive ops require `--apply` in the same invocation.** Without it, the script returns a dry-run preview and exits 6. Never auto-pass `--apply` without explicit user confirmation in the *current* turn — a `--apply` from three messages ago does not authorize a new one.
2. **One in-flight destructive op at a time.** Wait for one `--apply` to return before issuing the next.
3. **Never echo or summarize `rclone.conf` contents.** It holds OAuth refresh tokens. Scripts already redact `token=`, `access_token=`, `refresh_token=`, `client_secret=`, `client_id=`, `code=`, and `Bearer …` from anything they emit; don't undo this when relaying results.
4. **Provider gate is unconditional.** Every script calls `require_supported_remote` before any rclone work. A remote with `type != drive|onedrive` is rejected — listings hide it entirely.
5. **Quote paths.** Remote paths may contain spaces. Always pass `--path '<value>'` quoted, especially for `delete.sh` and `sync.sh`.
6. **Don't run scripts under `bash -x`.** `set -x` traces variable assignments populated from `rclone config show`, which contains the literal `token = {...}` line. Redaction runs on script output, not on the shell trace. Use `bash -v` for safer debugging.

---

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `RCLONE_CONFIG` | `/home/coder/.config/xo-cowork/rclone.conf` | Path to rclone config. Shared with `xo-cowork-api`. |
| `RCLONE_TIMEOUT` | `60s` | Per-operation rclone timeout. |
| `RCLONE_TRANSFERS` | `4` | Parallel transfers for copy/sync. |
| `RCLONE_CHECKERS` | `8` | Parallel checkers for copy/sync. |

```bash
export RCLONE_CONFIG="/home/coder/.config/xo-cowork/rclone.conf"
export RCLONE_TIMEOUT="60s"
export RCLONE_TRANSFERS="4"
export RCLONE_CHECKERS="8"
```

---

## Skill layout

```
rclone-cloud/
├── SKILL.md          ← this file (the base)
└── scripts/
    ├── _common.sh    ← shared: provider gate, JSON helpers, redaction, --apply guard
    ├── _rclone.sh    ← shared: rclone runner pinned to RCLONE_CONFIG
    ├── status.sh     ← env health + remotes
    ├── ls.sh         ← list path (+ --tree)
    ├── size.sh       ← bytes/count + about
    ├── upload.sh     ← local → remote
    ├── download.sh   ← remote → local
    ├── sync.sh       ← rclone sync (destructive, --apply)
    ├── mkdir.sh      ← idempotent folder create
    ├── delete.sh     ← file or --recursive folder (destructive, --apply)
    └── exec.sh       ← allowlisted rclone passthrough
```

The two `_*.sh` files are sourced helpers — never invoked directly.
