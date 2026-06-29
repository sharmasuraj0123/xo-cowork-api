#!/usr/bin/env bash
# _common.sh — shared helpers sourced by every script in the github skill.
#
# Conventions enforced here:
#   * stdout is JSON; stderr is human prose
#   * the GitHub OAuth token is read from MCP_TOKENS (.github.access_token),
#     exported as GH_TOKEN — this skill never runs `gh auth login` / OAuth
#   * targeting is via --repo OWNER/NAME (optional when cwd is a git repo)
#   * publishing AND destructive actions require --apply (dry-run preview otherwise)
#   * GitHub tokens and Authorization headers are redacted before logging

set -euo pipefail
IFS=$'\n\t'

# Save the script's original stdout to fd 3 so `err` can emit its JSON to the
# user's real stdout even when invoked inside a `$(...)` command substitution
# (those subshells inherit fd 3 unchanged from the parent, so `>&3` bypasses
# the capture pipe). Mirrors the rclone-cloud skill.
if [[ -z "${_GH_SKILL_FD_SETUP:-}" ]]; then
  exec 3>&1
  _GH_SKILL_FD_SETUP=1
fi

# ---------------------------------------------------------------------------
# Configuration paths and defaults
# ---------------------------------------------------------------------------

mcp_tokens_path() { echo "${MCP_TOKENS:-/home/coder/.config/xo-cowork/mcp-tokens.json}"; }
gh_host()         { echo "${GH_HOST:-github.com}"; }
gh_default_limit(){ echo "${GH_LIMIT:-30}"; }
gh_timeout()      { echo "${GH_TIMEOUT:-60s}"; }

# ---------------------------------------------------------------------------
# JSON I/O helpers
# ---------------------------------------------------------------------------

# Emit a JSON object on the script's real stdout (fd 3) from a JSON fragment.
# Usage: ok '{"foo":"bar"}'
ok() {
  local payload="${1:-{\}}"
  jq -nc --argjson p "$payload" '$p + {ok:true}' >&3
}

# Emit a structured error on the script's real stdout (fd 3) and exit.
# Usage: err <exit_code> "<message>" ["extra_json"]
err() {
  local code="${1:-1}"
  local msg
  msg="$(redact "${2:-error}")"
  local extra="${3:-{\}}"
  jq -nc --arg m "$msg" --argjson code "$code" --argjson extra "$extra" \
    '{ok:false, error:$m, exit:$code} + $extra' >&3
  exit "$code"
}

# Print to stderr; redacts secrets first.
say() { redact "$*" >&2; }

# Strip GitHub tokens / Authorization headers / token env assignments from any
# string before it crosses stdout or stderr.
redact() {
  local s="${1:-}"
  s="${s//$'\r'/}"
  s="$(printf '%s' "$s" | sed -E \
      -e 's/gh[opsur]_[A-Za-z0-9_]+/gh_***REDACTED***/g' \
      -e 's/github_pat_[A-Za-z0-9_]+/github_pat_***REDACTED***/g' \
      -e 's/(Authorization: *(token|Bearer) +)[^[:space:]]+/\1***/gI' \
      -e 's/(GH_TOKEN|GITHUB_TOKEN)=[^[:space:]]+/\1=***/g' \
      -e 's/(access_token|refresh_token|client_secret|client_id)=[^&[:space:]]+/\1=***/g')"
  printf '%s' "$s"
}

# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------

require_jq() {
  command -v jq >/dev/null 2>&1 || { printf '%s\n' '{"ok":false,"error":"jq not installed or not on PATH","exit":3}' >&3; exit 3; }
}

require_gh() {
  command -v gh >/dev/null 2>&1 || err 3 "gh CLI not installed or not on PATH"
}

# THE auth choke-point. Reads the GitHub OAuth token from MCP_TOKENS, validates
# it is present and not expired, and exports GH_TOKEN for every later gh call.
# Exits 3 on any environment/auth failure. OAuth/refresh is out of scope: a
# missing or expired token must be re-minted via xo-cowork-api.
#
# Exports: GH_TOKEN, GH_SCOPES, GH_AUTH_METHOD, GH_EXPIRES_AT
load_github_token() {
  local f tok exp now
  f="$(mcp_tokens_path)"

  if [[ ! -r "$f" ]]; then
    err 3 "mcp tokens file not readable at: $f" \
      '{"kind":"auth","hint":"set MCP_TOKENS or authenticate via xo-cowork-api"}'
  fi

  tok="$(jq -r '.github.access_token // empty' "$f" 2>/dev/null || true)"
  if [[ -z "$tok" ]]; then
    err 3 "no .github.access_token in mcp tokens; re-authenticate via xo-cowork-api" \
      '{"kind":"auth","hint":"this skill does not perform OAuth"}'
  fi

  exp="$(jq -r '.github.expires_at // 0' "$f" 2>/dev/null || echo 0)"
  [[ "$exp" =~ ^[0-9]+$ ]] || exp=0
  now="$(date +%s)"
  if (( exp > 0 && exp < now )); then
    err 3 "github token expired; re-mint via xo-cowork-api" \
      "$(jq -nc --argjson e "$exp" '{kind:"auth", expired_at:$e}')"
  fi

  # GitHub reports scopes comma-separated ("gist, read:org, repo"); normalize to
  # single spaces so has_scope can match on " <scope> ".
  GH_SCOPES="$(jq -r '.github.scope // ""' "$f" 2>/dev/null | tr ',' ' ' | tr -s ' ' || true)"
  GH_AUTH_METHOD="$(jq -r '.github.auth_method // ""' "$f" 2>/dev/null || true)"
  GH_EXPIRES_AT="$exp"
  export GH_TOKEN="$tok"
  # A stale GITHUB_TOKEN in the environment would shadow GH_TOKEN — drop it.
  unset GITHUB_TOKEN
  export GH_SCOPES GH_AUTH_METHOD GH_EXPIRES_AT
}

require_auth() { load_github_token; }

# Bundle of checks every command script runs before doing anything.
preflight() {
  require_jq
  require_gh
  require_auth
}

# True if the token carries the named OAuth scope (space-separated GH_SCOPES).
has_scope() {
  local want="$1"
  [[ " ${GH_SCOPES:-} " == *" $want "* ]]
}

# Tri-state scope check for previews: prints JSON true / false / null (unknown
# when the scope list is empty). Never used to hard-block — only to annotate.
scope_present_json() {
  local want="$1"
  if [[ -z "${GH_SCOPES:-}" ]]; then echo "null"
  elif has_scope "$want"; then echo "true"
  else echo "false"; fi
}

# ---------------------------------------------------------------------------
# Repo targeting
# ---------------------------------------------------------------------------

# Validates --repo (if given) and builds REPO_ARGS for gh. When --repo is
# omitted, REPO_ARGS is empty and gh resolves the repo from the cwd git remote.
# Exports: REPO, REPO_ARGS (array)
resolve_repo() {
  REPO_ARGS=()
  if [[ -n "${REPO:-}" ]]; then
    if [[ ! "$REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]; then
      err 2 "--repo must be OWNER/NAME (got '$REPO')"
    fi
    REPO_ARGS=(--repo "$REPO")
  fi
  export REPO
}

# ---------------------------------------------------------------------------
# --apply guard for publish/destroy ops
# ---------------------------------------------------------------------------

# Usage: need_apply "$APPLY"
# Exits 6 if APPLY is not set/truthy. Pair with a synthesized preview (the
# caller usually calls emit_preview instead, which embeds the preview JSON).
need_apply() {
  local v="${1:-0}"
  case "$v" in
    1|true|TRUE|yes|YES) return 0 ;;
    *) err 6 "dry-run only; pass --apply to commit this action" \
        '{"hint":"re-run the exact command with --apply once you have confirmed the preview"}' ;;
  esac
}

# ---------------------------------------------------------------------------
# Flag validation
# ---------------------------------------------------------------------------

require_flag() {
  local name="$1" value="$2"
  if [[ -z "$value" ]]; then
    err 2 "missing required flag: --$name"
  fi
}

# Validates $NUMBER is a positive integer.
require_number() {
  if [[ ! "${NUMBER:-}" =~ ^[0-9]+$ ]]; then
    err 2 "--number must be an integer (got '${NUMBER:-<empty>}')"
  fi
}

# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

# Classifies gh stderr (GraphQL / REST / gh-native string families) into
# auth | rate_limit | not_found | validation | network | other.
classify_gh_err() {
  local raw="${1:-}"
  if   grep -qi -E 'rate limit|secondary rate|API rate limit exceeded|HTTP 429|You have exceeded' <<<"$raw"; then echo "rate_limit"
  elif grep -qi -E 'Bad credentials|HTTP 401|requires authentication|GH_TOKEN.*invalid|must have admin|Resource not accessible|requires.*scope|SAML' <<<"$raw"; then echo "auth"
  elif grep -qi -E 'Could not resolve to|HTTP 404|404: Not Found|release not found|failed to get run|no .* found|not found'                       <<<"$raw"; then echo "not_found"
  elif grep -qi -E 'HTTP 422|Validation Failed|already exists|already merged'                                                                      <<<"$raw"; then echo "validation"
  elif grep -qi -E 'error connecting to|dial tcp|connection refused|timeout|deadline|TLS handshake|check your internet'                            <<<"$raw"; then echo "network"
  else                                                                                                                                                  echo "other"
  fi
}

# ---------------------------------------------------------------------------
# Long-flag parser
# ---------------------------------------------------------------------------

# Parses long-form --flag value pairs plus a few booleans. Repeatable flags
# (--label, --assignee, --reviewer, --field, --raw-field) accumulate into arrays.
# Unknown flags exit 2. Call after sourcing; no positional provider arg.
parse_flags() {
  REPO="${GH_REPO:-}"; ACTION=""; NUMBER=""; STATE=""; LIMIT=""
  TITLE=""; BODY=""; BODY_FILE=""; BASE=""; HEAD=""; REASON=""
  TAG=""; NAME=""; TARGET=""; MERGE_METHOD=""
  DRAFT="0"; PRERELEASE="0"; GEN_NOTES="0"
  QUERY=""; SEARCH_TYPE=""; LANGUAGE=""; SORT=""; AUTHOR=""
  DESCRIPTION=""; VISIBILITY=""; CLONE="0"; DIR=""
  REF=""; WORKFLOW=""; LOG_FAILED="0"; FAILED_ONLY="0"
  METHOD=""; API_PATH=""
  DELETE_BRANCH="0"
  APPLY="0"
  LABELS=(); ASSIGNEES=(); REVIEWERS=(); FIELDS=(); RAW_FIELDS=()

  while (( $# )); do
    case "$1" in
      --repo)           REPO="${2:-}"; shift 2 ;;
      --action)         ACTION="${2:-}"; shift 2 ;;
      --number)         NUMBER="${2:-}"; shift 2 ;;
      --state)          STATE="${2:-}"; shift 2 ;;
      --limit)          LIMIT="${2:-}"; shift 2 ;;
      --title)          TITLE="${2:-}"; shift 2 ;;
      --body)           BODY="${2:-}"; shift 2 ;;
      --body-file)      BODY_FILE="${2:-}"; shift 2 ;;
      --base)           BASE="${2:-}"; shift 2 ;;
      --head)           HEAD="${2:-}"; shift 2 ;;
      --reason)         REASON="${2:-}"; shift 2 ;;
      --tag)            TAG="${2:-}"; shift 2 ;;
      --name)           NAME="${2:-}"; shift 2 ;;
      --target)         TARGET="${2:-}"; shift 2 ;;
      --merge-method)   MERGE_METHOD="${2:-}"; shift 2 ;;
      --delete-branch)  DELETE_BRANCH="1"; shift ;;
      --draft)          DRAFT="1"; shift ;;
      --prerelease)     PRERELEASE="1"; shift ;;
      --generate-notes) GEN_NOTES="1"; shift ;;
      --query|-q)       QUERY="${2:-}"; shift 2 ;;
      --search-type)    SEARCH_TYPE="${2:-}"; shift 2 ;;
      --language)       LANGUAGE="${2:-}"; shift 2 ;;
      --sort)           SORT="${2:-}"; shift 2 ;;
      --author)         AUTHOR="${2:-}"; shift 2 ;;
      --description)    DESCRIPTION="${2:-}"; shift 2 ;;
      --visibility)     VISIBILITY="${2:-}"; shift 2 ;;
      --clone)          CLONE="1"; shift ;;
      --dir)            DIR="${2:-}"; shift 2 ;;
      --ref)            REF="${2:-}"; shift 2 ;;
      --workflow)       WORKFLOW="${2:-}"; shift 2 ;;
      --log-failed)     LOG_FAILED="1"; shift ;;
      --failed)         FAILED_ONLY="1"; shift ;;
      --method)         METHOD="${2:-}"; shift 2 ;;
      --path)           API_PATH="${2:-}"; shift 2 ;;
      --label)          LABELS+=("${2:-}"); shift 2 ;;
      --assignee)       ASSIGNEES+=("${2:-}"); shift 2 ;;
      --reviewer)       REVIEWERS+=("${2:-}"); shift 2 ;;
      --field)          FIELDS+=("${2:-}"); shift 2 ;;
      --raw-field)      RAW_FIELDS+=("${2:-}"); shift 2 ;;
      --apply)          APPLY="1"; shift ;;
      --) shift; break ;;
      *) err 2 "unknown flag: $1" '{"hint":"see SKILL.md for supported flags"}' ;;
    esac
  done

  export REPO ACTION NUMBER STATE LIMIT TITLE BODY BODY_FILE BASE HEAD REASON \
    TAG NAME TARGET MERGE_METHOD DRAFT PRERELEASE GEN_NOTES QUERY SEARCH_TYPE \
    LANGUAGE SORT AUTHOR DESCRIPTION VISIBILITY CLONE DIR REF WORKFLOW \
    LOG_FAILED FAILED_ONLY METHOD API_PATH DELETE_BRANCH APPLY
}

# Helper: turn a bash array into a compact JSON array of strings.
json_array() {
  if (( $# == 0 )); then echo '[]'; else printf '%s\n' "$@" | jq -Rsc 'split("\n")|map(select(length>0))'; fi
}
