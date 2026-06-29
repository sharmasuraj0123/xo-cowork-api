#!/usr/bin/env bash
# api.sh — allowlisted `gh api` passthrough (the long tail). Read-biased.
#
# Usage:
#   api.sh --method GET    --path <rest/path> [--raw-field K=V]... [--field K=V]...
#   api.sh --method POST   --path <rest/path> [--raw-field K=V]... [--field K=V]... --apply
#
# Policy:
#   * GET            → allowed for any path, no --apply (always sent -X GET so it
#                      cannot accidentally mutate; gh turns into POST if -f/-F is
#                      present without an explicit method).
#   * POST/PATCH/PUT/DELETE → require --apply AND match the mutating allowlist.
#   * A hard-block list (repo/org/branch-protection/secrets/keys/hooks/collab/
#     transfer/admin) is refused even WITH --apply. Use the dedicated scripts.
#
# --raw-field maps to gh -f (string); --field maps to gh -F (typed: true/false/
# null/123 coerced, @file/@- read file/stdin).
#
# Exit: 0 ok · 2 usage / not-allowlisted / hard-blocked · 3 env · 5 timeout
#       · 6 mutating without --apply · 7 gh error

set -euo pipefail
IFS=$'\n\t'

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_common.sh
source "$HERE/_common.sh"
# shellcheck source=_gh.sh
source "$HERE/_gh.sh"

preflight
parse_flags "$@"

require_flag method "$METHOD"
require_flag path   "$API_PATH"
METHOD="${METHOD^^}"

# Normalize: drop leading slash and any query string (matching is on the path).
norm="${API_PATH#/}"
norm="${norm%%\?*}"

case "$METHOD" in
  GET) RO=1 ;;
  POST|PATCH|PUT|DELETE) RO=0 ;;
  *) err 2 "method not allowed: '$METHOD'" '{"allowed_methods":["GET","POST","PATCH","PUT","DELETE"]}' ;;
esac

# --- Hard-block list (refused even with --apply) ----------------------------
HARD_BLOCK=(
  "DELETE ^repos/[^/]+/[^/]+/?$"                       # repo deletion
  "ANY ^repos/[^/]+/[^/]+/transfer/?$"                 # repo transfer
  "ANY ^orgs/"                                         # org settings/membership
  "ANY ^admin/"
  "ANY ^scim/"
  "ANY ^applications/"
  "ANY ^enterprises/"
  "ANY ^repos/[^/]+/[^/]+/actions/permissions"         # actions policy
  "ANY ^repos/[^/]+/[^/]+/actions/secrets"             # secrets
  "ANY ^repos/[^/]+/[^/]+/actions/variables"           # variables
  "ANY ^repos/[^/]+/[^/]+/branches/[^/]+/protection"   # branch protection
  "ANY ^repos/[^/]+/[^/]+/(keys|hooks)"                # deploy keys / webhooks
  "ANY ^user/keys"
  "ANY ^repos/[^/]+/[^/]+/collaborators/"              # access changes
  "DELETE ^repos/[^/]+/[^/]+/git/refs/"                # branch/tag deletion
)

# --- Mutating allowlist (method + path regex) -------------------------------
MUTATING_ALLOW=(
  "POST ^repos/[^/]+/[^/]+/issues/?$"
  "POST ^repos/[^/]+/[^/]+/issues/[0-9]+/comments/?$"
  "PATCH ^repos/[^/]+/[^/]+/issues/[0-9]+/?$"
  "POST ^repos/[^/]+/[^/]+/issues/[0-9]+/labels/?$"
  "DELETE ^repos/[^/]+/[^/]+/issues/[0-9]+/labels/[^/]+/?$"
  "POST ^repos/[^/]+/[^/]+/issues/[0-9]+/assignees/?$"
  "POST ^repos/[^/]+/[^/]+/pulls/?$"
  "PATCH ^repos/[^/]+/[^/]+/pulls/[0-9]+/?$"
  "PUT ^repos/[^/]+/[^/]+/pulls/[0-9]+/merge/?$"
  "POST ^repos/[^/]+/[^/]+/pulls/[0-9]+/reviews/?$"
  "POST ^repos/[^/]+/[^/]+/pulls/[0-9]+/comments/?$"
  "POST ^repos/[^/]+/[^/]+/releases/?$"
  "PATCH ^repos/[^/]+/[^/]+/releases/[0-9]+/?$"
  "POST ^repos/[^/]+/[^/]+/actions/runs/[0-9]+/rerun/?$"
  "POST ^repos/[^/]+/[^/]+/actions/runs/[0-9]+/rerun-failed-jobs/?$"
)

matches_rule() {  # matches_rule "<METHOD-or-ANY> <regex>" <method> <path>
  local rule="$1" m="$2" p="$3"
  local rm="${rule%% *}" rx="${rule#* }"
  [[ "$rm" == "ANY" || "$rm" == "$m" ]] && [[ "$p" =~ $rx ]]
}

# Hard-block check (applies to mutating methods; GET is read-only and exempt).
if [[ "$RO" == "0" ]]; then
  for rule in "${HARD_BLOCK[@]}"; do
    if matches_rule "$rule" "$METHOD" "$norm"; then
      err 2 "endpoint is hard-blocked: $METHOD /$norm" \
        "$(jq -nc --arg m "$METHOD" --arg p "$norm" \
          '{kind:"validation", method:$m, path:$p, reason:"hard-blocked endpoint", hint:"repo/org/branch-protection/secrets/keys/hooks/collaborator/transfer changes are intentionally unreachable; use the dedicated scripts or do it manually"}')"
    fi
  done

  allowed=0
  for rule in "${MUTATING_ALLOW[@]}"; do
    if matches_rule "$rule" "$METHOD" "$norm"; then allowed=1; break; fi
  done
  if [[ "$allowed" == "0" ]]; then
    err 2 "path '$norm' with method $METHOD is not on the mutating allowlist" \
      "$(jq -nc --arg m "$METHOD" --arg p "$norm" \
        --argjson allow "$(printf '%s\n' "${MUTATING_ALLOW[@]}" | jq -Rsc 'split("\n")|map(select(length>0))')" \
        '{method:$m, path:$p, allowed_mutating:$allow, hint:"GET is unrestricted; for other mutations use pr.sh/issue.sh/release.sh/run.sh"}')"
  fi
fi

# Build gh args. Always pin the explicit method so a GET cannot become a POST.
gh_args=(api -X "$METHOD" --hostname "$(gh_host)" "$API_PATH")
for kv in "${RAW_FIELDS[@]:-}"; do [[ -n "$kv" ]] && gh_args+=(-f "$kv"); done
for kv in "${FIELDS[@]:-}";     do [[ -n "$kv" ]] && gh_args+=(-F "$kv"); done

# Mutating ops require --apply; show a preview otherwise.
if [[ "$RO" == "0" && "$APPLY" != "1" ]]; then
  emit_preview "$METHOD" "$(jq -nc --arg m "$METHOD" --arg p "$norm" \
    --argjson raw "$(json_array "${RAW_FIELDS[@]:-}")" --argjson fields "$(json_array "${FIELDS[@]:-}")" \
    '{action:"api", method:$m, path:$p, raw_fields:$raw, fields:$fields}')"
fi

out="$(gh_capture "${gh_args[@]}")" || exit $?

# Wrap: parsed JSON if possible, else raw text.
if jq -e . >/dev/null 2>&1 <<<"$out"; then
  ok "$(jq -nc --arg m "$METHOD" --arg p "$norm" --argjson r "$out" \
    '{action:"api", method:$m, path:$p, response:$r}')"
else
  ok "$(jq -nc --arg m "$METHOD" --arg p "$norm" --arg r "$out" \
    '{action:"api", method:$m, path:$p, response_text:$r}')"
fi
