#!/usr/bin/env bash
# GitHub environment helper for the xo-cowork-api `github` skill.
#
# Authentication is pre-provisioned by xo-cowork-api and stored in the MCP
# tokens file (mcp-tokens.json). This helper loads the GitHub OAuth token from
# there and exports GH_TOKEN for `gh` and `curl`. It never performs an
# interactive login and never mints/refreshes tokens — a missing or expired
# token must be re-issued via xo-cowork-api.
#
# Usage (via terminal tool), once per session:
#   source "${GH_SKILL_DIR:-/home/coder/xo-cowork-api/.agents/skills/github}/github-auth/scripts/gh-env.sh"
#
# After sourcing, these are set:
#   GH_TOKEN       - GitHub OAuth access token (also what `gh` reads)
#   GH_AUTH_METHOD - auth_method recorded in the tokens file (informational)
#   GH_USER        - GitHub login resolved from the token
#   GH_OWNER       - repo owner  (only inside a git repo with a github remote)
#   GH_REPO        - repo name   (only inside a git repo with a github remote)
#   GH_OWNER_REPO  - owner/repo  (only inside a git repo with a github remote)
#
# NOTE: this file is sourced — it must not `set -e` or exit, or it would kill
# the caller's shell.

# --- Token source (mcp-tokens.json; never changed by this skill) ---

_mcp_tokens="${MCP_TOKENS:-/home/coder/.config/xo-cowork/mcp-tokens.json}"

GH_TOKEN=""
GH_AUTH_METHOD=""
GH_USER=""

if ! command -v jq >/dev/null 2>&1; then
    echo "⚠ jq not found — cannot read the token from $_mcp_tokens" >&2
elif [ ! -r "$_mcp_tokens" ]; then
    echo "⚠ MCP tokens file not readable: $_mcp_tokens — re-authenticate via xo-cowork-api" >&2
else
    GH_TOKEN="$(jq -r '.github.access_token // empty' "$_mcp_tokens" 2>/dev/null)"
    GH_AUTH_METHOD="$(jq -r '.github.auth_method // empty' "$_mcp_tokens" 2>/dev/null)"
    _exp="$(jq -r '.github.expires_at // 0' "$_mcp_tokens" 2>/dev/null)"
    [ "$_exp" -eq "$_exp" ] 2>/dev/null || _exp=0
    _now="$(date +%s)"
    if [ "$_exp" -gt 0 ] && [ "$_exp" -lt "$_now" ]; then
        echo "⚠ GitHub token expired — re-mint via xo-cowork-api" >&2
        GH_TOKEN=""
    fi
    unset _exp _now
fi

if [ -n "$GH_TOKEN" ]; then
    export GH_TOKEN
else
    echo "⚠ No usable GitHub token in $_mcp_tokens — see the github-auth skill" >&2
fi

# --- Identity (uses the token just loaded) ---

if [ -n "$GH_TOKEN" ]; then
    GH_USER="$(gh api user --jq '.login' 2>/dev/null)"
    if [ -z "$GH_USER" ]; then
        GH_USER="$(curl -s -H "Authorization: token $GH_TOKEN" \
            https://api.github.com/user 2>/dev/null \
            | jq -r '.login // empty' 2>/dev/null)"
    fi
fi

# --- Repo detection (if inside a git repo with a GitHub remote) ---

GH_OWNER=""
GH_REPO=""
GH_OWNER_REPO=""

_remote_url="$(git remote get-url origin 2>/dev/null)"
if [ -n "$_remote_url" ] && echo "$_remote_url" | grep -q "github.com"; then
    GH_OWNER_REPO="$(echo "$_remote_url" | sed -E 's|.*github\.com[:/]||; s|\.git$||')"
    GH_OWNER="$(echo "$GH_OWNER_REPO" | cut -d/ -f1)"
    GH_REPO="$(echo "$GH_OWNER_REPO" | cut -d/ -f2)"
fi
unset _remote_url

# --- Summary (human prose to stderr) ---

{
  if [ -n "$GH_TOKEN" ]; then echo "GitHub: authenticated (token from mcp-tokens.json)"; else echo "GitHub: NOT authenticated"; fi
  [ -n "$GH_USER" ]       && echo "User: $GH_USER"
  [ -n "$GH_OWNER_REPO" ] && echo "Repo: $GH_OWNER_REPO"
} >&2

export GH_TOKEN GH_AUTH_METHOD GH_USER GH_OWNER GH_REPO GH_OWNER_REPO
