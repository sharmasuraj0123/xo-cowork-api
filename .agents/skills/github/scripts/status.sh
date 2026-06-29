#!/usr/bin/env bash
# status.sh — first-call health check: token validity, scopes, API rate limit,
#             and the resolved repo context (when cwd is a git repo).
#
# Usage:  status.sh
# Output: {"ok":true,"gh_version":"…","authenticated":true,"account":"…",
#          "scopes":[…],"token_source":"mcp-tokens.json","auth_method":"…",
#          "expires_at":N,"rate_limit":{…},"repo":{…}|null}
# Exit:   0 ok · 3 env failure (gh/jq missing, token missing/expired/rejected)
#
# This skill never performs OAuth. A missing/expired/rejected token must be
# re-minted via xo-cowork-api.

set -euo pipefail
IFS=$'\n\t'

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_common.sh
source "$HERE/_common.sh"
# shellcheck source=_gh.sh
source "$HERE/_gh.sh"

require_jq
require_gh
load_github_token   # exits 3 itself on missing/unreadable/expired token

gh_version="$(gh --version 2>/dev/null | head -1 | sed -E 's/^gh version //; s/ \(.*$//' || true)"

# Live token check (plain GET, no -f params → stays read-only).
login="$(gh api -X GET --hostname "$(gh_host)" user --jq '.login' 2>/dev/null || true)"
if [[ -z "$login" ]]; then
  err 3 "github token present but rejected by GitHub; re-mint via xo-cowork-api" \
    '{"kind":"auth","authenticated":false}'
fi

rate="$(gh api -X GET --hostname "$(gh_host)" rate_limit --jq '{
  core:    {limit:.resources.core.limit,    remaining:.resources.core.remaining,    reset:.resources.core.reset},
  search:  {limit:.resources.search.limit,  remaining:.resources.search.remaining,  reset:.resources.search.reset},
  graphql: {limit:.resources.graphql.limit, remaining:.resources.graphql.remaining, reset:.resources.graphql.reset}
}' 2>/dev/null || echo 'null')"
[[ -z "$rate" ]] && rate='null'

# Repo context — tolerate cwd not being a git repo / no remote.
repo_raw="$(gh repo view --json nameWithOwner,defaultBranchRef,isPrivate,viewerPermission 2>/dev/null || true)"
if [[ -n "$repo_raw" ]] && jq -e . >/dev/null 2>&1 <<<"$repo_raw"; then
  repo="$(jq -c '{nameWithOwner, defaultBranch:(.defaultBranchRef.name // null), isPrivate, viewerPermission}' <<<"$repo_raw")"
else
  repo='null'
fi

# Prefer the live X-OAuth-Scopes response header (accurate) over the file's
# recorded scope string (often empty for cli/app tokens). Response headers never
# contain the Authorization request header, so this -i read is safe; we extract
# only the scopes line and discard everything else.
live_scopes="$(gh api -i -X GET --hostname "$(gh_host)" user 2>/dev/null \
  | sed -nE 's/^[Xx]-[Oo][Aa]uth-[Ss]copes: *//p' | tr -d '\r' | tr ',' ' ' | tr -s ' ' | sed -E 's/^ | $//g' || true)"
eff_scopes="${live_scopes:-${GH_SCOPES:-}}"
export GH_SCOPES="$eff_scopes"   # so has_scope below reflects the live token
scopes_json="$(printf '%s' "$eff_scopes" | tr ' ' '\n' | jq -Rsc 'split("\n")|map(select(length>0))')"

# Warn only about scopes we positively know are absent (skip when scopes unknown).
if [[ -n "$eff_scopes" ]]; then
  has_scope workflow    || say "note: token lacks the 'workflow' scope — run.sh --action workflow-dispatch will fail (HTTP 403)"
  has_scope delete_repo || say "note: token lacks the 'delete_repo' scope — repo.sh --action delete will fail (HTTP 403)"
fi

ok "$(jq -nc \
  --arg v "${gh_version:-unknown}" \
  --arg login "$login" \
  --argjson scopes "$scopes_json" \
  --arg method "${GH_AUTH_METHOD:-unknown}" \
  --argjson exp "${GH_EXPIRES_AT:-0}" \
  --argjson rate "$rate" \
  --argjson repo "$repo" \
  '{gh_version:$v, authenticated:true, account:$login, scopes:$scopes,
    token_source:"mcp-tokens.json", auth_method:$method, expires_at:$exp,
    rate_limit:$rate, repo:$repo}')"
