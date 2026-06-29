#!/usr/bin/env bash
# mkdir.sh — idempotent folder create at <remote>:<path>.
#
# Usage:    mkdir.sh <provider> --remote <name> --path <p>
# Output:   {"ok":true,"remote":"…","path":"…","created":true|false}
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

# Check existence first to report created:true|false honestly.
already_exists=0
if rclone --config "$(rclone_config_path)" --timeout "$(rclone_timeout)" \
     lsf --dirs-only --max-depth 0 "$target" >/dev/null 2>&1; then
  already_exists=1
fi

if (( already_exists == 0 )); then
  rclone_run mkdir "$target"
fi

jq -nc \
  --arg remote "$REMOTE" \
  --arg path "$PATH_" \
  --argjson created "$([[ $already_exists -eq 0 ]] && echo true || echo false)" \
  '{ok:true, remote:$remote, path:$path, created:$created}'
