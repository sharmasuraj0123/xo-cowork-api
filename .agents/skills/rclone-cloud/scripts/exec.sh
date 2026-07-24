#!/usr/bin/env bash
# exec.sh — dynamic, allowlisted rclone subcommand passthrough.
#
# Usage:
#   exec.sh <provider> --subcommand X --remote N [--path P] [--src S] [--dst D]
#           [--flag KEY=VALUE]... [--apply]
#
# Allowlist policy:
#   * Read-only subcommands: lsf, lsd, lsl, lsjson, cat, md5sum, sha1sum,
#                            hashsum, version, check, cleanup, dedupe
#   * Cross-cloud transfers: copy, move
#                            (both --src and --dst MUST be remotes; their
#                            types must be in {drive, onedrive}; --apply
#                            is required)
#   * Anything else        : exit 2 with the allowlist printed.
#
# Flag allowlist:
#   --max-depth, --max-age, --min-size, --include, --exclude, --filter,
#   --transfers, --checkers, --bwlimit, --fast-list, --no-traverse
#
# Unknown subcommand or flag → exit 2.
# Provider gate still applies to every --remote / --src / --dst.

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

require_flag subcommand "$SUBCOMMAND"

# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

READONLY_SUBS=(lsf lsd lsl lsjson cat md5sum sha1sum hashsum version check cleanup dedupe)
MUTATING_SUBS=(copy move)
ALLOWED_FLAGS=(max-depth max-age min-size include exclude filter transfers checkers bwlimit fast-list no-traverse)

in_array() {
  local needle="$1"; shift
  local x
  for x in "$@"; do [[ "$x" == "$needle" ]] && return 0; done
  return 1
}

if ! in_array "$SUBCOMMAND" "${READONLY_SUBS[@]}" "${MUTATING_SUBS[@]}"; then
  err 2 "subcommand '$SUBCOMMAND' is not on the allowlist" \
    "$(jq -nc \
        --argjson ro "$(printf '%s\n' "${READONLY_SUBS[@]}" | jq -R . | jq -s .)" \
        --argjson mu "$(printf '%s\n' "${MUTATING_SUBS[@]}" | jq -R . | jq -s .)" \
        '{readonly:$ro, mutating:$mu, hint:"mutating ops outside this list (delete, sync, mkdir, upload, download) have dedicated scripts"}')"
fi

# ---------------------------------------------------------------------------
# Validate --flag K=V entries
# ---------------------------------------------------------------------------

extra_args=()
for kv in "${FLAGS[@]}"; do
  [[ -z "$kv" ]] && continue
  if [[ "$kv" != *=* ]]; then
    err 2 "--flag value must be KEY=VALUE (got '$kv')"
  fi
  k="${kv%%=*}"
  v="${kv#*=}"
  if ! in_array "$k" "${ALLOWED_FLAGS[@]}"; then
    err 2 "flag '$k' not on allowlist" \
      "$(jq -nc --argjson a "$(printf '%s\n' "${ALLOWED_FLAGS[@]}" | jq -R . | jq -s .)" '{allowed:$a}')"
  fi
  extra_args+=( "--$k" "$v" )
done

# ---------------------------------------------------------------------------
# Resolve the rclone arg position(s) — different subcommands take 0/1/2 args.
# ---------------------------------------------------------------------------

# Helper: build "<remote>:<path>" if --remote and --path given, else accept --src.
build_remote_target() {
  if [[ -n "$REMOTE" ]]; then
    require_supported_remote "$REMOTE"
    local p="${PATH_:-}"
    printf '%s:%s' "$REMOTE" "$p"
  elif [[ -n "$SRC" ]]; then
    local src_remote="${SRC%%:*}"
    if [[ "$src_remote" != "$SRC" ]]; then
      require_supported_remote "$src_remote"
    fi
    printf '%s' "$SRC"
  else
    err 2 "this subcommand requires --remote (and optionally --path) or --src"
  fi
}

# Mutating subcommands (copy, move) require both --src and --dst, both must
# reference supported remotes, and --apply is mandatory.
if in_array "$SUBCOMMAND" "${MUTATING_SUBS[@]}"; then
  require_flag src "$SRC"
  require_flag dst "$DST"

  src_remote="${SRC%%:*}"
  dst_remote="${DST%%:*}"
  if [[ "$src_remote" == "$SRC" || "$dst_remote" == "$DST" ]]; then
    err 2 "cross-cloud --subcommand $SUBCOMMAND requires both --src and --dst to be remotes (e.g. 'gdrive-r:/path' and 'onedrive-r:/path')"
  fi
  require_supported_remote "$src_remote"
  require_supported_remote "$dst_remote"

  if [[ "$APPLY" != "1" ]]; then
    # Honor the destructive-policy: refuse without --apply, show preview.
    preview="$(jq -nc \
      --arg src "$SRC" --arg dst "$DST" --arg sub "$SUBCOMMAND" \
      '{subcommand:$sub, src:$src, dst:$dst}')"
    err 6 "dry-run only; pass --apply to commit cross-cloud $SUBCOMMAND" "$preview"
  fi

  rclone_run "$SUBCOMMAND" "$SRC" "$DST" "${extra_args[@]}"
  jq -nc \
    --arg sub "$SUBCOMMAND" --arg src "$SRC" --arg dst "$DST" \
    '{ok:true, subcommand:$sub, applied:true, src:$src, dst:$dst}'
  exit 0
fi

# Read-only subcommands: most take a single remote:path or remote: arg.
case "$SUBCOMMAND" in
  version)
    raw="$(rclone_capture version "${extra_args[@]}" || true)"
    jq -nc --arg out "$raw" '{ok:true, subcommand:"version", stdout:$out}'
    ;;
  lsjson)
    target="$(build_remote_target)"
    raw="$(rclone_capture lsjson "$target" "${extra_args[@]}")"
    if ! jq -e 'type == "array"' >/dev/null 2>&1 <<<"$raw"; then
      err 7 "rclone lsjson did not return a JSON array"
    fi
    jq -nc --arg target "$target" --argjson entries "$raw" \
      '{ok:true, subcommand:"lsjson", target:$target, entries:$entries}'
    ;;
  cleanup)
    target="$(build_remote_target)"
    rclone_run cleanup "$target" "${extra_args[@]}"
    jq -nc --arg target "$target" '{ok:true, subcommand:"cleanup", target:$target}'
    ;;
  check)
    require_flag src "$SRC"
    require_flag dst "$DST"
    src_remote="${SRC%%:*}"
    dst_remote="${DST%%:*}"
    [[ "$src_remote" != "$SRC" ]] && require_supported_remote "$src_remote"
    [[ "$dst_remote" != "$DST" ]] && require_supported_remote "$dst_remote"
    raw="$(rclone_capture check "$SRC" "$DST" "${extra_args[@]}" || true)"
    jq -nc --arg src "$SRC" --arg dst "$DST" --arg out "$raw" \
      '{ok:true, subcommand:"check", src:$src, dst:$dst, stdout:$out}'
    ;;
  *)
    # lsf / lsd / lsl / cat / md5sum / sha1sum / hashsum / dedupe — generic text output.
    target="$(build_remote_target)"
    raw="$(rclone_capture "$SUBCOMMAND" "$target" "${extra_args[@]}" || true)"
    jq -nc --arg sub "$SUBCOMMAND" --arg target "$target" --arg out "$raw" \
      '{ok:true, subcommand:$sub, target:$target, stdout:$out}'
    ;;
esac
