#!/usr/bin/env bash
# issue.sh — issue operations.
#
# Usage:  issue.sh --action <verb> [--repo OWNER/NAME] [flags] [--apply]
#
# Read-only (no --apply):
#   list  [--state open|closed|all] [--limit N] [--label L]... [--assignee A] [--author X]
#   view  --number N
#
# Publish/destroy (require --apply; preview + exit 6 otherwise):
#   create  --title T [--body B] [--label L]... [--assignee A]...
#   comment --number N --body B
#   close   --number N [--reason completed|"not planned"|duplicate]
#   reopen  --number N
#   label   --number N --label L...            (adds labels; gh issue edit)
#   assign  --number N --assignee A...
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

ISSUE_FIELDS="number,title,state,stateReason,author,assignees,labels,milestone,comments,body,url,createdAt,updatedAt,closedAt"

case "$ACTION" in

  list)
    args=(--state "${STATE:-open}" --limit "${LIMIT:-$(gh_default_limit)}")
    for l in "${LABELS[@]:-}"; do [[ -n "$l" ]] && args+=(--label "$l"); done
    [[ -n "$AUTHOR" ]] && args+=(--author "$AUTHOR")
    [[ "${#ASSIGNEES[@]}" -gt 0 && -n "${ASSIGNEES[0]}" ]] && args+=(--assignee "${ASSIGNEES[0]}")
    issues="$(gh_json issue list 'number,title,state,labels,assignees,author,createdAt,url' "${args[@]}")" || exit $?
    count="$(jq 'length' <<<"$issues")"
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson c "$count" --argjson issues "$issues" \
      '{action:"list", repo:$repo, count:$c, issues:$issues}')"
    ;;

  view)
    require_number
    issue="$(gh_json issue view "$ISSUE_FIELDS" "$NUMBER")" || exit $?
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson issue "$issue" '{action:"view", repo:$repo, issue:$issue}')"
    ;;

  create)
    require_flag title "$TITLE"
    if [[ "$APPLY" != "1" ]]; then
      emit_preview create "$(jq -nc --arg repo "${REPO:-}" --arg title "$TITLE" --arg body "$BODY" \
        --argjson labels "$(json_array "${LABELS[@]:-}")" \
        --argjson assignees "$(json_array "${ASSIGNEES[@]:-}")" \
        '{action:"create", repo:$repo, would_create:{title:$title, body:$body, labels:$labels, assignees:$assignees}}')"
    fi
    args=(--title "$TITLE" --body "${BODY:-}")
    for l in "${LABELS[@]:-}";   do [[ -n "$l" ]] && args+=(--label "$l"); done
    for a in "${ASSIGNEES[@]:-}";do [[ -n "$a" ]] && args+=(--assignee "$a"); done
    url="$(gh_capture issue create "${REPO_ARGS[@]}" "${args[@]}" | tail -1)" || exit $?
    ok "$(jq -nc --arg repo "${REPO:-}" --arg url "$url" '{action:"create", applied:true, repo:$repo, url:$url}')"
    ;;

  comment)
    require_number; require_flag body "$BODY"
    if [[ "$APPLY" != "1" ]]; then
      ref="$(gh_try issue view "$NUMBER" "${REPO_ARGS[@]}" --json number,title)" || true
      jq -e . >/dev/null 2>&1 <<<"$ref" || ref='{}'
      emit_preview comment "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" --arg body "$BODY" --argjson target "$ref" \
        '{action:"comment", repo:$repo, number:$n, target:$target, body:$body}')"
    fi
    url="$(gh_capture issue comment "$NUMBER" "${REPO_ARGS[@]}" --body "$BODY" | tail -1)" || exit $?
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" --arg url "$url" \
      '{action:"comment", applied:true, repo:$repo, number:$n, url:$url}')"
    ;;

  close)
    require_number
    cur="$(gh_json issue view 'number,state,stateReason' "$NUMBER")" || exit $?
    if [[ "$(jq -r '.state' <<<"$cur")" == "CLOSED" ]]; then
      ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" \
        '{action:"close", applied:false, changed:false, repo:$repo, number:$n, state:"CLOSED", note:"already closed"}')"; exit 0
    fi
    if [[ "$APPLY" != "1" ]]; then
      emit_preview close "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" --arg reason "${REASON:-}" --argjson cur "$cur" \
        '{action:"close", repo:$repo, number:$n, reason:(if $reason=="" then null else $reason end), current:$cur}')"
    fi
    cargs=()
    [[ -n "$REASON" ]] && cargs+=(--reason "$REASON")
    gh_run issue close "$NUMBER" "${REPO_ARGS[@]}" "${cargs[@]}"
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" --arg reason "${REASON:-}" \
      '{action:"close", applied:true, changed:true, repo:$repo, number:$n, reason:(if $reason=="" then null else $reason end)}')"
    ;;

  reopen)
    require_number
    cur="$(gh_json issue view 'number,state' "$NUMBER")" || exit $?
    if [[ "$(jq -r '.state' <<<"$cur")" == "OPEN" ]]; then
      ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" \
        '{action:"reopen", applied:false, changed:false, repo:$repo, number:$n, state:"OPEN", note:"already open"}')"; exit 0
    fi
    if [[ "$APPLY" != "1" ]]; then
      emit_preview reopen "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" '{action:"reopen", repo:$repo, number:$n}')"
    fi
    gh_run issue reopen "$NUMBER" "${REPO_ARGS[@]}"
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" '{action:"reopen", applied:true, changed:true, repo:$repo, number:$n}')"
    ;;

  label|assign)
    require_number
    if [[ "$ACTION" == "label" ]]; then
      [[ "${#LABELS[@]}" -gt 0 && -n "${LABELS[0]}" ]] || err 2 "label action requires at least one --label"
      add_json="$(json_array "${LABELS[@]:-}")"
    else
      [[ "${#ASSIGNEES[@]}" -gt 0 && -n "${ASSIGNEES[0]}" ]] || err 2 "assign action requires at least one --assignee"
      add_json="$(json_array "${ASSIGNEES[@]:-}")"
    fi
    if [[ "$APPLY" != "1" ]]; then
      emit_preview "$ACTION" "$(jq -nc --arg a "$ACTION" --arg repo "${REPO:-}" --argjson n "$NUMBER" --argjson add "$add_json" \
        '{action:$a, repo:$repo, number:$n, add:$add}')"
    fi
    eargs=()
    if [[ "$ACTION" == "label" ]]; then
      for l in "${LABELS[@]:-}";   do [[ -n "$l" ]] && eargs+=(--add-label "$l"); done
    else
      for a in "${ASSIGNEES[@]:-}";do [[ -n "$a" ]] && eargs+=(--add-assignee "$a"); done
    fi
    gh_run issue edit "$NUMBER" "${REPO_ARGS[@]}" "${eargs[@]}"
    ok "$(jq -nc --arg a "$ACTION" --arg repo "${REPO:-}" --argjson n "$NUMBER" --argjson add "$add_json" \
      '{action:$a, applied:true, repo:$repo, number:$n, added:$add}')"
    ;;

  *)
    err 2 "unknown issue action: '$ACTION'" \
      '{"actions":["list","view","create","comment","close","reopen","label","assign"]}'
    ;;
esac
