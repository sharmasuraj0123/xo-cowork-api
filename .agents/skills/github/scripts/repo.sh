#!/usr/bin/env bash
# repo.sh — repository operations.
#
# Usage:  repo.sh --action <verb> [--repo OWNER/NAME] [flags] [--apply]
#
# Read-only (no --apply):
#   view  [--repo OWNER/NAME]
#   list  [--repo OWNER/...] [--limit N]        (owner taken from --repo's owner segment)
#   clone --repo OWNER/NAME [--dir DIR]         (writes to local fs only; not gated)
#
# Publish/destroy (require --apply; preview + exit 6 otherwise):
#   create  --name NAME [--visibility public|private|internal] [--description D] [--clone]
#   edit    --repo OWNER/NAME [--description D] [--visibility V] [--add-topic T]...
#   archive --repo OWNER/NAME
#   fork    --repo OWNER/NAME [--clone]
#   delete  --repo OWNER/NAME                   (needs delete_repo scope)
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

REPO_FIELDS="nameWithOwner,description,isPrivate,isArchived,isFork,defaultBranchRef,primaryLanguage,stargazerCount,forkCount,diskUsage,licenseInfo,pushedAt,url,repositoryTopics,visibility"

case "$ACTION" in

  view)
    # gh repo view takes the repo as a positional, not --repo.
    pos=(); [[ -n "${REPO:-}" ]] && pos=("$REPO")
    raw="$(gh_capture repo view "${pos[@]}" --json "$REPO_FIELDS")" || exit $?
    repo="$(jq -c '{nameWithOwner, description, visibility, isPrivate, isArchived, isFork,
      defaultBranch:(.defaultBranchRef.name // null),
      primaryLanguage:(.primaryLanguage.name // null),
      stargazerCount, forkCount, diskUsage,
      license:(.licenseInfo.spdxId // null),
      topics:[.repositoryTopics[]?.name // .repositoryTopics[]?], pushedAt, url}' <<<"$raw")"
    ok "$(jq -nc --argjson repo "$repo" '{action:"view", repo:$repo}')"
    ;;

  list)
    owner="${REPO%%/*}"   # owner segment of --repo, if given; else empty (current user)
    pos=(); [[ -n "$owner" && "$owner" != "$REPO" ]] && pos=("$owner")
    [[ -n "$owner" && "$owner" == "$REPO" ]] && pos=("$owner")  # bare owner with no slash
    repos="$(gh_capture repo list "${pos[@]}" --limit "${LIMIT:-$(gh_default_limit)}" --json nameWithOwner,description,isPrivate,primaryLanguage,stargazerCount,updatedAt,url)" || exit $?
    jq -e 'type=="array"' >/dev/null 2>&1 <<<"$repos" || err 7 "gh repo list did not return an array" '{"kind":"other"}'
    count="$(jq 'length' <<<"$repos")"
    ok "$(jq -nc --arg owner "${owner:-}" --argjson c "$count" --argjson repos "$repos" \
      '{action:"list", owner:(if $owner=="" then null else $owner end), count:$c, repos:$repos}')"
    ;;

  clone)
    require_flag repo "$REPO"
    dst="${DIR:-}"
    cargs=("$REPO"); [[ -n "$dst" ]] && cargs+=("$dst")
    gh_run repo clone "${cargs[@]}"
    ok "$(jq -nc --arg repo "$REPO" --arg dir "${dst:-./${REPO##*/}}" \
      '{action:"clone", repo:$repo, dir:$dir, note:"cloned to local filesystem"}')"
    ;;

  create)
    require_flag name "$NAME"
    vis="${VISIBILITY:-private}"
    case "$vis" in public|private|internal) ;; *) err 2 "--visibility must be public|private|internal" ;; esac
    if [[ "$APPLY" != "1" ]]; then
      emit_preview create "$(jq -nc --arg name "$NAME" --arg vis "$vis" --arg desc "$DESCRIPTION" --argjson clone "${CLONE:-0}" \
        '{action:"create", would_create:{name:$name, visibility:$vis, description:$desc, clone:($clone==1)}, WARNING:"creates a new public/private repo on your account"}')"
    fi
    args=("$NAME" "--$vis")
    [[ -n "$DESCRIPTION" ]] && args+=(--description "$DESCRIPTION")
    [[ "$CLONE" == "1" ]] && args+=(--clone)
    out="$(gh_capture repo create "${args[@]}" | tail -1)" || exit $?
    ok "$(jq -nc --arg name "$NAME" --arg vis "$vis" --arg out "$out" \
      '{action:"create", applied:true, name:$name, visibility:$vis, result:$out}')"
    ;;

  edit)
    require_flag repo "$REPO"
    if [[ "$APPLY" != "1" ]]; then
      emit_preview edit "$(jq -nc --arg repo "$REPO" --arg desc "$DESCRIPTION" --arg vis "${VISIBILITY:-}" \
        --argjson topics "$(json_array "${LABELS[@]:-}")" \
        '{action:"edit", repo:$repo, changes:{description:(if $desc=="" then null else $desc end), visibility:(if $vis=="" then null else $vis end), add_topics:$topics}}')"
    fi
    args=("$REPO")
    [[ -n "$DESCRIPTION" ]] && args+=(--description "$DESCRIPTION")
    [[ -n "$VISIBILITY" ]]  && args+=(--visibility "$VISIBILITY")
    for t in "${LABELS[@]:-}"; do [[ -n "$t" ]] && args+=(--add-topic "$t"); done
    gh_run repo edit "${args[@]}"
    ok "$(jq -nc --arg repo "$REPO" '{action:"edit", applied:true, repo:$repo}')"
    ;;

  archive)
    require_flag repo "$REPO"
    if [[ "$APPLY" != "1" ]]; then
      emit_preview archive "$(jq -nc --arg repo "$REPO" '{action:"archive", repo:$repo, note:"makes the repo read-only"}')"
    fi
    gh_run repo archive "$REPO" --yes
    ok "$(jq -nc --arg repo "$REPO" '{action:"archive", applied:true, repo:$repo}')"
    ;;

  fork)
    require_flag repo "$REPO"
    if [[ "$APPLY" != "1" ]]; then
      emit_preview fork "$(jq -nc --arg repo "$REPO" --argjson clone "${CLONE:-0}" \
        '{action:"fork", repo:$repo, clone:($clone==1)}')"
    fi
    fargs=("$REPO"); [[ "$CLONE" == "1" ]] && fargs+=(--clone)
    gh_run repo fork "${fargs[@]}"
    ok "$(jq -nc --arg repo "$REPO" '{action:"fork", applied:true, repo:$repo}')"
    ;;

  delete)
    require_flag repo "$REPO"
    scope="$(scope_present_json delete_repo)"
    if [[ "$APPLY" != "1" ]]; then
      info="$(gh_try repo view "$REPO" --json nameWithOwner,isPrivate,diskUsage)" || true
      jq -e . >/dev/null 2>&1 <<<"$info" || info='null'
      emit_preview delete "$(jq -nc --arg repo "$REPO" --argjson scope "$scope" --argjson info "$info" \
        '{action:"delete", repo:$repo, WARNING:"IRREVERSIBLE — permanently deletes the repository", scope_required:"delete_repo", scope_present:$scope, info:$info}')"
    fi
    gh_run repo delete "$REPO" --yes
    ok "$(jq -nc --arg repo "$REPO" '{action:"delete", applied:true, repo:$repo, deleted:true}')"
    ;;

  *)
    err 2 "unknown repo action: '$ACTION'" \
      '{"actions":["view","list","clone","create","edit","archive","fork","delete"]}'
    ;;
esac
