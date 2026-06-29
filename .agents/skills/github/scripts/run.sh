#!/usr/bin/env bash
# run.sh — GitHub Actions workflow-run operations.
#
# Usage:  run.sh --action <verb> [--repo OWNER/NAME] [flags] [--apply]
#
# Read-only (no --apply):
#   list          [--limit N] [--workflow W] [--head BRANCH]
#   view          --number RUN_ID
#   logs          --number RUN_ID [--log-failed]
#   workflow-list
#
# Publish/destroy (require --apply; preview + exit 6 otherwise):
#   rerun             --number RUN_ID [--failed]
#   cancel            --number RUN_ID
#   delete            --number RUN_ID
#   workflow-dispatch --workflow W [--ref REF] [--raw-field K=V]...   (needs workflow scope)
#
# Exit: 0 ok · 2 usage · 3 env · 4 not found · 5 timeout · 6 needs --apply · 7 gh error

set -euo pipefail
IFS=$'\n\t'

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_common.sh
source "$HERE/_common.sh"
# shellcheck source=_gh.sh
source "$HERE/_gh.sh"

preflight
parse_flags "$@"
resolve_repo
require_flag action "$ACTION"

RUN_FIELDS="databaseId,name,displayTitle,workflowName,headBranch,headSha,event,status,conclusion,createdAt,url"

case "$ACTION" in

  list)
    args=(--limit "${LIMIT:-20}")
    [[ -n "$WORKFLOW" ]] && args+=(--workflow "$WORKFLOW")
    [[ -n "$HEAD" ]]     && args+=(--branch "$HEAD")
    runs="$(gh_json run list "$RUN_FIELDS" "${args[@]}")" || exit $?
    count="$(jq 'length' <<<"$runs")"
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson c "$count" --argjson runs "$runs" \
      '{action:"list", repo:$repo, count:$c, runs:$runs}')"
    ;;

  view)
    require_number
    run="$(gh_json run view 'databaseId,status,conclusion,workflowName,displayTitle,headBranch,headSha,event,createdAt,url,jobs' "$NUMBER")" || exit $?
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson run "$run" '{action:"view", repo:$repo, run:$run}')"
    ;;

  logs)
    require_number
    largs=(run view "$NUMBER" "${REPO_ARGS[@]}")
    [[ "$LOG_FAILED" == "1" ]] && largs+=(--log-failed) || largs+=(--log)
    text="$(gh_try "${largs[@]}")" || true
    # Logs can be large; cap to keep the JSON manageable.
    text="$(printf '%s' "$text" | tail -c 100000)"
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" --argjson lf "${LOG_FAILED:-0}" --arg t "$text" \
      '{action:"logs", repo:$repo, number:$n, log_failed:($lf==1), text:$t, note:"truncated to last 100000 bytes"}')"
    ;;

  workflow-list)
    wfs="$(gh_capture workflow list "${REPO_ARGS[@]}" --json id,name,state,path)" || exit $?
    jq -e 'type=="array"' >/dev/null 2>&1 <<<"$wfs" || err 7 "gh workflow list did not return an array" '{"kind":"other"}'
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson wfs "$wfs" '{action:"workflow-list", repo:$repo, count:($wfs|length), workflows:$wfs}')"
    ;;

  rerun|cancel|delete)
    require_number
    if [[ "$APPLY" != "1" ]]; then
      cur="$(gh_try run view "$NUMBER" "${REPO_ARGS[@]}" --json databaseId,status,conclusion,workflowName)" || true
      jq -e . >/dev/null 2>&1 <<<"$cur" || cur='null'
      # delete of a missing run is idempotent-ok (handled after apply by gh 404 → not_found),
      # but show whatever we can in the preview.
      emit_preview "$ACTION" "$(jq -nc --arg a "$ACTION" --arg repo "${REPO:-}" --argjson n "$NUMBER" \
        --argjson failed "${FAILED_ONLY:-0}" --argjson cur "$cur" \
        '{action:$a, repo:$repo, number:$n, failed_only:($failed==1), current:$cur}')"
    fi
    case "$ACTION" in
      rerun)
        rargs=(run rerun "$NUMBER" "${REPO_ARGS[@]}")
        [[ "$FAILED_ONLY" == "1" ]] && rargs+=(--failed)
        gh_run "${rargs[@]}"
        ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" --argjson f "${FAILED_ONLY:-0}" \
          '{action:"rerun", applied:true, repo:$repo, number:$n, failed_only:($f==1)}')"
        ;;
      cancel)
        gh_run run cancel "$NUMBER" "${REPO_ARGS[@]}"
        ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" '{action:"cancel", applied:true, repo:$repo, number:$n}')"
        ;;
      delete)
        gh_run run delete "$NUMBER" "${REPO_ARGS[@]}"
        ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" '{action:"delete", applied:true, deleted:true, repo:$repo, number:$n}')"
        ;;
    esac
    ;;

  workflow-dispatch)
    require_flag workflow "$WORKFLOW"
    scope="$(scope_present_json workflow)"
    inputs_json='{}'
    if [[ "${#RAW_FIELDS[@]}" -gt 0 && -n "${RAW_FIELDS[0]}" ]]; then
      inputs_json="$(printf '%s\n' "${RAW_FIELDS[@]}" | jq -Rsc 'split("\n")|map(select(length>0)|split("=")|{(.[0]):(.[1:]|join("="))})|add // {}')"
    fi
    if [[ "$APPLY" != "1" ]]; then
      emit_preview workflow-dispatch "$(jq -nc --arg repo "${REPO:-}" --arg wf "$WORKFLOW" --arg ref "${REF:-}" \
        --argjson inputs "$inputs_json" --argjson scope "$scope" \
        '{action:"workflow-dispatch", repo:$repo, workflow:$wf, ref:(if $ref=="" then "default branch" else $ref end), inputs:$inputs, scope_required:"workflow", scope_present:$scope}')"
    fi
    wargs=(workflow run "$WORKFLOW" "${REPO_ARGS[@]}")
    [[ -n "$REF" ]] && wargs+=(--ref "$REF")
    for kv in "${RAW_FIELDS[@]:-}"; do [[ -n "$kv" ]] && wargs+=(-f "$kv"); done
    gh_run "${wargs[@]}"
    ok "$(jq -nc --arg repo "${REPO:-}" --arg wf "$WORKFLOW" --arg ref "${REF:-}" --argjson inputs "$inputs_json" \
      '{action:"workflow-dispatch", applied:true, repo:$repo, workflow:$wf, ref:(if $ref=="" then "default branch" else $ref end), inputs:$inputs}')"
    ;;

  *)
    err 2 "unknown run action: '$ACTION'" \
      '{"actions":["list","view","logs","workflow-list","rerun","cancel","delete","workflow-dispatch"]}'
    ;;
esac
