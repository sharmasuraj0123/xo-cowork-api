#!/usr/bin/env bash
# sync.sh — rclone sync with dry-run-by-default and --apply guard.
#
# Usage:    sync.sh <provider> --src <S> --dst <D> [--filter F] [--apply]
#
# Direction:
#   --src and --dst may be either a local path or a "<remote>:<path>" pair.
#   Any remote referenced must have type drive or onedrive.
#
# Dry-run mode (default):
#   Output: {"ok":true, "applied":false, "src":"…", "dst":"…", "preview":{transfers, deletes, …}}
#   Exit:   0
#
# Apply mode (--apply):
#   Output: {"ok":true, "applied":true,  "src":"…", "dst":"…", "stats":{…}}
#   Exit:   0 · 7 on rclone error
#
# Without --apply, exit code is still 0 (the preview is the result), but the
# user sees what *would* change and must re-invoke with --apply to commit.
# This matches the convention of cloud provisioning tools (terraform plan vs
# apply) and matches the plan's safety policy: destructive ops surface a
# preview, never silently mutate.
#
# Other exits: 2 bad usage · 3 env failure · 4 provider/remote error · 7 rclone error

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

require_flag src "$SRC"
require_flag dst "$DST"

# Identify which side(s) are remotes and validate their types.
extract_remote_name() {
  local s="$1"
  if [[ "$s" == *:* ]]; then
    printf '%s' "${s%%:*}"
  fi
}

src_remote="$(extract_remote_name "$SRC")"
dst_remote="$(extract_remote_name "$DST")"

# At least one side must be a supported remote; otherwise this is a
# local-to-local operation and `rclone sync` is the wrong tool.
if [[ -z "$src_remote" && -z "$dst_remote" ]]; then
  err 2 "at least one of --src or --dst must reference a remote (e.g. 'remote:/path')"
fi

# Validate every named remote against the provider gate.
[[ -n "$src_remote" ]] && require_supported_remote "$src_remote"
[[ -n "$dst_remote" ]] && require_supported_remote "$dst_remote"

# Build the rclone args.
sync_args=(
  --transfers "$(rclone_transfers)"
  --checkers  "$(rclone_checkers)"
  --use-json-log
)
[[ -n "$FILTER" ]] && sync_args+=( --filter "$FILTER" )

# Dry-run path: render the plan, no mutation.
if [[ "$APPLY" != "1" ]]; then
  stderr_file="$(mktemp)"
  trap 'rm -f "$stderr_file"' EXIT

  set +e
  rclone --config "$(rclone_config_path)" \
         --timeout "$(rclone_timeout)" --contimeout 30s --low-level-retries 3 \
         --stats=0 --dry-run \
         sync "$SRC" "$DST" "${sync_args[@]}" 2>"$stderr_file"
  rc=$?
  set -e

  if (( rc != 0 )); then
    raw="$(redact "$(cat "$stderr_file")")"
    printf '%s\n' "$raw" >&2
    kind="$(classify_rclone_err "$raw")"
    err "$rc" "rclone sync (dry-run) failed" \
      "$(jq -nc --arg s "sync" --arg k "$kind" '{kind:$k, subcommand:$s}')"
  fi

  # Count categories from the JSON-log skip/copy/delete messages.
  # Note: `grep -c` exits 1 on zero matches but still prints "0", so combining
  # it with `|| echo 0` doubles the output. Pipe through `wc -l` instead.
  copies="$(grep -hE '"msg":"Skipped copy"|"msg":"Copied"|"msg":"Would copy"' "$stderr_file" 2>/dev/null | wc -l | tr -d ' \n')"
  deletes="$(grep -hE '"msg":"Skipped delete"|"msg":"Deleted"|"msg":"Would delete"' "$stderr_file" 2>/dev/null | wc -l | tr -d ' \n')"
  copies="${copies:-0}"
  deletes="${deletes:-0}"

  jq -nc \
    --arg src "$SRC" \
    --arg dst "$DST" \
    --argjson copies "$copies" \
    --argjson deletes "$deletes" \
    '{ok:true, applied:false, src:$src, dst:$dst,
      hint:"this is a dry-run preview; re-run with --apply to commit",
      preview:{would_copy:$copies, would_delete:$deletes}}'
  exit 0
fi

# Apply path: real sync.
need_apply "$APPLY"

stderr_file="$(mktemp)"
trap 'rm -f "$stderr_file"' EXIT

set +e
rclone --config "$(rclone_config_path)" \
       --timeout "$(rclone_timeout)" --contimeout 30s --low-level-retries 3 \
       --stats=0 \
       sync "$SRC" "$DST" "${sync_args[@]}" 2>"$stderr_file"
rc=$?
set -e

if (( rc != 0 )); then
  raw="$(redact "$(cat "$stderr_file")")"
  printf '%s\n' "$raw" >&2
  kind="$(classify_rclone_err "$raw")"
  err "$rc" "rclone sync failed" \
    "$(jq -nc --arg s "sync" --arg k "$kind" '{kind:$k, subcommand:$s}')"
fi

summary="$(grep -hE '"msg":"Transferred:"|"transferred":' "$stderr_file" 2>/dev/null || true)"
transferred="$(echo "$summary" | tail -1 | jq -r '.stats.transfers // empty' 2>/dev/null || true)"
bytes="$(echo "$summary"      | tail -1 | jq -r '.stats.bytes // empty'     2>/dev/null || true)"
deletes="$(echo "$summary"    | tail -1 | jq -r '.stats.deletes // empty'   2>/dev/null || true)"
errors="$(echo "$summary"     | tail -1 | jq -r '.stats.errors // empty'    2>/dev/null || true)"

jq -nc \
  --arg src "$SRC" \
  --arg dst "$DST" \
  --argjson transferred "${transferred:-null}" \
  --argjson bytes       "${bytes:-null}" \
  --argjson deletes     "${deletes:-null}" \
  --argjson errors      "${errors:-null}" \
  '{ok:true, applied:true, src:$src, dst:$dst,
    stats:{transferred:$transferred, bytes:$bytes, deletes:$deletes, errors:$errors}}'
