#!/usr/bin/env bash
# size.sh — bytes + count for a path, plus account free/used/quota.
#
# Usage:    size.sh <provider> --remote <name> [--path <p>]
# Output:   {"ok":true,"remote":"…","path":"…","bytes":N,"count":N,"about":{"free":N,"used":N,"total":N,"trashed":N}|null}
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
require_supported_remote "$REMOTE"

path="${PATH_:-/}"
validate_remote_path "$path"
target="${REMOTE}:${path}"

# 1) rclone size --json
size_json="$(rclone_capture size --json "$target")"
if ! jq -e 'type == "object"' >/dev/null 2>&1 <<<"$size_json"; then
  err 7 "rclone size returned non-object output"
fi
bytes="$(jq -r '.bytes // 0' <<<"$size_json")"
count="$(jq -r '.count // 0' <<<"$size_json")"

# 2) rclone about --json  (may not be supported by every backend; tolerate failure)
about_json="null"
if about_raw="$(rclone --config "$(rclone_config_path)" --timeout "$(rclone_timeout)" \
                 about --json "${REMOTE}:" 2>/dev/null)"; then
  if jq -e 'type == "object"' >/dev/null 2>&1 <<<"$about_raw"; then
    about_json="$(jq -c '{
      free:    (.free    // null),
      used:    (.used    // null),
      total:   (.total   // null),
      trashed: (.trashed // null)
    }' <<<"$about_raw")"
  fi
fi

jq -nc \
  --arg remote "$REMOTE" \
  --arg path "$path" \
  --argjson bytes "$bytes" \
  --argjson count "$count" \
  --argjson about "$about_json" \
  '{ok:true, remote:$remote, path:$path, bytes:$bytes, count:$count, about:$about}'
