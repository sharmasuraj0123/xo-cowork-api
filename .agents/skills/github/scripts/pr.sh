#!/usr/bin/env bash
# pr.sh — pull request operations.
#
# Usage:  pr.sh --action <verb> [--repo OWNER/NAME] [flags] [--apply]
#
# Read-only actions (no --apply):
#   list   [--state open|closed|merged|all] [--limit N] [--author X] [--label L]...
#   view   --number N
#   diff   --number N
#   checks --number N
#
# Publish/destroy actions (require --apply; dry-run preview + exit 6 otherwise):
#   create  --title T --head BR [--base BR] [--body B] [--draft] [--label L]... [--reviewer R]... [--assignee A]...
#   comment --number N --body B
#   merge   --number N [--merge-method merge|squash|rebase] [--delete-branch]
#   close   --number N
#   reopen  --number N
#   ready   --number N
#   review  --number N --reason approve|request-changes|comment [--body B]
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

PR_FIELDS="number,title,state,isDraft,author,baseRefName,headRefName,url,createdAt,updatedAt,mergeable,mergeStateStatus,reviewDecision,labels,additions,deletions,changedFiles,statusCheckRollup"

case "$ACTION" in

  # ------------------------------------------------------------------ read-only
  list)
    args=(--state "${STATE:-open}" --limit "${LIMIT:-$(gh_default_limit)}")
    [[ -n "$AUTHOR" ]] && args+=(--author "$AUTHOR")
    for l in "${LABELS[@]:-}"; do [[ -n "$l" ]] && args+=(--label "$l"); done
    prs="$(gh_json pr list 'number,title,state,isDraft,author,baseRefName,headRefName,url,createdAt,labels,reviewDecision' "${args[@]}")" || exit $?
    count="$(jq 'length' <<<"$prs")"
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson prs "$prs" --argjson c "$count" \
      '{action:"list", repo:$repo, count:$c, prs:$prs}')"
    ;;

  view)
    require_number
    pr="$(gh_json pr view "$PR_FIELDS" "$NUMBER")" || exit $?
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson pr "$pr" '{action:"view", repo:$repo, pr:$pr}')"
    ;;

  diff)
    require_number
    text="$(gh_capture pr diff "$NUMBER" "${REPO_ARGS[@]}")" || exit $?
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" --arg d "$text" \
      '{action:"diff", repo:$repo, number:$n, diff:$d}')"
    ;;

  checks)
    require_number
    checks="$(gh_try pr checks "$NUMBER" "${REPO_ARGS[@]}" --json name,state,bucket,link)" || true
    jq -e 'type=="array"' >/dev/null 2>&1 <<<"$checks" || checks='[]'
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" --argjson c "$checks" \
      '{action:"checks", repo:$repo, number:$n, checks:$c}')"
    ;;

  # ------------------------------------------------------------- publish/destroy
  create)
    require_flag title "$TITLE"
    require_flag head  "$HEAD"
    if [[ "$APPLY" != "1" ]]; then
      # Detect an existing open PR for this head branch (idempotency hint).
      existing="$(gh_try pr list "${REPO_ARGS[@]}" --head "$HEAD" --state open --json number,url)" || true
      jq -e 'type=="array"' >/dev/null 2>&1 <<<"$existing" || existing='[]'
      emit_preview create "$(jq -nc \
        --arg repo "${REPO:-}" --arg title "$TITLE" --arg body "$BODY" \
        --arg base "${BASE:-}" --arg head "$HEAD" --argjson draft "${DRAFT:-0}" \
        --argjson labels "$(json_array "${LABELS[@]:-}")" \
        --argjson reviewers "$(json_array "${REVIEWERS[@]:-}")" \
        --argjson existing "$existing" \
        '{action:"create", repo:$repo, would_create:{title:$title, head:$head, base:(if $base=="" then null else $base end), draft:($draft==1), body:$body, labels:$labels, reviewers:$reviewers}, existing_open_prs:$existing}')"
    fi
    args=(--title "$TITLE" --head "$HEAD")
    [[ -n "$BASE" ]] && args+=(--base "$BASE")
    if [[ -n "$BODY" ]]; then args+=(--body "$BODY"); else args+=(--body ""); fi
    [[ "$DRAFT" == "1" ]] && args+=(--draft)
    for l in "${LABELS[@]:-}";   do [[ -n "$l" ]] && args+=(--label "$l"); done
    for r in "${REVIEWERS[@]:-}";do [[ -n "$r" ]] && args+=(--reviewer "$r"); done
    for a in "${ASSIGNEES[@]:-}";do [[ -n "$a" ]] && args+=(--assignee "$a"); done
    url="$(gh_capture pr create "${REPO_ARGS[@]}" "${args[@]}" | tail -1)"
    ok "$(jq -nc --arg repo "${REPO:-}" --arg url "$url" \
      '{action:"create", applied:true, repo:$repo, url:$url}')"
    ;;

  comment)
    require_number; require_flag body "$BODY"
    if [[ "$APPLY" != "1" ]]; then
      ref="$(gh_try pr view "$NUMBER" "${REPO_ARGS[@]}" --json number,title)" || true
      jq -e . >/dev/null 2>&1 <<<"$ref" || ref='{}'
      emit_preview comment "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" \
        --arg body "$BODY" --argjson target "$ref" \
        '{action:"comment", repo:$repo, number:$n, target:$target, body:$body}')"
    fi
    url="$(gh_capture pr comment "$NUMBER" "${REPO_ARGS[@]}" --body "$BODY" | tail -1)"
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" --arg url "$url" \
      '{action:"comment", applied:true, repo:$repo, number:$n, url:$url}')"
    ;;

  merge)
    require_number
    method="${MERGE_METHOD:-squash}"
    case "$method" in merge|squash|rebase) ;; *) err 2 "--merge-method must be merge|squash|rebase" ;; esac
    # Fetch current state for the preview AND idempotency.
    cur="$(gh_json pr view 'number,title,state,mergeable,mergeStateStatus,reviewDecision,headRefName,baseRefName' "$NUMBER")" || exit $?
    state="$(jq -r '.state' <<<"$cur")"
    if [[ "$state" == "MERGED" ]]; then
      ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" \
        '{action:"merge", applied:false, changed:false, repo:$repo, number:$n, state:"MERGED", note:"already merged"}')"
      exit 0
    fi
    if [[ "$state" == "CLOSED" ]]; then
      err 7 "PR #$NUMBER is closed (unmerged); cannot merge" \
        "$(jq -nc --argjson n "$NUMBER" '{kind:"validation", number:$n, state:"CLOSED"}')"
    fi
    if [[ "$APPLY" != "1" ]]; then
      emit_preview merge "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" \
        --arg method "$method" --argjson del "${DELETE_BRANCH:-0}" --argjson cur "$cur" \
        '{action:"merge", repo:$repo, number:$n, merge_method:$method, current:$cur, hint:"review .mergeable / .mergeStateStatus before applying"}')"
    fi
    margs=(--"$method")
    # --delete-branch is opt-in via --reason delete-branch is too magic; expose via flag below.
    [[ "${DELETE_BRANCH:-0}" == "1" ]] && margs+=(--delete-branch)
    gh_run pr merge "$NUMBER" "${REPO_ARGS[@]}" "${margs[@]}"
    after="$(gh_json pr view 'number,state,url' "$NUMBER")" || exit $?
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" --arg m "$method" --argjson pr "$after" \
      '{action:"merge", applied:true, repo:$repo, number:$n, merge_method:$m, pr:$pr}')"
    ;;

  close|reopen|ready)
    require_number
    cur="$(gh_json pr view 'number,state,isDraft' "$NUMBER")" || exit $?
    state="$(jq -r '.state' <<<"$cur")"
    # Idempotent no-ops.
    if [[ "$ACTION" == "close"  && "$state" == "CLOSED" ]]; then
      ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" '{action:"close", applied:false, changed:false, repo:$repo, number:$n, state:"CLOSED", note:"already closed"}')"; exit 0
    fi
    if [[ "$ACTION" == "reopen" && "$state" == "OPEN" ]]; then
      ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" '{action:"reopen", applied:false, changed:false, repo:$repo, number:$n, state:"OPEN", note:"already open"}')"; exit 0
    fi
    if [[ "$ACTION" == "ready" && "$(jq -r '.isDraft' <<<"$cur")" == "false" ]]; then
      ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" '{action:"ready", applied:false, changed:false, repo:$repo, number:$n, note:"already ready for review"}')"; exit 0
    fi
    if [[ "$APPLY" != "1" ]]; then
      emit_preview "$ACTION" "$(jq -nc --arg a "$ACTION" --arg repo "${REPO:-}" --argjson n "$NUMBER" --argjson cur "$cur" \
        '{action:$a, repo:$repo, number:$n, current:$cur}')"
    fi
    gh_run pr "$ACTION" "$NUMBER" "${REPO_ARGS[@]}"
    ok "$(jq -nc --arg a "$ACTION" --arg repo "${REPO:-}" --argjson n "$NUMBER" \
      '{action:$a, applied:true, changed:true, repo:$repo, number:$n}')"
    ;;

  review)
    require_number; require_flag reason "$REASON"
    case "$REASON" in
      approve)         ev=(--approve) ;;
      request-changes) ev=(--request-changes) ;;
      comment)         ev=(--comment) ;;
      *) err 2 "--reason must be approve|request-changes|comment for review" ;;
    esac
    [[ "$REASON" != "approve" ]] && require_flag body "$BODY"
    if [[ "$APPLY" != "1" ]]; then
      emit_preview review "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" \
        --arg event "$REASON" --arg body "$BODY" \
        '{action:"review", repo:$repo, number:$n, event:$event, body:$body}')"
    fi
    rargs=("${ev[@]}")
    [[ -n "$BODY" ]] && rargs+=(--body "$BODY")
    gh_run pr review "$NUMBER" "${REPO_ARGS[@]}" "${rargs[@]}"
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson n "$NUMBER" --arg event "$REASON" \
      '{action:"review", applied:true, repo:$repo, number:$n, event:$event}')"
    ;;

  *)
    err 2 "unknown pr action: '$ACTION'" \
      '{"actions":["list","view","diff","checks","create","comment","merge","close","reopen","ready","review"]}'
    ;;
esac
