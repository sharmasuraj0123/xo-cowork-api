#!/usr/bin/env bash
# _gh.sh — gh runner pinned to GH_TOKEN (from MCP_TOKENS) + structured failure mapping.
#
# Sourced after _common.sh. Auth is ambient: load_github_token() has already
# exported GH_TOKEN, so every gh invocation here inherits it — no per-call token
# plumbing. Never pass --show-token / -i / --include / --verbose (they leak the
# token or Authorization header); the rate-limit header read below is the one
# internal exception and is always redacted before it can surface.

# Run `gh <args...>` under a timeout. Streams stdout unchanged (so --json output
# flows through cleanly); captures stderr. On non-zero exit, redacts + classifies
# the failure and emits a structured JSON error, then exits.
#   exit-code mapping: timeout(124) -> 5 ; not_found -> 4 ; else -> 7 (with kind)
gh_run() {
  # No RETURN trap: a RETURN trap that toggles set -e is fatal when the function
  # is ever used as the left operand of `||`/`&&` inside a $( ). Clean up
  # explicitly instead — including before every err (which exits, not returns).
  local stderr_file rc raw kind code
  stderr_file="$(mktemp)"

  set +e
  timeout "$(gh_timeout)" gh "$@" 2>"$stderr_file"
  rc=$?
  set -e

  if (( rc != 0 )); then
    raw="$(redact "$(cat "$stderr_file")")"; rm -f "$stderr_file"
    printf '%s\n' "$raw" >&2
    if (( rc == 124 )); then
      err 5 "gh timed out after $(gh_timeout)" '{"kind":"network"}'
    fi
    kind="$(classify_gh_err "$raw")"
    if [[ "$kind" == "not_found" ]]; then code=4; else code=7; fi
    err "$code" "gh ${1:-} failed" \
      "$(jq -nc --arg k "$kind" --arg c "${1:-}" '{kind:$k, cmd:$c}')"
  fi
  rm -f "$stderr_file"
}

# Like gh_run but captures stdout into a variable for post-processing.
# Usage: out="$(gh_capture pr list --json number ...)"
gh_capture() {
  local stdout_file stderr_file rc raw kind code
  stdout_file="$(mktemp)"; stderr_file="$(mktemp)"

  set +e
  timeout "$(gh_timeout)" gh "$@" >"$stdout_file" 2>"$stderr_file"
  rc=$?
  set -e

  if (( rc != 0 )); then
    raw="$(redact "$(cat "$stderr_file")")"; rm -f "$stdout_file" "$stderr_file"
    printf '%s\n' "$raw" >&2
    if (( rc == 124 )); then
      err 5 "gh timed out after $(gh_timeout)" '{"kind":"network"}'
    fi
    kind="$(classify_gh_err "$raw")"
    if [[ "$kind" == "not_found" ]]; then code=4; else code=7; fi
    err "$code" "gh ${1:-} failed" \
      "$(jq -nc --arg k "$kind" --arg c "${1:-}" '{kind:$k, cmd:$c}')"
  fi
  cat "$stdout_file"
  rm -f "$stdout_file" "$stderr_file"
}

# Capture a `gh <noun> <verb> [REPO_ARGS] --json <fields> [extra...]` and
# validate it parses as JSON. Echoes the JSON.
# Usage: out="$(gh_json pr list 'number,title' --state open --limit 30)"
gh_json() {
  local noun="$1" verb="$2" fields="$3"; shift 3
  local out
  # `|| exit $?` is load-bearing: set -e does NOT propagate out of a command
  # substitution assignment, so without it a gh_capture failure (which already
  # emitted its JSON error to fd 3 and exited its subshell) would let this
  # function continue and emit a SECOND, wrong error. Every capture site that
  # must abort on failure repeats this idiom.
  out="$(gh_capture "$noun" "$verb" "${REPO_ARGS[@]}" --json "$fields" "$@")" || exit $?
  if ! jq -e . >/dev/null 2>&1 <<<"$out"; then
    err 7 "gh $noun $verb did not return valid JSON" '{"kind":"other"}'
  fi
  printf '%s' "$out"
}

# Tolerant capture for OPTIONAL data: runs gh directly (NOT through gh_capture,
# so no JSON error is ever emitted on failure), echoes stdout, swallows stderr,
# and never aborts the script. Use when a fetch failing should fall back to a
# caller default (idempotency hints, `gh pr checks` whose non-zero exit merely
# reflects check status). Pair with `|| true` and validate the output.
gh_try() {
  timeout "$(gh_timeout)" gh "$@" 2>/dev/null || true
}

# Read-only GitHub REST call. ALWAYS forces -X GET: gh api auto-switches to POST
# the instant a -f/-F param is present, so an unguarded "read" could mutate.
# Usage: out="$(api_get rate_limit ['.resources.core'])"
api_get() {
  local path="$1" jqf="${2:-}"
  local args=(api -X GET --hostname "$(gh_host)" "$path")
  [[ -n "$jqf" ]] && args+=(--jq "$jqf")
  gh_capture "${args[@]}"
}

# Refuse a publish/destroy action that lacks --apply, embedding a synthesized
# preview so a human can confirm the exact change before committing.
# Usage: emit_preview "<action>" '<preview_json>'
emit_preview() {
  local action="$1" preview="${2:-{\}}"
  err 6 "dry-run only; pass --apply to commit this $action" "$preview"
}

# Surface rate-limit state. For the search bucket (only ~30/hr) we refuse when
# exhausted; for core/graphql (5000) we only warn on low. Call before search.
# Usage: rate_guard search   |   rate_guard core
rate_guard() {
  local bucket="$1" rl remaining reset
  # Use gh_try (no internal set -e toggle / trap) for this tolerant fetch.
  rl="$(gh_try api -X GET --hostname "$(gh_host)" rate_limit --jq ".resources.${bucket} // empty")"
  [[ -z "$rl" ]] && return 0
  remaining="$(jq -r '.remaining // empty' <<<"$rl" 2>/dev/null || true)"
  reset="$(jq -r '.reset // 0' <<<"$rl" 2>/dev/null || echo 0)"
  [[ -z "$remaining" ]] && return 0
  if [[ "$bucket" == "search" ]]; then
    if (( remaining == 0 )); then
      err 7 "github search rate limit exhausted" \
        "$(jq -nc --argjson r "$reset" '{kind:"rate_limit", reset:$r, hint:"search is ~30/hour; wait until reset"}')"
    fi
    if (( remaining < 5 )); then say "warning: only $remaining github search requests left this window"; fi
  else
    if (( remaining < 100 )); then say "warning: only $remaining github $bucket API requests left this window"; fi
  fi
  # Must end on a zero status: rate_guard is called as a bare statement and a
  # trailing false conditional would make set -e abort the caller.
  return 0
}
