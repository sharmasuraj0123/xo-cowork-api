#!/usr/bin/env bash
# delete.sh — delete a file (default) or folder (--recursive).
#
# Usage:    delete.sh <provider> --remote <name> --path <p> [--recursive] [--apply]
#
# Dry-run mode (no --apply):
#   File: stat the file and report what would be deleted (1 file, N bytes).
#   Dir:  rclone size --json + count of files to be purged.
#   Exit: 6 — refuses to delete without explicit consent.
#
# Apply mode (--apply):
#   File: rclone deletefile
#   Dir:  rclone purge (--recursive required for directories)
#   Exit: 0 ok · 7 rclone error · 4 not found if path doesn't exist.
#
# Other exits: 2 bad usage (dir without --recursive) · 3 env failure · 4 provider err

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

# Probe whether target is a file, a dir, or missing.
kind="missing"
if rclone --config "$(rclone_config_path)" --timeout "$(rclone_timeout)" \
     lsf --dirs-only --max-depth 0 "$target" >/dev/null 2>&1; then
  kind="dir"
elif rclone --config "$(rclone_config_path)" --timeout "$(rclone_timeout)" \
       lsf --files-only --max-depth 0 "${target}" >/dev/null 2>&1; then
  # Note: lsf on a single file works if we strip the trailing path component,
  # but the simplest universally-correct test is `rclone size --json` on the
  # exact path returning count >= 1.
  size_out="$(rclone --config "$(rclone_config_path)" --timeout "$(rclone_timeout)" \
              size --json "$target" 2>/dev/null || echo '{}')"
  count="$(jq -r '.count // 0' <<<"$size_out")"
  if (( count > 0 )); then kind="file"; fi
fi

# A more reliable existence check for a single file: size --json on the path
# only returns count==1 when the path is a single file.
if [[ "$kind" == "missing" ]]; then
  size_out="$(rclone --config "$(rclone_config_path)" --timeout "$(rclone_timeout)" \
              size --json "$target" 2>/dev/null || echo '{}')"
  count="$(jq -r '.count // 0' <<<"$size_out")"
  if (( count == 1 )); then
    kind="file"
  elif (( count > 1 )); then
    kind="dir"
  fi
fi

if [[ "$kind" == "missing" ]]; then
  # Idempotent: missing path → ok+deleted:false (no error).
  jq -nc \
    --arg remote "$REMOTE" --arg path "$PATH_" \
    '{ok:true, remote:$remote, path:$path, deleted:false, kind:"missing"}'
  exit 0
fi

# Directory operation requires --recursive.
if [[ "$kind" == "dir" && "$RECURSIVE" != "1" ]]; then
  err 2 "path '$PATH_' is a directory; pass --recursive to purge it"
fi

# Compute deletion preview.
size_out="$(rclone --config "$(rclone_config_path)" --timeout "$(rclone_timeout)" \
              size --json "$target" 2>/dev/null || echo '{}')"
prev_bytes="$(jq -r '.bytes // 0' <<<"$size_out")"
prev_count="$(jq -r '.count // 0' <<<"$size_out")"

# Dry-run: report preview, refuse to commit.
if [[ "$APPLY" != "1" ]]; then
  preview="$(jq -nc \
    --arg remote "$REMOTE" --arg path "$PATH_" --arg kind "$kind" \
    --argjson bytes "$prev_bytes" --argjson count "$prev_count" \
    '{remote:$remote, path:$path, kind:$kind, would_delete:{bytes:$bytes, files:$count}}')"
  err 6 "dry-run only; pass --apply to delete" "$preview"
fi

# Apply.
if [[ "$kind" == "file" ]]; then
  rclone_run deletefile "$target"
else
  rclone_run purge "$target"
fi

jq -nc \
  --arg remote "$REMOTE" --arg path "$PATH_" --arg kind "$kind" \
  --argjson bytes "$prev_bytes" --argjson count "$prev_count" \
  '{ok:true, remote:$remote, path:$path, deleted:true, kind:$kind,
    removed:{bytes:$bytes, files:$count}}'
