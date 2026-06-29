#!/usr/bin/env bash
# release.sh — release operations.
#
# Usage:  release.sh --action <verb> [--repo OWNER/NAME] [flags] [--apply]
#
# Read-only (no --apply):
#   list     [--limit N]
#   view     --tag TAG
#   download --tag TAG [--dir DIR]              (writes to local fs only; not gated)
#
# Publish/destroy (require --apply; preview + exit 6 otherwise):
#   create --tag TAG [--name N] [--body NOTES] [--generate-notes] [--draft] [--prerelease] [--target COMMITISH]
#   edit   --tag TAG [--name N] [--body NOTES] [--draft] [--prerelease]
#   delete --tag TAG
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

REL_FIELDS="tagName,name,body,isDraft,isPrerelease,createdAt,publishedAt,author,assets,url,targetCommitish"

# Returns "exists" / "missing" for a tag without emitting an error.
release_exists() {
  local t="$1"
  if gh_try release view "$t" "${REPO_ARGS[@]}" --json tagName | jq -e '.tagName' >/dev/null 2>&1; then
    echo exists
  else
    echo missing
  fi
}

case "$ACTION" in

  list)
    rels="$(gh_json release list 'tagName,name,isDraft,isPrerelease,publishedAt' --limit "${LIMIT:-$(gh_default_limit)}")" || exit $?
    count="$(jq 'length' <<<"$rels")"
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson c "$count" --argjson rels "$rels" \
      '{action:"list", repo:$repo, count:$c, releases:$rels}')"
    ;;

  view)
    require_flag tag "$TAG"
    rel="$(gh_json release view "$REL_FIELDS" "$TAG")" || exit $?
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson rel "$rel" '{action:"view", repo:$repo, release:$rel}')"
    ;;

  download)
    require_flag tag "$TAG"
    dir="${DIR:-.}"
    gh_run release download "$TAG" "${REPO_ARGS[@]}" --dir "$dir"
    ok "$(jq -nc --arg repo "${REPO:-}" --arg tag "$TAG" --arg dir "$dir" \
      '{action:"download", repo:$repo, tag:$tag, dir:$dir, note:"downloaded assets to local filesystem"}')"
    ;;

  create)
    require_flag tag "$TAG"
    # Creative idempotency: an existing tag is an ERROR (never silently succeed —
    # a release is outward-facing; ambiguous "success" could hide unpublished content).
    if [[ "$(release_exists "$TAG")" == "exists" ]]; then
      err 7 "release tag '$TAG' already exists" \
        "$(jq -nc --arg t "$TAG" '{kind:"validation", tag:$t, hint:"use --action edit, or choose a new tag"}')"
    fi
    if [[ "$APPLY" != "1" ]]; then
      emit_preview create "$(jq -nc --arg repo "${REPO:-}" --arg tag "$TAG" --arg name "$NAME" --arg body "$BODY" \
        --argjson gen "${GEN_NOTES:-0}" --argjson draft "${DRAFT:-0}" --argjson pre "${PRERELEASE:-0}" --arg target "${TARGET:-}" \
        '{action:"create", repo:$repo, would_create:{tag:$tag, name:(if $name=="" then null else $name end), notes:$body, generate_notes:($gen==1), draft:($draft==1), prerelease:($pre==1), target:(if $target=="" then null else $target end)}}')"
    fi
    args=("$TAG")
    [[ -n "$NAME" ]]        && args+=(--title "$NAME")
    [[ -n "$BODY" ]]        && args+=(--notes "$BODY")
    [[ "$GEN_NOTES" == "1" ]]  && args+=(--generate-notes)
    [[ "$DRAFT" == "1" ]]      && args+=(--draft)
    [[ "$PRERELEASE" == "1" ]] && args+=(--prerelease)
    [[ -n "$TARGET" ]]      && args+=(--target "$TARGET")
    [[ -z "$NAME" && -z "$BODY" && "$GEN_NOTES" != "1" ]] && args+=(--notes "")
    gh_run release create "${REPO_ARGS[@]}" "${args[@]}"
    after="$(gh_json release view 'tagName,url,isDraft,isPrerelease' "$TAG")" || exit $?
    ok "$(jq -nc --arg repo "${REPO:-}" --argjson rel "$after" '{action:"create", applied:true, repo:$repo, release:$rel}')"
    ;;

  edit)
    require_flag tag "$TAG"
    if [[ "$(release_exists "$TAG")" == "missing" ]]; then
      err 4 "release tag '$TAG' not found" "$(jq -nc --arg t "$TAG" '{kind:"not_found", tag:$t}')"
    fi
    if [[ "$APPLY" != "1" ]]; then
      emit_preview edit "$(jq -nc --arg repo "${REPO:-}" --arg tag "$TAG" --arg name "$NAME" --arg body "$BODY" \
        --argjson draft "${DRAFT:-0}" --argjson pre "${PRERELEASE:-0}" \
        '{action:"edit", repo:$repo, tag:$tag, changes:{name:(if $name=="" then null else $name end), notes:(if $body=="" then null else $body end), draft:($draft==1), prerelease:($pre==1)}}')"
    fi
    args=("$TAG")
    [[ -n "$NAME" ]]        && args+=(--title "$NAME")
    [[ -n "$BODY" ]]        && args+=(--notes "$BODY")
    [[ "$DRAFT" == "1" ]]      && args+=(--draft)
    [[ "$PRERELEASE" == "1" ]] && args+=(--prerelease)
    gh_run release edit "${REPO_ARGS[@]}" "${args[@]}"
    ok "$(jq -nc --arg repo "${REPO:-}" --arg tag "$TAG" '{action:"edit", applied:true, repo:$repo, tag:$tag}')"
    ;;

  delete)
    require_flag tag "$TAG"
    if [[ "$(release_exists "$TAG")" == "missing" ]]; then
      # Destructive idempotency: deleting a missing release is a no-op success.
      ok "$(jq -nc --arg repo "${REPO:-}" --arg tag "$TAG" \
        '{action:"delete", applied:false, deleted:false, repo:$repo, tag:$tag, kind:"missing"}')"; exit 0
    fi
    if [[ "$APPLY" != "1" ]]; then
      info="$(gh_try release view "$TAG" "${REPO_ARGS[@]}" --json tagName,name,assets,isDraft)" || true
      jq -e . >/dev/null 2>&1 <<<"$info" || info='null'
      emit_preview delete "$(jq -nc --arg repo "${REPO:-}" --arg tag "$TAG" --argjson info "$info" \
        '{action:"delete", repo:$repo, tag:$tag, info:$info}')"
    fi
    gh_run release delete "$TAG" "${REPO_ARGS[@]}" --yes
    ok "$(jq -nc --arg repo "${REPO:-}" --arg tag "$TAG" '{action:"delete", applied:true, deleted:true, repo:$repo, tag:$tag}')"
    ;;

  *)
    err 2 "unknown release action: '$ACTION'" \
      '{"actions":["list","view","download","create","edit","delete"]}'
    ;;
esac
