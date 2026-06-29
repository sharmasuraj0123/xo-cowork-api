#!/usr/bin/env bash
# upload.sh — copy a local file or directory to a remote path.
#
# Usage:    upload.sh <provider> --remote <name> --src <local> --dst <remote_path> [--filter F]
# Output:   {"ok":true,"remote":"…","src":"…","dst":"…","transferred":N,"bytes":N,"errors":N,"checks":N}
# Exit:     0 · 2 · 3 · 4 · 7
#
# rclone copy is idempotent: unchanged files are skipped, so re-running an
# upload is safe.

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
validate_remote_path "$DST"

if [[ ! -e "$SRC" ]]; then
  err 2 "local --src does not exist: $SRC"
fi

target="${REMOTE}:${DST}"

args=(
  --transfers "$(rclone_transfers)"
  --checkers  "$(rclone_checkers)"
  --use-json-log
)
[[ -n "$FILTER" ]] && args+=( --filter "$FILTER" )

# Stats end-of-run go to stderr; logs are JSON on stderr. We use a tempfile to
# parse stats reliably from "--stats=1s" not being on; instead we rely on
# rclone's final summary which is on the last lines of stderr when
# --use-json-log is set. Simpler: parse from `rclone copy --stats-one-line=false`
# isn't structured. We do a single-shot run and rely on a post-run `ls` size.

stderr_file="$(mktemp)"
trap 'rm -f "$stderr_file"' EXIT

set +e
rclone --config "$(rclone_config_path)" \
       --timeout "$(rclone_timeout)" --contimeout 30s --low-level-retries 3 \
       --stats=0 \
       copy "$SRC" "$target" "${args[@]}" 2>"$stderr_file"
rc=$?
set -e

if (( rc != 0 )); then
  raw="$(redact "$(cat "$stderr_file")")"
  printf '%s\n' "$raw" >&2
  kind="$(classify_rclone_err "$raw")"
  err "$rc" "rclone copy failed (upload)" \
    "$(jq -nc --arg s "copy" --arg k "$kind" '{kind:$k, subcommand:$s}')"
fi

# Parse JSON-log lines for the final summary if present; otherwise fall back
# to counting transferred files via a quick size diff.
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
