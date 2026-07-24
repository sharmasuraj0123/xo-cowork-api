#!/usr/bin/env bash
# _common.sh — shared helpers sourced by every script in rclone-cloud.
#
# Conventions enforced here:
#   * stdout is JSON; stderr is human prose
#   * provider is restricted to gdrive | onedrive
#   * remote must have rclone type drive | onedrive (the choke-point invariant)
#   * destructive ops require --apply
#   * tokens and OAuth codes are redacted before logging

set -euo pipefail
IFS=$'\n\t'

# Save the script's original stdout to fd 3.
#
# This lets `err` emit its JSON to the user's real stdout even when a helper
# function (e.g. rclone_capture inside lsjson handling) is invoked inside a
# `$(...)` command substitution within the same script — those subshells
# inherit fd 3 unchanged from the parent, so `>&3` bypasses the capture pipe
# and reaches the terminal/pipe the script was originally writing to.
if [[ -z "${_RCLONE_SKILL_FD_SETUP:-}" ]]; then
  exec 3>&1
  _RCLONE_SKILL_FD_SETUP=1
fi

# ---------------------------------------------------------------------------
# Configuration paths and defaults
# ---------------------------------------------------------------------------

rclone_config_path() {
  echo "${RCLONE_CONFIG:-/home/coder/.config/xo-cowork/rclone.conf}"
}

rclone_timeout() { echo "${RCLONE_TIMEOUT:-60s}"; }
rclone_transfers() { echo "${RCLONE_TRANSFERS:-4}"; }
rclone_checkers() { echo "${RCLONE_CHECKERS:-8}"; }

# Provider <-> rclone backend-type mapping.
provider_to_type() {
  case "${1:-}" in
    gdrive)   echo "drive" ;;
    onedrive) echo "onedrive" ;;
    *)        return 1 ;;
  esac
}

type_to_provider() {
  case "${1:-}" in
    drive)    echo "gdrive" ;;
    onedrive) echo "onedrive" ;;
    *)        return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# JSON I/O helpers
# ---------------------------------------------------------------------------

# Emit a JSON object on the script's real stdout (fd 3) from a JSON fragment.
# Usage: ok '{"foo":"bar"}'
ok() {
  local payload="${1:-{\}}"
  jq -nc --argjson p "$payload" '$p + {ok:true}' >&3
}

# Emit a structured error on the script's real stdout (fd 3) and exit.
# Writing to fd 3 (not fd 1) means the JSON reaches the user even when err is
# triggered from inside a `$(...)` capture within the script.
# Usage: err <exit_code> "<message>" ["extra_json"]
err() {
  local code="${1:-1}"
  local msg
  msg="$(redact "${2:-error}")"
  local extra="${3:-{\}}"
  jq -nc --arg m "$msg" --argjson code "$code" --argjson extra "$extra" \
    '{ok:false, error:$m, exit:$code} + $extra' >&3
  exit "$code"
}

# Print to stderr; redacts secrets first.
say() {
  redact "$*" >&2
}

# Strip tokens / OAuth codes / client secrets from any string before
# letting it cross stdout or stderr.
redact() {
  local s="${1:-}"
  s="${s//$'\r'/}"
  # Mask query-string secrets
  s="$(echo "$s" | sed -E \
      -e 's/(access_token=)[^&[:space:]]+/\1***/g' \
      -e 's/(refresh_token=)[^&[:space:]]+/\1***/g' \
      -e 's/(client_secret=)[^&[:space:]]+/\1***/g' \
      -e 's/(client_id=)[^&[:space:]]+/\1***/g' \
      -e 's/(code=)[^&[:space:]]+/\1***/g' \
      -e 's/(token=)[^&[:space:]]+/\1***/g' \
      -e 's/(Bearer +)[A-Za-z0-9._\-]+/\1***/g')"
  # Mask any line that looks like an rclone.conf token = {...} block
  s="$(echo "$s" | sed -E 's/^(token *= *).*/\1***/')"
  printf '%s' "$s"
}

# ---------------------------------------------------------------------------
# Provider gate
# ---------------------------------------------------------------------------

# Validates the first positional arg is gdrive|onedrive.
# Exports: PROVIDER, PROVIDER_TYPE
require_provider() {
  PROVIDER="${1:-}"
  if ! PROVIDER_TYPE="$(provider_to_type "$PROVIDER" 2>/dev/null)"; then
    err 2 "unsupported provider: '${PROVIDER:-<empty>}'" \
      '{"allowed":["gdrive","onedrive"]}'
  fi
  export PROVIDER PROVIDER_TYPE
}

# THE choke-point: validates that the named remote exists and that its
# rclone type matches the requested provider. Exits 4 otherwise.
# Requires require_provider to have run first.
#
# Implementation note: the type lookup is inlined here (not factored into a
# helper that returns the type) so `err` is never called inside a command
# substitution — which would capture the JSON error into a variable instead
# of emitting it on stdout.
require_supported_remote() {
  local name="${1:?remote name required}"
  local cfg show_out type listed
  cfg="$(rclone_config_path)"

  # `rclone config show <missing>` returns exit 0 with a comment, so we must
  # check existence via `listremotes` first.
  listed="$(rclone --config "$cfg" listremotes 2>/dev/null || true)"
  if ! grep -qx "${name}:" <<<"$listed"; then
    err 4 "remote '$name' not found in rclone config" \
      "$(jq -nc --arg n "$name" '{remote:$n}')"
  fi

  show_out="$(rclone --config "$cfg" config show "$name" 2>/dev/null || true)"
  type="$(printf '%s\n' "$show_out" \
          | awk -F'=' '$1 ~ /^[[:space:]]*type[[:space:]]*$/ { gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2); print $2; exit }')"

  if [[ -z "$type" ]]; then
    err 4 "remote '$name' has no type field in config" \
      "$(jq -nc --arg n "$name" '{remote:$n}')"
  fi

  # Type must be one of the supported backends.
  if [[ "$type" != "drive" && "$type" != "onedrive" ]]; then
    err 4 "remote '$name' has type '$type'; this skill supports only Google Drive (drive) and OneDrive (onedrive)" \
      "$(jq -nc --arg t "$type" '{type:$t}')"
  fi

  # And it must match the provider passed on the command line.
  if [[ -n "${PROVIDER_TYPE:-}" && "$type" != "$PROVIDER_TYPE" ]]; then
    err 4 "remote '$name' is type '$type' but provider is '${PROVIDER}' (expects type '${PROVIDER_TYPE}')" \
      "$(jq -nc --arg t "$type" --arg p "$PROVIDER" '{actual_type:$t,provider:$p}')"
  fi
}

# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------

require_rclone() {
  if ! command -v rclone >/dev/null 2>&1; then
    err 3 "rclone not installed or not on PATH"
  fi
}

require_jq() {
  if ! command -v jq >/dev/null 2>&1; then
    err 3 "jq not installed or not on PATH"
  fi
}

require_config_readable() {
  local cfg
  cfg="$(rclone_config_path)"
  if [[ ! -r "$cfg" ]]; then
    err 3 "rclone config not readable at: $cfg"
  fi
}

# Bundle of checks every script should run before doing anything.
preflight() {
  require_jq
  require_rclone
  require_config_readable
}

# ---------------------------------------------------------------------------
# --apply guard for destructive ops
# ---------------------------------------------------------------------------

# Usage: need_apply "$APPLY"
# Exits 6 if APPLY is not "1" / "true".
need_apply() {
  local v="${1:-0}"
  case "$v" in
    1|true|TRUE|yes|YES) return 0 ;;
    *) err 6 "dry-run only; pass --apply to commit this destructive operation" \
        '{"hint":"re-run the exact command with --apply once you have confirmed the preview"}' ;;
  esac
}

# ---------------------------------------------------------------------------
# Long-flag parser
# ---------------------------------------------------------------------------

# Parses long-form --flag value pairs and a few boolean flags.
# Populates: REMOTE, SRC, DST, PATH_, FILTER, DEPTH, SUBCOMMAND, APPLY,
#            RECURSIVE, TREE, FLAGS (array of --flag K=V pairs for exec.sh).
# Unknown flags exit 2.
#
# Call after require_provider has consumed the first positional arg.
parse_flags() {
  REMOTE=""; SRC=""; DST=""; PATH_=""; FILTER=""; DEPTH=""; SUBCOMMAND=""
  APPLY="0"; RECURSIVE="0"; TREE="0"
  FLAGS=()

  while (( $# )); do
    case "$1" in
      --remote)     REMOTE="${2:-}"; shift 2 ;;
      --src)        SRC="${2:-}"; shift 2 ;;
      --dst)        DST="${2:-}"; shift 2 ;;
      --path)       PATH_="${2:-}"; shift 2 ;;
      --filter)     FILTER="${2:-}"; shift 2 ;;
      --depth)      DEPTH="${2:-}"; shift 2 ;;
      --subcommand) SUBCOMMAND="${2:-}"; shift 2 ;;
      --flag)       FLAGS+=("${2:-}"); shift 2 ;;
      --apply)      APPLY="1"; shift ;;
      --recursive)  RECURSIVE="1"; shift ;;
      --tree)       TREE="1"; shift ;;
      --) shift; break ;;
      *) err 2 "unknown flag: $1" \
           '{"hint":"see SKILL.md or references/operations.md for supported flags"}' ;;
    esac
  done
  export REMOTE SRC DST PATH_ FILTER DEPTH SUBCOMMAND APPLY RECURSIVE TREE
}

# Validate a string contains no shell-meta we don't want in a remote path.
# Allows spaces (quoted by the caller). Rejects newlines.
# (NUL can't appear in a bash variable — NUL terminates C strings — so we
# don't bother checking for it.)
validate_remote_path() {
  local p="${1:-}"
  case "$p" in
    *$'\n'*) err 2 "remote path contains forbidden newline" ;;
  esac
}

# Ensures a value is non-empty; err 2 with a friendly hint if not.
require_flag() {
  local name="$1" value="$2"
  if [[ -z "$value" ]]; then
    err 2 "missing required flag: --$name"
  fi
}

# Classifies rclone stderr text into auth | quota | not_found | network | other.
# Used by upload.sh, download.sh, sync.sh — scripts that handle stderr inline
# instead of going through rclone_run.
classify_rclone_err() {
  local raw="${1:-}"
  if   echo "$raw" | grep -qi -E 'quota|rate.?limit|storage.*full|over.*limit'; then echo "quota"
  elif echo "$raw" | grep -qi -E 'token|oauth|unauthor|invalid_grant|forbidden'; then echo "auth"
  elif echo "$raw" | grep -qi -E 'not[[:space:]]*found|no such|404';             then echo "not_found"
  elif echo "$raw" | grep -qi -E 'timeout|deadline|connection refused|temporar'; then echo "network"
  else                                                                                echo "other"
  fi
}
