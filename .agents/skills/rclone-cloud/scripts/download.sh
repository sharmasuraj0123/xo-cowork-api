#!/usr/bin/env bash
# download.sh — copy a remote path to a local file or directory.
#
# Usage:    download.sh <provider> --remote <name> --src <remote_path> --dst <local> [--filter F]
# Output:   {"ok":true,"remote":"…","src":"…","dst":"…","transferred":N,"bytes":N,"errors":N,"checks":N}
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
require_flag src    "$SRC"
require_flag dst    "$DST"
require_supported_remote "$REMOTE"
validate_remote_path "$SRC"

# Create local destination directory if needed (rclone copy semantics: if dst
# is an existing directory, files land inside; otherwise rclone creates it).
mkdir -p "$DST"

source_target="${REMOTE}:${SRC}"

args=(
  --transfers "$(rclone_transfers)"
  --checkers  "$(rclone_checkers)"
  --use-json-log
)
[[ -n "$FILTER" ]] && args+=( --filter "$FILTER" )

stderr_file="$(mktemp)"
trap 'rm -f "$stderr_file"' EXIT

set +e
rclone --config "$(rclone_config_path)" \
       --timeout "$(rclone_timeout)" --contimeout 30s --low-level-retries 3 \
       --stats=0 \
       copy "$source_target" "$DST" "${args[@]}" 2>"$stderr_file"
rc=$?
set -e

if (( rc != 0 )); then
  raw="$(redact "$(cat "$stderr_file")")"
  printf '%s\n' "$raw" >&2
  kind="$(classify_rclone_err "$raw")"
  err "$rc" "rclone copy failed (download)" \
    "$(jq -nc --arg s "copy" --arg k "$kind" '{kind:$k, subcommand:$s}')"
fi

summary="$(grep -hE '"msg":"Transferred:"|"transferred":' "$stderr_file" 2>/dev/null || true)"
transferred="$(echo "$summary" | tail -1 | jq -r '.stats.transfers // empty' 2>/dev/null || true)"
bytes="$(echo "$summary"      | tail -1 | jq -r '.stats.bytes // empty'     2>/dev/null || true)"
errors="$(echo "$summary"     | tail -1 | jq -r '.stats.errors // empty'    2>/dev/null || true)"
checks="$(echo "$summary"     | tail -1 | jq -r '.stats.checks // empty'    2>/dev/null || true)"

jq -nc \
  --arg remote "$REMOTE" \
  --arg src "$SRC" \
  --arg dst "$DST" \
  --argjson transferred "${transferred:-null}" \
  --argjson bytes       "${bytes:-null}" \
  --argjson errors      "${errors:-null}" \
  --argjson checks      "${checks:-null}" \
  '{ok:true, remote:$remote, src:$src, dst:$dst,
    transferred:$transferred, bytes:$bytes, errors:$errors, checks:$checks}'
