#!/usr/bin/env bash
# ==============================================================
# Hermes OneClick Setup & Gateway Manager
# Combined setup + gateway management in a single script.
# Modeled after openclaw.sh but targeting Hermes Agent.
#
# Usage: ./hermes.sh {setup|start|stop|restart|status|logs}
#
# Required env vars (set in .env):
#     ANTHROPIC_API_KEY       - Anthropic API key for Claude model
#       — or —
#     OPENAI_API_KEY          - OpenAI API key
#       — or —
#     OPENROUTER_API_KEY      - OpenRouter API key
#
# Channel tokens (optional — depends on enabled channels):
#     TELEGRAM_BOT_TOKEN      - Telegram bot token from BotFather
#     SLACK_BOT_TOKEN         - Slack Bot User OAuth Token (xoxb-...)
#     SLACK_APP_TOKEN         - Slack App-Level Token (xapp-...)
#
# Optional env vars:
#     TELEGRAM_ENABLED        - enable/disable Telegram channel (default: true)
#     HERMES_MAX_RESTARTS     - max consecutive restarts (default: 10)
#     HERMES_RESTART_DELAY    - seconds between restarts (default: 5)
#     HERMES_RESTART_WINDOW   - seconds of uptime to reset counter (default: 300)
#     HERMES_LOG_MAX_SIZE     - max log size in bytes before rotation (default: 10485760 / 10MB)
# ==============================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_DIR="${HOME}/.hermes"
HERMES_REPO="${HERMES_DIR}/hermes-agent"
CONFIG_FILE="${HERMES_DIR}/config.yaml"
ENV_FILE="${HERMES_DIR}/.env"
AUTH_FILE="${HERMES_DIR}/auth.json"

# Gateway (API) process management
GW_PID_FILE="/tmp/hermes-gateway.pid"
GW_LOG_FILE="/tmp/hermes-gateway.log"
GW_LOCK_FILE="/tmp/hermes-gateway.lock"

# Dashboard process management
DASH_PID_FILE="/tmp/hermes-dashboard.pid"
DASH_LOG_FILE="/tmp/hermes-dashboard.log"

# Ports
HERMES_API_PORT=8642
HERMES_DASH_PORT=9119

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()         { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

# ==============================================================
# Load .env safely (don't fail on unset vars)
# ==============================================================
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
load_env "$ENV_FILE"

# --- Settings ---
RESTART_DELAY="${HERMES_RESTART_DELAY:-5}"
MAX_RESTARTS="${HERMES_MAX_RESTARTS:-10}"
RESTART_WINDOW="${HERMES_RESTART_WINDOW:-300}"
LOG_MAX_SIZE="${HERMES_LOG_MAX_SIZE:-10485760}"

# ==============================================================
# Helpers
# ==============================================================

rotate_log() {
    local lf="$1"
    if [ -f "$lf" ]; then
        local size
        size=$(stat -c%s "$lf" 2>/dev/null || stat -f%z "$lf" 2>/dev/null || echo 0)
        if [ "$size" -ge "$LOG_MAX_SIZE" ]; then
            mv "$lf" "${lf}.1"
            log "Log rotated: ${lf} (was ${size} bytes)"
        fi
    fi
}

LOCK_ACQUIRED=0
acquire_lock() {
    if [ "$LOCK_ACQUIRED" -eq 1 ]; then
        return 0
    fi
    exec 9>"$GW_LOCK_FILE"
    if ! flock -w 5 9; then
        log_warn "Lock held, cleaning stale lock and retrying..."
        rm -f "$GW_LOCK_FILE"
        exec 9>"$GW_LOCK_FILE"
        if ! flock -n 9; then
            log_error "Another hermes.sh operation is in progress"
            exit 1
        fi
    fi
    LOCK_ACQUIRED=1
}

clean_stale_pid() {
    local pf="$1"
    if [ -f "$pf" ]; then
        local pid
        pid=$(cat "$pf")
        if ! kill -0 "$pid" 2>/dev/null; then
            log_warn "Removing stale PID file (process $pid is dead)"
            rm -f "$pf"
        fi
    fi
}

# Find orphan hermes gateway processes not managed by our wrapper
find_orphan_gateways() {
    local managed_wrapper_pid=""
    if [ -f "$GW_PID_FILE" ]; then
        managed_wrapper_pid=$(cat "$GW_PID_FILE" 2>/dev/null || true)
    fi

    local gw_pids
    gw_pids=$(( pgrep -f "hermes gateway run" 2>/dev/null; pgrep -f "hermes gateway start" 2>/dev/null ) | sort -u || true)
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

kill_orphan_gateways() {
    local orphans
    orphans=$(find_orphan_gateways)
    [ -z "$orphans" ] && return 0

    for opid in $orphans; do
        log_warn "Found orphan gateway process (PID: $opid) — killing it"
        kill "$opid" 2>/dev/null || true
    done
    sleep 1
    for opid in $orphans; do
        if kill -0 "$opid" 2>/dev/null; then
            kill -9 "$opid" 2>/dev/null || true
        fi
    done
    return 0
}

# Wait for a port to be released
wait_for_port_release() {
    local port="$1"
    local max_wait="${2:-5}"
    for i in $(seq 1 "$max_wait"); do
        if ! ss -tlnp 2>/dev/null | grep -q ":${port} " && \
           ! netstat -tlnp 2>/dev/null | grep -q ":${port} "; then
            return 0
        fi
        sleep 1
    done
    log_warn "Port $port still in use after ${max_wait}s"
}

# ==============================================================
# Setup: Validate required env vars
# ==============================================================
validate_env() {
    local missing=0

    # At least one model provider key required
    if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${OPENROUTER_API_KEY:-}" ]; then
        log_error "No model provider key set. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or OPENROUTER_API_KEY."
        missing=1
    fi

    # Channel-specific validation
    if [ "${TELEGRAM_ENABLED:-true}" = "true" ] && [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
        log_warn "TELEGRAM_BOT_TOKEN is not set (Telegram will not be available)"
    fi

    if [ "${SLACK_ENABLED:-false}" = "true" ]; then
        if [ -z "${SLACK_BOT_TOKEN:-}" ] || [ -z "${SLACK_APP_TOKEN:-}" ]; then
            log_warn "SLACK_BOT_TOKEN or SLACK_APP_TOKEN not set (Slack will not be available)"
        fi
    fi

    if [ "$missing" -eq 1 ]; then
        log_error "Set required vars in .env. Exiting."
        exit 1
    fi
    log_success "Required environment variables are set"
}

# ==============================================================
# Setup: Install .env to ~/.hermes/
# ==============================================================
install_env() {
    log "Installing .env to ${HERMES_DIR}/.env..."
    mkdir -p "$HERMES_DIR"
    if [ -f "$SCRIPT_DIR/.env" ]; then
        cp "$SCRIPT_DIR/.env" "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        log_success ".env copied to ${ENV_FILE} (mode 600)"
    else
        log_warn "No .env found in $SCRIPT_DIR — Hermes will use existing config or defaults"
    fi
}

# ==============================================================
# Setup: Install Hermes CLI
# ==============================================================
install_cli() {
    log "Installing Hermes Agent..."
    export PATH="$HOME/.local/bin:$HERMES_DIR/hermes-agent/venv/bin:$PATH"

    if command -v hermes &>/dev/null; then
        log_success "Hermes CLI already installed: $(which hermes)"
        return 0
    fi

    # Clone repo if not present
    if [ ! -d "$HERMES_REPO" ]; then
        log "Cloning hermes-agent repo..."
        mkdir -p "$HERMES_DIR"
        if git clone https://github.com/NousResearch/hermes-agent.git "$HERMES_REPO"; then
            log_success "Hermes repo cloned"
        else
            log_error "Failed to clone hermes-agent repo"
            exit 1
        fi
    else
        log "Hermes repo exists, pulling latest..."
        cd "$HERMES_REPO" && git pull --ff-only || true
        cd "$HOME"
    fi

    # Set up Python venv and install
    cd "$HERMES_REPO"

    if command -v uv &>/dev/null; then
        log "Installing with uv..."
        if [ ! -d "venv" ]; then
            uv venv venv
        fi
        # Try full install, fall back to base
        uv pip install -e ".[all]" 2>/dev/null || uv pip install -e . || {
            log_error "Failed to install hermes-agent with uv"
            exit 1
        }
    else
        log "Installing with pip (uv not found)..."
        if [ ! -d "venv" ]; then
            python3 -m venv venv
        fi
        source venv/bin/activate
        pip install --upgrade pip setuptools wheel
        pip install -e ".[all]" 2>/dev/null || pip install -e . || {
            log_error "Failed to install hermes-agent with pip"
            exit 1
        }
    fi

    # Create symlink so hermes is on PATH
    local bin_dir="$HOME/.local/bin"
    mkdir -p "$bin_dir"
    if [ -f "$HERMES_REPO/venv/bin/hermes" ] && [ ! -L "$bin_dir/hermes" ]; then
        ln -sf "$HERMES_REPO/venv/bin/hermes" "$bin_dir/hermes"
    fi

    export PATH="$bin_dir:$HERMES_REPO/venv/bin:$PATH"
    cd "$HOME"

    if command -v hermes &>/dev/null; then
        log_success "Hermes CLI available: $(which hermes)"
    else
        log_error "Hermes CLI not found in PATH after install"
        exit 1
    fi
}

# ==============================================================
# Setup: Configure Hermes via hermes config set
# ==============================================================
configure_hermes() {
    log "Configuring Hermes..."
    mkdir -p "$HERMES_DIR/skills" "$HERMES_DIR/sessions" "$HERMES_DIR/memories" \
             "$HERMES_DIR/cron" "$HERMES_DIR/logs" "$HERMES_DIR/hooks"

    # ── Model ──────────────────────────────────────────────────────────────────
    # HERMES_PROVIDER (from Coder form) selects which key to use when multiple are set.
    # Normalise: "openai" → "custom" (Hermes has no standalone openai provider).
    local _provider="${HERMES_PROVIDER:-}"
    [ "$_provider" = "openai" ] && _provider="custom"

    _configure_model() {
        local provider="$1" key_var="$2" default_model="$3" base_url="$4"
        local api_key="${!key_var:-}"
        [ -z "$api_key" ] && { log_warn "Provider $provider selected but $key_var is not set — skipping"; return 1; }
        local model="${HERMES_MODEL:-$default_model}"
        log "Configuring model: $provider / $model"
        hermes config set "$key_var" "$api_key"
        hermes config set model.provider "$provider"
        hermes config set model.default "$model"
        [ -n "$base_url" ] && hermes config set model.base_url "$base_url"
        log_success "Model: $provider / $model"
    }

    case "$_provider" in
        anthropic)
            _configure_model anthropic ANTHROPIC_API_KEY claude-opus-4-6 https://api.anthropic.com ;;
        custom)
            _configure_model custom OPENAI_API_KEY gpt-5.4 https://api.openai.com/v1 ;;
        openrouter)
            _configure_model openrouter OPENROUTER_API_KEY anthropic/claude-sonnet-4 "" ;;
        *)
            # Auto-detect from whichever key is set (anthropic wins if multiple)
            if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
                _configure_model anthropic ANTHROPIC_API_KEY claude-opus-4-6 https://api.anthropic.com
            elif [ -n "${OPENAI_API_KEY:-}" ]; then
                _configure_model custom OPENAI_API_KEY gpt-5.4 https://api.openai.com/v1
            elif [ -n "${OPENROUTER_API_KEY:-}" ]; then
                _configure_model openrouter OPENROUTER_API_KEY anthropic/claude-sonnet-4 ""
            fi
            ;;
    esac

    # ── Channels ───────────────────────────────────────────────────────────────
    # Parse ENABLED_CHANNELS JSON array if set (from Coder multi-select)
    local telegram_enabled="${TELEGRAM_ENABLED:-true}"
    local whatsapp_enabled="${WHATSAPP_ENABLED:-false}"
    local slack_enabled="${SLACK_ENABLED:-false}"
    if [ -n "${ENABLED_CHANNELS:-}" ]; then
        echo "$ENABLED_CHANNELS" | grep -q '"telegram"' && telegram_enabled=true || telegram_enabled=false
        echo "$ENABLED_CHANNELS" | grep -q '"whatsapp"' && whatsapp_enabled=true || whatsapp_enabled=false
        echo "$ENABLED_CHANNELS" | grep -q '"slack"'    && slack_enabled=true    || slack_enabled=false
    fi
    # Auto-enable Slack if both tokens are present
    [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_APP_TOKEN:-}" ] && slack_enabled=true

    # Telegram
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ "$telegram_enabled" = "true" ]; then
        log "Configuring Telegram..."
        hermes config set TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
        hermes config set TELEGRAM_ALLOWED_USERS "${TELEGRAM_ALLOWED_USERS:-*}"
        hermes config set GATEWAY_ALLOW_ALL_USERS true
        log_success "Telegram configured"
    fi

    # Slack
    if [ "$slack_enabled" = "true" ] && [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_APP_TOKEN:-}" ]; then
        log "Configuring Slack..."
        hermes config set SLACK_BOT_TOKEN "$SLACK_BOT_TOKEN"
        hermes config set SLACK_APP_TOKEN "$SLACK_APP_TOKEN"
        hermes config set SLACK_ALLOWED_USERS "${SLACK_ALLOWED_USERS:-*}"
        log_success "Slack configured"
    fi

    # WhatsApp
    if [ "$whatsapp_enabled" = "true" ] || [ -n "${WHATSAPP_CREDS:-}" ]; then
        log "Configuring WhatsApp..."
        hermes config set WHATSAPP_ENABLED true
        hermes config set WHATSAPP_MODE "${WHATSAPP_MODE:-bot}"
        hermes config set WHATSAPP_ALLOWED_USERS "${WHATSAPP_ALLOWED_USERS:-*}"
        if [ -n "${WHATSAPP_CREDS:-}" ]; then
            local wa_dir="$HERMES_DIR/whatsapp/session"
            mkdir -p "$wa_dir"
            echo "$WHATSAPP_CREDS" > "$wa_dir/creds.json"
            chmod 600 "$wa_dir/creds.json"
            log_success "WhatsApp credentials written to $wa_dir/creds.json"
        fi
        log_success "WhatsApp configured"
    fi

    # ── auth.json (credential pool) ────────────────────────────────────────────
    if [ ! -f "$AUTH_FILE" ]; then
        log "Building auth.json credential pool..."
        _build_auth_json
        log_success "auth.json written"
    else
        log "auth.json already exists — skipping"
    fi

    # ── Skills ────────────────────────────────────────────────────────────────
    if [ -d "$HERMES_REPO/skills" ] && command -v hermes &>/dev/null; then
        hermes skills sync 2>/dev/null || {
            rsync -a --ignore-existing "$HERMES_REPO/skills/" "$HERMES_DIR/skills/" 2>/dev/null || \
            cp -rn "$HERMES_REPO/skills/"* "$HERMES_DIR/skills/" 2>/dev/null || true
        }
        log_success "Skills synced"
    fi
}

# Build auth.json from available API keys
_build_auth_json() {
    local entries=()

    if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        local aid
        aid=$(head -c 6 /dev/urandom | xxd -p 2>/dev/null || echo "a1b2c3")
        entries+=("$(cat <<ENTRY
    "anthropic": [
      {
        "id": "${aid}",
        "label": "coder-param",
        "auth_type": "api_key",
        "priority": 0,
        "source": "coder_parameter",
        "api_key": "${ANTHROPIC_API_KEY}",
        "last_status": null,
        "request_count": 0
      }
    ]
ENTRY
        )")
    fi

    if [ -n "${OPENROUTER_API_KEY:-}" ]; then
        local oid
        oid=$(head -c 6 /dev/urandom | xxd -p 2>/dev/null || echo "d4e5f6")
        entries+=("$(cat <<ENTRY
    "openrouter": [
      {
        "id": "${oid}",
        "label": "coder-param",
        "auth_type": "api_key",
        "priority": 0,
        "source": "coder_parameter",
        "api_key": "${OPENROUTER_API_KEY}",
        "last_status": null,
        "request_count": 0
      }
    ]
ENTRY
        )")
    fi

    if [ -n "${OPENAI_API_KEY:-}" ]; then
        local iid
        iid=$(head -c 6 /dev/urandom | xxd -p 2>/dev/null || echo "789abc")
        entries+=("$(cat <<ENTRY
    "openai": [
      {
        "id": "${iid}",
        "label": "coder-param",
        "auth_type": "api_key",
        "priority": 0,
        "source": "coder_parameter",
        "api_key": "${OPENAI_API_KEY}",
        "last_status": null,
        "request_count": 0
      }
    ]
ENTRY
        )")
    fi

    # Join entries with commas
    local pool=""
    local first=true
    for entry in "${entries[@]}"; do
        if [ "$first" = true ]; then
            pool="$entry"
            first=false
        else
            pool="${pool},"$'\n'"${entry}"
        fi
    done

    cat > "$AUTH_FILE" <<AUTHEOF
{
  "version": 1,
  "credential_pool": {
${pool}
  }
}
AUTHEOF
    chmod 600 "$AUTH_FILE"
}

# ==============================================================
# Setup: Install gateway guard (prevent direct "hermes gateway start")
# ==============================================================
install_gateway_guard() {
    local guard_file="/etc/profile.d/hermes-guard.sh"
    local setup_dir="$SCRIPT_DIR"

    log "Installing gateway guard..."
    sudo tee "$guard_file" > /dev/null <<GUARDEOF
# Intercept "hermes gateway start/run" to prevent unmanaged gateway processes.
# All gateway lifecycle should go through hermes.sh (start/stop/restart).
hermes() {
    if [ "\$1" = "gateway" ] && { [ "\${2:-}" = "start" ] || [ "\${2:-}" = "run" ] || [ "\${2:-}" = "restart" ]; }; then
        echo "⚠  Do not run 'hermes gateway $2' directly (no systemd in this container)."
        echo "   Use the managed gateway instead:"
        echo ""
        echo "     ${setup_dir}/hermes.sh start    # start gateway + dashboard"
        echo "     ${setup_dir}/hermes.sh stop     # stop everything"
        echo "     ${setup_dir}/hermes.sh restart  # restart everything"
        echo "     ${setup_dir}/hermes.sh status   # check status"
        echo "     ${setup_dir}/hermes.sh logs     # tail gateway logs"
        echo ""
        echo "   This ensures PID tracking, auto-restart, and log rotation."
        return 1
    fi
    command hermes "\$@"
}
GUARDEOF
    sudo chmod +x "$guard_file"
    log_success "Gateway guard installed (${guard_file})"
}

# ==============================================================
# Gateway: Check if running
# ==============================================================
is_gateway_running() {
    clean_stale_pid "$GW_PID_FILE"
    if [ -f "$GW_PID_FILE" ]; then
        local pid
        pid=$(cat "$GW_PID_FILE" 2>/dev/null)
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
    else
        return 1
    fi
}

is_dashboard_running() {
    clean_stale_pid "$DASH_PID_FILE"
    if [ -f "$DASH_PID_FILE" ]; then
        local pid
        pid=$(cat "$DASH_PID_FILE" 2>/dev/null)
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
    else
        return 1
    fi
}

# ==============================================================
# Gateway: Launch background auto-restart loop
# ==============================================================
_launch_gateway_loop() {
    rotate_log "$GW_LOG_FILE"
    echo -e "${GREEN}Starting Hermes Gateway (API on port ${HERMES_API_PORT})...${NC}"

    # Ensure hermes is on PATH inside the subshell
    local hermes_bin
    hermes_bin=$(which hermes 2>/dev/null || echo "$HOME/.local/bin/hermes")

    nohup bash -c '
        exec 9>&-

        pid_file="'"$GW_PID_FILE"'"
        log_file="'"$GW_LOG_FILE"'"
        log_max_size='"$LOG_MAX_SIZE"'
        hermes_bin="'"$hermes_bin"'"
        hermes_dir="'"$HERMES_DIR"'"

        gateway_pid=""
        cleanup() {
            echo "[$(date "+%Y-%m-%d %H:%M:%S")] Shutting down gateway (signal received)..."
            if [ -n "$gateway_pid" ] && kill -0 "$gateway_pid" 2>/dev/null; then
                pkill -P "$gateway_pid" 2>/dev/null || true
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
            if [ -f "$log_file" ]; then
                size=$(stat -c%s "$log_file" 2>/dev/null || stat -f%z "$log_file" 2>/dev/null || echo 0)
                if [ "$size" -ge "$log_max_size" ]; then
                    mv "$log_file" "${log_file}.1"
                    echo "[$(date "+%Y-%m-%d %H:%M:%S")] Log rotated" >> "$log_file"
                fi
            fi

            start_time=$(date +%s)
            echo "[$(date "+%Y-%m-%d %H:%M:%S")] Starting gateway (attempt $((restart_count + 1)))..."

            cd "$hermes_dir"
            # Use "gateway run" (foreground) instead of "gateway start" (systemd).
            # No systemd/loginctl in this container — hermes.sh manages the lifecycle.
            "$hermes_bin" gateway run 2>&1 &
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
    ' >> "$GW_LOG_FILE" 2>&1 &

    local pid=$!
    echo "$pid" > "$GW_PID_FILE"
    echo -e "${GREEN}Gateway running (PID: $pid, port: ${HERMES_API_PORT})${NC}"
    echo -e "Logs: ${YELLOW}$GW_LOG_FILE${NC}"
}

# ==============================================================
# Dashboard: Launch background process
# ==============================================================
_launch_dashboard() {
    rotate_log "$DASH_LOG_FILE"
    echo -e "${GREEN}Starting Hermes Dashboard (port ${HERMES_DASH_PORT})...${NC}"

    local hermes_bin
    hermes_bin=$(which hermes 2>/dev/null || echo "$HOME/.local/bin/hermes")

    nohup bash -c '
        hermes_bin="'"$hermes_bin"'"
        hermes_dir="'"$HERMES_DIR"'"
        cd "$hermes_dir"
        "$hermes_bin" dashboard --host 0.0.0.0 --port '"$HERMES_DASH_PORT"' 2>&1
    ' >> "$DASH_LOG_FILE" 2>&1 &

    local pid=$!
    echo "$pid" > "$DASH_PID_FILE"
    echo -e "${GREEN}Dashboard running (PID: $pid, port: ${HERMES_DASH_PORT})${NC}"
    echo -e "Logs: ${YELLOW}$DASH_LOG_FILE${NC}"
}

# ==============================================================
# Start: Gateway + Dashboard
# ==============================================================
start_all() {
    acquire_lock

    if is_gateway_running; then
        echo -e "${YELLOW}Gateway is already running (PID: $(cat "$GW_PID_FILE"))${NC}"
    else
        kill_orphan_gateways
        _launch_gateway_loop
    fi

    if is_dashboard_running; then
        echo -e "${YELLOW}Dashboard is already running (PID: $(cat "$DASH_PID_FILE"))${NC}"
    else
        _launch_dashboard
    fi
}

# ==============================================================
# Stop: Gateway + Dashboard
# ==============================================================
_stop_process() {
    local name="$1"
    local pf="$2"
    local port="$3"

    clean_stale_pid "$pf"
    if [ ! -f "$pf" ]; then
        echo -e "${YELLOW}${name} is not running.${NC}"
        return 0
    fi

    local pid
    pid=$(cat "$pf")
    echo -e "${RED}Stopping ${name} (PID: $pid)...${NC}"

    kill "$pid" 2>/dev/null

    # Wait up to 10s for graceful shutdown
    for i in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done

    # Force kill if still alive
    if kill -0 "$pid" 2>/dev/null; then
        log_warn "Graceful shutdown failed for ${name}, force killing..."
        kill -9 "$pid" 2>/dev/null
        pkill -9 -P "$pid" 2>/dev/null || true
    fi

    rm -f "$pf"

    if [ -n "$port" ]; then
        wait_for_port_release "$port"
    fi

    echo -e "${GREEN}${name} stopped.${NC}"
}

stop_all() {
    acquire_lock

    _stop_process "Hermes Gateway" "$GW_PID_FILE" "$HERMES_API_PORT"
    _stop_process "Hermes Dashboard" "$DASH_PID_FILE" "$HERMES_DASH_PORT"

    # Kill any orphan gateway processes
    kill_orphan_gateways
}

# ==============================================================
# Restart
# ==============================================================
restart_all() {
    stop_all
    sleep 1
    start_all
}

# ==============================================================
# Status
# ==============================================================
status_all() {
    echo -e "${CYAN}═══════════════════════════════════════${NC}"
    echo -e "${CYAN}  Hermes Agent Status${NC}"
    echo -e "${CYAN}═══════════════════════════════════════${NC}"

    if is_gateway_running; then
        local gw_pid
        gw_pid=$(cat "$GW_PID_FILE")
        echo -e "  Gateway:   ${GREEN}Up${NC} (PID: $gw_pid, port: ${HERMES_API_PORT})"
    else
        echo -e "  Gateway:   ${RED}Down${NC}"
    fi

    if is_dashboard_running; then
        local dash_pid
        dash_pid=$(cat "$DASH_PID_FILE")
        echo -e "  Dashboard: ${GREEN}Up${NC} (PID: $dash_pid, port: ${HERMES_DASH_PORT})"
    else
        echo -e "  Dashboard: ${RED}Down${NC}"
    fi

    # Check hermes doctor if available
    if command -v hermes &>/dev/null; then
        echo ""
        hermes gateway status 2>/dev/null || true
    fi

    # Warn about orphan processes
    local orphans
    orphans=$(find_orphan_gateways)
    if [ -n "$orphans" ]; then
        echo ""
        echo -e "${YELLOW}⚠ WARNING: Found unmanaged gateway process(es): ${orphans}${NC}"
        echo -e "${YELLOW}  Run '$0 stop' to clean them up, or '$0 restart' to take over.${NC}"
    fi

    echo -e "${CYAN}═══════════════════════════════════════${NC}"
}

# ==============================================================
# Logs
# ==============================================================
show_logs() {
    local target="${2:-gateway}"
    case "$target" in
        gateway|gw)
            if [ -f "$GW_LOG_FILE" ]; then
                tail -f "$GW_LOG_FILE"
            else
                echo "No gateway log file found at $GW_LOG_FILE"
            fi
            ;;
        dashboard|dash)
            if [ -f "$DASH_LOG_FILE" ]; then
                tail -f "$DASH_LOG_FILE"
            else
                echo "No dashboard log file found at $DASH_LOG_FILE"
            fi
            ;;
        all)
            tail -f "$GW_LOG_FILE" "$DASH_LOG_FILE" 2>/dev/null
            ;;
        *)
            echo "Usage: $0 logs {gateway|dashboard|all}"
            ;;
    esac
}

# ==============================================================
# Setup: Full setup + start
# ==============================================================
run_setup() {
    log "Hermes OneClick Setup"
    log "====================="
    validate_env
    install_env
    install_cli

    # Reload shell so hermes CLI is on PATH before config commands run
    set +u
    [ -f "$HOME/.zshrc" ]  && source "$HOME/.zshrc"  2>/dev/null || true
    [ -f "$HOME/.bashrc" ] && source "$HOME/.bashrc" 2>/dev/null || true
    set -u
    export PATH="$HOME/.local/bin:$HERMES_DIR/hermes-agent/venv/bin:$PATH"

    configure_hermes

    # Run hermes doctor for health check
    log "Running health check..."
    if command -v hermes &>/dev/null; then
        hermes doctor 2>/dev/null && log_success "Health check passed" || \
            log_warn "hermes doctor reported issues (may need manual review)"
    fi

    install_gateway_guard
    start_all

    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Hermes Agent is ready!${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
    echo -e "  API:       ${CYAN}http://localhost:${HERMES_API_PORT}${NC}"
    echo -e "  Dashboard: ${CYAN}http://localhost:${HERMES_DASH_PORT}${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
}

# ==============================================================
# Main
# ==============================================================
case "${1:-setup}" in
    setup)   run_setup ;;
    start)   start_all ;;
    stop)    stop_all ;;
    restart) restart_all ;;
    status)  status_all ;;
    logs)    show_logs "$@" ;;
    *)       echo "Usage: $0 {setup|start|stop|restart|status|logs}"; exit 1 ;;
esac
