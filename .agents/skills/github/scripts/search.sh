#!/usr/bin/env bash
# search.sh — GitHub search (read-only; never gated).
#
# Usage:  search.sh --search-type repos|issues|prs|code|commits -q "QUERY" [flags]
#         (default --search-type repos)
#
# Flags:  --query/-q (required), --limit N, --language L (repos/code),
#         --sort S, --author A
#
# Search uses the dedicated search rate bucket (~30/hour). When exhausted the
# call refuses with exit 7 kind:rate_limit rather than hard-failing.
#
# Exit: 0 ok · 2 usage · 3 env · 5 timeout · 7 gh error (incl. rate_limit)

set -euo pipefail
IFS=$'\n\t'

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_common.sh
source "$HERE/_common.sh"
# shellcheck source=_gh.sh
source "$HERE/_gh.sh"

preflight
parse_flags "$@"
require_flag query "$QUERY"

stype="${SEARCH_TYPE:-repos}"
limit="${LIMIT:-$(gh_default_limit)}"

rate_guard search

case "$stype" in
  repos)
    fields="fullName,description,stargazersCount,language,isPrivate,updatedAt,url"
    args=(search repos "$QUERY" --limit "$limit" --json "$fields")
    [[ -n "$LANGUAGE" ]] && args+=(--language "$LANGUAGE")
    [[ -n "$SORT" ]]     && args+=(--sort "$SORT")
    ;;
  issues)
    fields="number,title,state,repository,author,labels,createdAt,url,isPullRequest"
    args=(search issues "$QUERY" --limit "$limit" --json "$fields")
    [[ -n "$AUTHOR" ]] && args+=(--author "$AUTHOR")
    [[ -n "$SORT" ]]   && args+=(--sort "$SORT")
    ;;
  prs)
    fields="number,title,state,repository,author,isDraft,createdAt,url"
    args=(search prs "$QUERY" --limit "$limit" --json "$fields")
    [[ -n "$AUTHOR" ]] && args+=(--author "$AUTHOR")
    [[ -n "$SORT" ]]   && args+=(--sort "$SORT")
    ;;
  code)
    fields="path,repository,sha,url"
    args=(search code "$QUERY" --limit "$limit" --json "$fields")
    [[ -n "$LANGUAGE" ]] && args+=(--language "$LANGUAGE")
    ;;
  commits)
    fields="sha,commit,author,repository,url"
    args=(search commits "$QUERY" --limit "$limit" --json "$fields")
    [[ -n "$SORT" ]] && args+=(--sort "$SORT")
    ;;
  *)
    err 2 "unknown --search-type: '$stype'" '{"types":["repos","issues","prs","code","commits"]}'
    ;;
esac

results="$(gh_capture "${args[@]}")" || exit $?
jq -e 'type=="array"' >/dev/null 2>&1 <<<"$results" || err 7 "gh search did not return an array" '{"kind":"other"}'
count="$(jq 'length' <<<"$results")"
truncated=false; if [[ "$count" -ge "$limit" ]]; then truncated=true; fi

ok "$(jq -nc --arg t "$stype" --arg q "$QUERY" --argjson c "$count" --argjson tr "$truncated" --argjson r "$results" \
  '{action:"search", search_type:$t, query:$q, count:$c, truncated:$tr, results:$r}')"
