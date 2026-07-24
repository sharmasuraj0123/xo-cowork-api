#!/usr/bin/env bash
# ls.sh — list a remote path. --tree switches to tree view.
#
# Usage:    ls.sh <provider> --remote <name> --path <p> [--tree [--depth N]] [--filter F]
# Output (list): {"ok":true, "mode":"list", "remote":"…", "path":"…", "entries":[{…}]}
# Output (tree): {"ok":true, "mode":"tree", "remote":"…", "path":"…", "text":"…"}
# Exit:     0 · 2 · 3 · 4 · 7

set -euo pipefail
IFS=$'\n\t'

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_common.sh
source "$HERE/_common.sh"
# shellcheck source=_rclone.sh
source "$HERE/_rclone.sh"

preflight
require_provider "${1:-}"; shift || true
parse_flags "$@"

require_flag remote "$REMOTE"
require_flag path   "$PATH_"
require_supported_remote "$REMOTE"
validate_remote_path "$PATH_"

target="${REMOTE}:${PATH_}"

if [[ "$TREE" == "1" ]]; then
  args=()
  [[ -n "$DEPTH"  ]] && args+=( --level "$DEPTH" )
  [[ -n "$FILTER" ]] && args+=( --filter "$FILTER" )
  text="$(rclone_capture tree "$target" "${args[@]}" || true)"
  jq -nc \
    --arg remote "$REMOTE" \
    --arg path "$PATH_" \
    --arg text "$text" \
    '{ok:true, mode:"tree", remote:$remote, path:$path, text:$text}'
  exit 0
fi

# Default: lsjson
args=( --no-modtime )
[[ -n "$DEPTH"  ]] && args+=( --max-depth "$DEPTH" )
[[ -n "$FILTER" ]] && args+=( --filter "$FILTER" )

entries="$(rclone_capture lsjson "$target" "${args[@]}")"
# lsjson already emits a JSON array; validate it parses.
if ! jq -e 'type == "array"' >/dev/null 2>&1 <<<"$entries"; then
  err 7 "rclone lsjson returned non-array output"
fi

jq -nc \
  --arg remote "$REMOTE" \
  --arg path "$PATH_" \
  --argjson entries "$entries" \
  '{ok:true, mode:"list", remote:$remote, path:$path, entries:$entries}'
