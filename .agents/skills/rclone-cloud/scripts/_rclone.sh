#!/usr/bin/env bash
# _rclone.sh — rclone runner pinned to the shared config + structured failure mapping.
#
# Sourced after _common.sh.

# Build the canonical rclone argv prefix.
_rclone_prefix() {
  printf -- '--config\n%s\n--stats=0\n--timeout\n%s\n--contimeout\n30s\n--low-level-retries\n3\n' \
    "$(rclone_config_path)" "$(rclone_timeout)"
}

# Run rclone <subcommand> <args...>. Captures stderr; on non-zero exit,
# classifies the failure and emits a structured JSON error then exits.
# Stdout is passed through unchanged (so JSON subcommands stream cleanly).
rclone_run() {
  local sub="${1:?subcommand required}"; shift
  local args=() arg

  while IFS= read -r arg; do args+=("$arg"); done < <(_rclone_prefix)

  local stderr_file rc
  stderr_file="$(mktemp)"
  trap 'rm -f "$stderr_file"' RETURN

  set +e
  rclone "${args[@]}" "$sub" "$@" 2>"$stderr_file"
  rc=$?
  set -e

  if (( rc != 0 )); then
    local raw kind
    raw="$(redact "$(cat "$stderr_file")")"
    # Re-echo the redacted rclone stderr to the user's stderr for visibility.
    printf '%s\n' "$raw" >&2
    kind="$(classify_rclone_err "$raw")"

    err "$rc" "rclone $sub failed" \
      "$(jq -nc --arg k "$kind" --arg s "$sub" '{kind:$k, subcommand:$s}')"
  fi
}

# Like rclone_run but captures stdout into a variable so callers can post-process.
# Usage: out="$(rclone_capture lsjson remote:path)"
rclone_capture() {
  local sub="${1:?subcommand required}"; shift
  local args=() arg
  while IFS= read -r arg; do args+=("$arg"); done < <(_rclone_prefix)

  local stdout_file stderr_file rc
  stdout_file="$(mktemp)"
  stderr_file="$(mktemp)"
  trap 'rm -f "$stdout_file" "$stderr_file"' RETURN

  set +e
  rclone "${args[@]}" "$sub" "$@" >"$stdout_file" 2>"$stderr_file"
  rc=$?
  set -e

  if (( rc != 0 )); then
    local raw; raw="$(redact "$(cat "$stderr_file")")"
    printf '%s\n' "$raw" >&2
    err "$rc" "rclone $sub failed" \
      "$(jq -nc --arg s "$sub" '{kind:"other", subcommand:$s}')"
  fi
  cat "$stdout_file"
}

# Returns a JSON array of supported remotes only:
#   [ { "name": "...", "type": "drive" | "onedrive" } ]
# Any remote with a non-supported type is dropped entirely.
rclone_listremotes_supported() {
  local raw
  raw="$(rclone_capture listremotes --long || true)"
  printf '%s\n' "$raw" | awk '
    NF == 0 { next }
    {
      # Each line: "name:    type"
      name=$1
      sub(/:$/, "", name)
      type=$2
      printf "%s\t%s\n", name, type
    }
  ' | jq -Rsc '
      split("\n")
      | map(select(length > 0)
            | split("\t")
            | {name: .[0], type: .[1]})
      | map(select(.type == "drive" or .type == "onedrive"))
    '
}

# Split rclone_listremotes_supported into per-provider arrays for status output.
rclone_listremotes_by_provider() {
  local all
  all="$(rclone_listremotes_supported)"
  jq -nc --argjson all "$all" '
    {
      gdrive:   ($all | map(select(.type == "drive"))),
      onedrive: ($all | map(select(.type == "onedrive")))
    }
  '
}
