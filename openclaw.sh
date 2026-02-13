#!/usr/bin/env bash
# ==============================================================
# OpenClaw OneClick Setup & Gateway Manager
# Combined setup + gateway management in a single script.
#
# Usage: ./openclaw.sh {setup|start|stop|restart|status|logs}
#
# Required env vars (set in .env):
#     TELEGRAM_BOT_TOKEN      - Telegram bot token from BotFather
#     OPENCLAW_GATEWAY_TOKEN  - Gateway auth token
#     CLAUDE_SETUP_TOKEN      - Token from 'claude setup-token' (Claude Pro/Max)
#
# Optional env vars:
#     TELEGRAM_ENABLED        - enable/disable Telegram channel (default: true)
#     TELEGRAM_ALLOW_FROM     - Telegram user ID to restrict DMs to (optional)
#     OPENCLAW_MAX_RESTARTS   - max consecutive restarts (default: 10)
#     OPENCLAW_RESTART_DELAY  - seconds between restarts (default: 5)
#     OPENCLAW_RESTART_WINDOW - seconds of uptime to reset counter (default: 300)
#     OPENCLAW_LOG_MAX_SIZE   - max log size in bytes before rotation (default: 10485760 / 10MB)
# ==============================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENCLAW_DIR="${HOME}/.openclaw"
CONFIG_FILE="${OPENCLAW_DIR}/openclaw.json"
PID_FILE="/tmp/openclaw-gateway.pid"
LOG_FILE="/tmp/openclaw-gateway.log"
LOCK_FILE="/tmp/openclaw-gateway.lock"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()         { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

# --- Load .env safely (don't fail on unset vars) ---
load_env() {
    local env_file="$1"
    if [ -f "$env_file" ]; then
        set +u
        set -a
        source "$env_file"
        set +a
        set -u
    fi
}

load_env "$SCRIPT_DIR/.env"
load_env "$OPENCLAW_DIR/.env"

# --- Settings ---
RESTART_DELAY="${OPENCLAW_RESTART_DELAY:-5}"
MAX_RESTARTS="${OPENCLAW_MAX_RESTARTS:-10}"
RESTART_WINDOW="${OPENCLAW_RESTART_WINDOW:-300}"
LOG_MAX_SIZE="${OPENCLAW_LOG_MAX_SIZE:-10485760}"

# ==============================================================
# Helpers
# ==============================================================

# Rotate log if it exceeds max size
rotate_log() {
    if [ -f "$LOG_FILE" ]; then
        local size
        size=$(stat -c%s "$LOG_FILE" 2>/dev/null || stat -f%z "$LOG_FILE" 2>/dev/null || echo 0)
        if [ "$size" -ge "$LOG_MAX_SIZE" ]; then
            mv "$LOG_FILE" "${LOG_FILE}.1"
            log "Log rotated (was ${size} bytes)"
        fi
    fi
}

# Acquire a lock to prevent concurrent start/stop races
LOCK_ACQUIRED=0
acquire_lock() {
    if [ "$LOCK_ACQUIRED" -eq 1 ]; then
        return 0
    fi
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        log_error "Another openclaw.sh operation is in progress"
        exit 1
    fi
    LOCK_ACQUIRED=1
}

# Clean stale PID file if process is dead
clean_stale_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if ! kill -0 "$pid" 2>/dev/null; then
            log_warn "Removing stale PID file (process $pid is dead)"
            rm -f "$PID_FILE"
        fi
    fi
}

# Find orphan "openclaw gateway run" processes not managed by our wrapper
find_orphan_gateways() {
    local managed_wrapper_pid=""
    if [ -f "$PID_FILE" ]; then
        managed_wrapper_pid=$(cat "$PID_FILE" 2>/dev/null || true)
    fi

    local gw_pids
    gw_pids=$(pgrep -f "openclaw gateway run" 2>/dev/null || true)
    [ -z "$gw_pids" ] && return

    for gw_pid in $gw_pids; do
        # Skip if it's a child of our managed wrapper
        if [ -n "$managed_wrapper_pid" ] && kill -0 "$managed_wrapper_pid" 2>/dev/null; then
            local parent
            parent=$(ps -o ppid= -p "$gw_pid" 2>/dev/null | tr -d ' ')
            [ "$parent" = "$managed_wrapper_pid" ] && continue
        fi
        echo "$gw_pid"
    done
}

# Kill orphan gateway processes and warn
kill_orphan_gateways() {
    local orphans
    orphans=$(find_orphan_gateways)
    [ -z "$orphans" ] && return 0

    for opid in $orphans; do
        log_warn "Found orphan gateway process (PID: $opid) — killing it"
        kill "$opid" 2>/dev/null || true
    done
    sleep 1
    # Force kill any survivors
    for opid in $orphans; do
        if kill -0 "$opid" 2>/dev/null; then
            kill -9 "$opid" 2>/dev/null || true
        fi
    done
    return 0
}

# ==============================================================
# Setup: Validate required env vars
# ==============================================================
validate_env() {
    local missing=0
    if [ "${TELEGRAM_ENABLED:-true}" = "true" ] && [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
        log_error "TELEGRAM_BOT_TOKEN is not set (required when TELEGRAM_ENABLED=true)"
        missing=1
    fi
    if [ -z "${CLAUDE_SETUP_TOKEN:-}" ]; then
        log_error "CLAUDE_SETUP_TOKEN is not set (run 'claude setup-token' to generate)"
        missing=1
    fi
    if [ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]; then
        log_error "OPENCLAW_GATEWAY_TOKEN is not set"
        missing=1
    fi
    if [ "$missing" -eq 1 ]; then
        log_error "Set required vars in .env. Exiting."
        exit 1
    fi
    log_success "Required environment variables are set"
}

# ==============================================================
# Setup: Install .env to ~/.openclaw/
# ==============================================================
install_env() {
    log "Installing .env to ${OPENCLAW_DIR}/.env..."
    mkdir -p "$OPENCLAW_DIR"
    if [ -f "$SCRIPT_DIR/.env" ]; then
        cp "$SCRIPT_DIR/.env" "$OPENCLAW_DIR/.env"
        chmod 600 "$OPENCLAW_DIR/.env"
        log_success ".env copied to ${OPENCLAW_DIR}/.env (mode 600)"
    else
        log_error "No .env found in $SCRIPT_DIR"
        exit 1
    fi
}

# ==============================================================
# Setup: Enable channels in openclaw.json (using jq if available)
# ==============================================================
enable_channels() {
    log "Configuring channels..."
    mkdir -p "$OPENCLAW_DIR"

    if [ -f "$CONFIG_FILE" ]; then
        log_warn "openclaw.json already exists, skipping channel config"
        return 0
    fi

    local telegram_enabled="${TELEGRAM_ENABLED:-true}"
    local allow_from="${TELEGRAM_ALLOW_FROM:-}"

    if command -v jq &>/dev/null; then
        # Build config safely with jq
        local config
        config=$(jq -n \
            --argjson tg_enabled "$telegram_enabled" \
            --arg allow_from "$allow_from" \
            '{
                gateway: { mode: "local" },
                commands: { native: "auto", nativeSkills: "auto" },
                channels: {
                    telegram: {
                        enabled: $tg_enabled,
                        dmPolicy: "open",
                        allowFrom: ["*"],
                        groupPolicy: "allowlist",
                        streamMode: "partial"
                    }
                },
                plugins: { entries: { telegram: { enabled: $tg_enabled } } },
                agents: { defaults: { maxConcurrent: 4, subagents: { maxConcurrent: 8 } } },
                messages: { ackReactionScope: "group-mentions" }
            }
            | if $allow_from != "" then
                .channels.telegram.allowFrom = [$allow_from]
              else . end')
        echo "$config" > "$CONFIG_FILE"
    else
        # Fallback: heredoc (no string concatenation)
        cat > "$CONFIG_FILE" <<'EOJSON'
{
  "gateway": { "mode": "local" },
  "commands": { "native": "auto", "nativeSkills": "auto" },
  "channels": {
    "telegram": {
      "enabled": true,
      "dmPolicy": "open",
      "allowFrom": ["*"],
      "groupPolicy": "allowlist",
      "streamMode": "partial"
    }
  },
  "plugins": { "entries": { "telegram": { "enabled": true } } },
  "agents": { "defaults": { "maxConcurrent": 4, "subagents": { "maxConcurrent": 8 } } },
  "messages": { "ackReactionScope": "group-mentions" }
}
EOJSON
        # Patch in allow_from and enabled state if needed
        if [ "$telegram_enabled" = "false" ]; then
            log_warn "jq not available — Telegram enabled defaults to true in fallback config. Install jq for full config support."
        fi
    fi

    log_success "Channels configured (telegram: ${telegram_enabled})"
}

# ==============================================================
# Setup: Ensure gateway.mode is set
# ==============================================================
ensure_gateway_mode() {
    if [ -f "$CONFIG_FILE" ] && ! grep -q '"gateway"' "$CONFIG_FILE"; then
        if command -v jq &>/dev/null; then
            local tmp
            tmp=$(jq '. + {gateway: {mode: "local"}}' "$CONFIG_FILE")
            echo "$tmp" > "$CONFIG_FILE"
        else
            local tmpfile
            tmpfile=$(mktemp)
            sed '1s/{/{\n  "gateway": { "mode": "local" },/' "$CONFIG_FILE" > "$tmpfile" && mv "$tmpfile" "$CONFIG_FILE"
        fi
        log_success "Added gateway.mode=local to config"
    fi
}

# ==============================================================
# Setup: Install OpenClaw CLI
# ==============================================================
install_cli() {
    log "Installing OpenClaw CLI (non-interactive)..."
    export PATH="$HOME/.local/bin:$HOME/.openclaw/bin:$PATH"

    if command -v openclaw &>/dev/null; then
        log_success "OpenClaw CLI already installed: $(which openclaw)"
        return 0
    fi

    export OPENCLAW_NO_ONBOARD=1
    export OPENCLAW_NO_PROMPT=1
    export OPENCLAW_DISABLE_BONJOUR=1

    if curl -fsSL https://openclaw.ai/install.sh | bash; then
        log_success "OpenClaw CLI installed"
    else
        log_error "Failed to install OpenClaw CLI"
        exit 1
    fi

    # Refresh PATH
    export PATH="$HOME/.local/bin:$HOME/.openclaw/bin:$PATH"

    if command -v openclaw &>/dev/null; then
        log_success "OpenClaw CLI available: $(which openclaw)"
    else
        log_error "OpenClaw CLI not found in PATH after install"
        exit 1
    fi
}

# ==============================================================
# Gateway: Check if running
# ==============================================================
is_running() {
    clean_stale_pid
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

# ==============================================================
# Gateway: Start (background with auto-restart + signal traps)
# ==============================================================
start_gateway() {
    acquire_lock

    if is_running; then
        echo -e "${YELLOW}Gateway is already running (PID: $(cat "$PID_FILE"))${NC}"
        return 0
    fi

    # Kill any orphan gateway processes (e.g. user ran "openclaw gateway run" directly)
    kill_orphan_gateways

    rotate_log
    echo -e "${GREEN}Starting OpenClaw Gateway...${NC}"

    nohup bash -c '
        pid_file="'"$PID_FILE"'"
        log_file="'"$LOG_FILE"'"
        log_max_size='"$LOG_MAX_SIZE"'

        # Cleanup on exit — remove PID file and kill child
        gateway_pid=""
        cleanup() {
            echo "[$(date "+%Y-%m-%d %H:%M:%S")] Shutting down (signal received)..."
            if [ -n "$gateway_pid" ] && kill -0 "$gateway_pid" 2>/dev/null; then
                kill "$gateway_pid" 2>/dev/null
                wait "$gateway_pid" 2>/dev/null
            fi
            rm -f "$pid_file"
            exit 0
        }
        trap cleanup SIGTERM SIGINT SIGHUP EXIT

        restart_count=0
        max_restarts='"$MAX_RESTARTS"'
        restart_delay='"$RESTART_DELAY"'
        restart_window='"$RESTART_WINDOW"'

        while true; do
            # Rotate log inline
            if [ -f "$log_file" ]; then
                size=$(stat -c%s "$log_file" 2>/dev/null || stat -f%z "$log_file" 2>/dev/null || echo 0)
                if [ "$size" -ge "$log_max_size" ]; then
                    mv "$log_file" "${log_file}.1"
                    echo "[$(date "+%Y-%m-%d %H:%M:%S")] Log rotated" >> "$log_file"
                fi
            fi

            start_time=$(date +%s)
            echo "[$(date "+%Y-%m-%d %H:%M:%S")] Starting gateway (attempt $((restart_count + 1)))..."

            openclaw gateway run 2>&1 &
            gateway_pid=$!
            wait "$gateway_pid"
            exit_code=$?
            gateway_pid=""

            end_time=$(date +%s)
            uptime=$((end_time - start_time))
            echo "[$(date "+%Y-%m-%d %H:%M:%S")] Gateway exited with code $exit_code after ${uptime}s"

            if [ "$uptime" -ge "$restart_window" ]; then
                restart_count=0
            fi

            restart_count=$((restart_count + 1))
            if [ "$restart_count" -ge "$max_restarts" ]; then
                echo "[$(date "+%Y-%m-%d %H:%M:%S")] ERROR: Max restarts ($max_restarts) reached. Giving up."
                rm -f "$pid_file"
                exit 1
            fi

            echo "[$(date "+%Y-%m-%d %H:%M:%S")] Restarting in ${restart_delay}s... ($restart_count/$max_restarts)"
            sleep "$restart_delay"
        done
    ' >> "$LOG_FILE" 2>&1 &

    local pid=$!
    echo "$pid" > "$PID_FILE"
    echo -e "${GREEN}Gateway auto-runner started (PID: $pid)${NC}"
    echo -e "Logs: ${YELLOW}$LOG_FILE${NC}"
}

# ==============================================================
# Gateway: Stop (targeted, no greedy pkill)
# ==============================================================
stop_gateway() {
    acquire_lock

    if ! is_running; then
        # Even if our managed wrapper isn't running, kill orphan processes
        local orphans
        orphans=$(find_orphan_gateways)
        if [ -n "$orphans" ]; then
            log_warn "No managed gateway, but found orphan process(es) — cleaning up"
            kill_orphan_gateways
            echo -e "${GREEN}Stopped orphan gateway process(es).${NC}"
        else
            echo -e "${YELLOW}Gateway is not running.${NC}"
        fi
        return 0
    fi

    local pid
    pid=$(cat "$PID_FILE")
    echo -e "${RED}Stopping gateway (PID: $pid)...${NC}"

    # Send SIGTERM to the wrapper — trap will clean up the child
    kill "$pid" 2>/dev/null

    # Wait up to 10s for graceful shutdown
    for i in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done

    # Force kill if still alive
    if kill -0 "$pid" 2>/dev/null; then
        log_warn "Graceful shutdown failed, force killing..."
        kill -9 "$pid" 2>/dev/null
        # Also kill any orphaned child
        pkill -9 -P "$pid" 2>/dev/null || true
    fi

    rm -f "$PID_FILE"

    # Also kill any orphan gateway processes
    kill_orphan_gateways

    echo -e "${GREEN}Stopped.${NC}"
}

# ==============================================================
# Gateway: Status
# ==============================================================
status_gateway() {
    if is_running; then
        local pid
        pid=$(cat "$PID_FILE")
        echo -e "${GREEN}Gateway auto-runner is running (PID: $pid)${NC}"
        openclaw gateway status 2>/dev/null || openclaw status 2>/dev/null || true
    else
        echo -e "${RED}Gateway auto-runner is not running.${NC}"
    fi

    # Warn about orphan processes
    local orphans
    orphans=$(find_orphan_gateways)
    if [ -n "$orphans" ]; then
        echo -e "${YELLOW}⚠ WARNING: Found unmanaged gateway process(es): ${orphans}${NC}"
        echo -e "${YELLOW}  These were likely started with 'openclaw gateway run' directly.${NC}"
        echo -e "${YELLOW}  Run '$0 stop' to clean them up, or '$0 restart' to take over.${NC}"
    fi
}

# ==============================================================
# Gateway: Logs
# ==============================================================
show_logs() {
    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE"
    else
        echo "No log file found at $LOG_FILE"
    fi
}

# ==============================================================
# Setup: Install gateway guard (prevent direct "openclaw gateway run")
# ==============================================================
install_gateway_guard() {
    local guard_file="/etc/profile.d/openclaw-guard.sh"
    local setup_dir="$SCRIPT_DIR"

    log "Installing gateway guard..."
    sudo tee "$guard_file" > /dev/null <<GUARDEOF
# Intercept "openclaw gateway run" to prevent unmanaged gateway processes.
# All gateway lifecycle should go through openclaw.sh (start/stop/restart).
openclaw() {
    if [ "\$1" = "gateway" ] && [ "\${2:-}" = "run" ]; then
        echo "⚠  Do not run 'openclaw gateway run' directly."
        echo "   Use the managed gateway instead:"
        echo ""
        echo "     ${setup_dir}/openclaw.sh start    # start gateway"
        echo "     ${setup_dir}/openclaw.sh stop     # stop gateway"
        echo "     ${setup_dir}/openclaw.sh restart  # restart gateway"
        echo "     ${setup_dir}/openclaw.sh status   # check status"
        echo "     ${setup_dir}/openclaw.sh logs     # tail logs"
        echo ""
        echo "   This ensures PID tracking, auto-restart, and log rotation."
        return 1
    fi
    command openclaw "\$@"
}
GUARDEOF
    sudo chmod +x "$guard_file"
    log_success "Gateway guard installed (${guard_file})"
}

# ==============================================================
# Setup: Register Claude setup-token (if provided)
# ==============================================================
register_claude_token() {
    log "Registering Claude setup-token with OpenClaw..."
    if echo "$CLAUDE_SETUP_TOKEN" | openclaw models auth paste-token --provider anthropic 2>/dev/null; then
        log_success "Claude setup-token registered"
    else
        log_error "Failed to register Claude setup-token"
        exit 1
    fi
}

# ==============================================================
# Setup: Full setup + start
# ==============================================================
run_setup() {
    log "OpenClaw OneClick Setup"
    log "======================"
    validate_env
    install_env
    enable_channels
    install_cli
    register_claude_token
    log "Running config doctor..."
    if openclaw doctor --fix --yes 2>/dev/null; then
        log_success "Config validated"
    else
        log_warn "openclaw doctor not available or config needs manual review"
    fi
    ensure_gateway_mode
    install_gateway_guard
    start_gateway
}

# ==============================================================
# Main
# ==============================================================
case "${1:-setup}" in
    setup)   run_setup ;;
    start)   start_gateway ;;
    stop)    stop_gateway ;;
    restart) stop_gateway; sleep 2; start_gateway ;;
    status)  status_gateway ;;
    logs)    show_logs ;;
    *)       echo "Usage: $0 {setup|start|stop|restart|status|logs}"; exit 1 ;;
esac