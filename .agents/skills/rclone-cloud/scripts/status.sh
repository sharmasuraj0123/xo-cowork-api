#!/usr/bin/env bash
# status.sh — rclone + config health, plus the list of supported remotes per provider.
#
# Usage:    status.sh
# Output:   {"ok":true,"rclone_version":"…","config":"…","remotes":{"gdrive":[…],"onedrive":[…]}}
# Exit:     0 ok · 3 env failure

set -euo pipefail
IFS=$'\n\t'

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=_common.sh
source "$HERE/_common.sh"
# shellcheck source=_rclone.sh
source "$HERE/_rclone.sh"

preflight

version_line="$(rclone --config "$(rclone_config_path)" version 2>/dev/null | head -1 || true)"
remotes_json="$(rclone_listremotes_by_provider)"

jq -nc \
  --arg v "${version_line:-unknown}" \
  --arg cfg "$(rclone_config_path)" \
  --argjson r "$remotes_json" \
  '{ok:true, rclone_version:$v, config:$cfg, remotes:$r}'
