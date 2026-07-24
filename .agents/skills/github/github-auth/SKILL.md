---
name: github-auth
description: "GitHub auth for xo-cowork-api: the token is pre-provisioned in mcp-tokens.json and loaded via gh-env.sh — no interactive login."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Authentication, mcp-tokens, gh-cli, Setup]
    related_skills: [github-pr-workflow, github-code-review, github-issues, github-repo-management]
---

# GitHub Authentication (xo-cowork-api)

In this environment GitHub authentication is **pre-provisioned**: the OAuth
access token is minted by `xo-cowork-api` and written to the MCP tokens file
(`mcp-tokens.json`). This skill **reads** that token — it never performs an
interactive login, never asks for a personal access token or SSH key, and never
mints or refreshes tokens itself.

> **The token source never changes.** Every GitHub operation in this skill
> authenticates with the token loaded from `mcp-tokens.json`. If the token is
> missing or expired, re-mint it via `xo-cowork-api`.

## The one setup step: source `gh-env.sh`

Run this once per session, before any `gh` or `curl` GitHub call:

```bash
source "${GH_SKILL_DIR:-/home/coder/xo-cowork-api/.agents/skills/github}/github-auth/scripts/gh-env.sh"
```

It loads the token from `mcp-tokens.json`, exports `GH_TOKEN` (which both `gh`
and the `curl` examples use), and resolves your identity and repo context.

After sourcing, these variables are set:

| Variable | Meaning |
|---|---|
| `GH_TOKEN` | GitHub OAuth access token (what `gh` and `curl` use) |
| `GH_AUTH_METHOD` | `auth_method` recorded in the tokens file (informational) |
| `GH_USER` | your GitHub login |
| `GH_OWNER` / `GH_REPO` / `GH_OWNER_REPO` | repo context, when the cwd is a git repo with a GitHub remote |

## Token file

Default location: `/home/coder/.config/xo-cowork/mcp-tokens.json` (override with
the `MCP_TOKENS` environment variable). Shape:

```json
{ "github": { "access_token": "…", "expires_at": 0, "scope": "…", "auth_method": "…" } }
```

`gh-env.sh` reads `.github.access_token`, checks `.github.expires_at`, and
exports the token as `GH_TOKEN`.

## Using the token

**With `gh` (preferred):** once `GH_TOKEN` is exported, `gh` is authenticated
automatically — no extra flags or login.

```bash
gh api user --jq '.login'
gh repo view owner/repo
```

**With `curl` (REST equivalent):** pass the token in the Authorization header.

```bash
curl -s -H "Authorization: token $GH_TOKEN" https://api.github.com/user
```

## Verify

```bash
source "${GH_SKILL_DIR:-/home/coder/xo-cowork-api/.agents/skills/github}/github-auth/scripts/gh-env.sh"
gh api user --jq '.login'        # prints your login when the token is valid
```

## Troubleshooting

| Problem | Solution |
|---|---|
| `gh-env.sh` prints "NOT authenticated" | The tokens file is missing/unreadable or has no `.github.access_token`. Re-mint via `xo-cowork-api`. |
| `Bad credentials` / `HTTP 401` | Token rejected — expired or revoked. Re-mint via `xo-cowork-api`. |
| `HTTP 403` on an action | Token lacks the needed scope (e.g. `workflow`, `delete_repo`). Re-mint with the right scopes via `xo-cowork-api`. |
| `jq: command not found` | Install `jq` — `gh-env.sh` needs it to read the JSON tokens file. |
| Token expired | `gh-env.sh` warns and clears `GH_TOKEN`. Re-mint via `xo-cowork-api`. |

## Layout

```
github-auth/
├── SKILL.md          ← this file
└── scripts/
    └── gh-env.sh     ← loads GH_TOKEN from mcp-tokens.json; sets GH_USER / repo context
```
