#!/usr/bin/env bash
# XO Cowork API process manager
# Usage: ./cowork-api.sh {start|stop|restart|status|logs}

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="/tmp/xo-cowork-api.pid"
LOG_FILE="/tmp/xo-cowork-api.log"
LOCK_FILE="/tmp/xo-cowork-api.lock"
LOCK_PID_FILE="${LOCK_FILE}/pid"
PORT="${PORT:-5002}"
HOST="${HOST:-0.0.0.0}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()         { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

LOCK_ACQUIRED=0
cleanup_lock() {
    if [ "$LOCK_ACQUIRED" -eq 1 ]; then
        rm -f "$LOCK_PID_FILE" 2>/dev/null || true
        rmdir "$LOCK_FILE" 2>/dev/null || true
        LOCK_ACQUIRED=0
    fi
}

acquire_lock() {
    if [ "$LOCK_ACQUIRED" -eq 1 ]; then
        return 0
    fi

    # Backward compatibility: older versions used LOCK_FILE as a plain file (flock).
    # If that legacy file exists, remove it so directory-based locking can work.
    if [ -e "$LOCK_FILE" ] && [ ! -d "$LOCK_FILE" ]; then
        rm -f "$LOCK_FILE" 2>/dev/null || true
    fi

    # Portable lock: atomic mkdir works on both macOS and Linux.
    if mkdir "$LOCK_FILE" 2>/dev/null; then
        echo "$$" > "$LOCK_PID_FILE"
        LOCK_ACQUIRED=1
        trap cleanup_lock EXIT
        return 0
    fi

    # If lock exists, check whether owner is stale.
    if [ -f "$LOCK_PID_FILE" ]; then
        local lock_pid
        lock_pid=$(cat "$LOCK_PID_FILE" 2>/dev/null || true)
        if [ -n "$lock_pid" ] && ! kill -0 "$lock_pid" 2>/dev/null; then
            log_warn "Removing stale lock held by dead PID: $lock_pid"
            rm -f "$LOCK_PID_FILE" 2>/dev/null || true
            rmdir "$LOCK_FILE" 2>/dev/null || true
            if mkdir "$LOCK_FILE" 2>/dev/null; then
                echo "$$" > "$LOCK_PID_FILE"
                LOCK_ACQUIRED=1
                trap cleanup_lock EXIT
                return 0
            fi
        fi
    fi

    log_error "Another cowork-api.sh operation is in progress"
    exit 1
}

clean_stale_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null || true)
        if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
            log_warn "Removing stale PID file"
            rm -f "$PID_FILE"
        fi
    fi
}

is_running() {
    clean_stale_pid
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null || true)
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
    else
        return 1
    fi
}

find_port_pids() {
    # lsof works on macOS/Linux and is the safest way to identify listeners.
    lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true
}

find_orphan_server_pids() {
    local managed_pid=""
    if [ -f "$PID_FILE" ]; then
        managed_pid=$(cat "$PID_FILE" 2>/dev/null || true)
    fi

    local candidates
    candidates=$(( pgrep -f "python.*server.py" 2>/dev/null; pgrep -f "uvicorn.*server:app" 2>/dev/null ) | sort -u || true)
    [ -z "$candidates" ] && return 0

    for pid in $candidates; do
        [ -n "$managed_pid" ] && [ "$pid" = "$managed_pid" ] && continue
        echo "$pid"
    done
}

kill_pid_graceful() {
    local pid="$1"
    [ -z "$pid" ] && return 0
    kill "$pid" 2>/dev/null || true
}

kill_pid_force_if_alive() {
    local pid="$1"
    [ -z "$pid" ] && return 0
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
    fi
}

kill_process_tree() {
    local root_pid="$1"
    [ -z "$root_pid" ] && return 0

    # Kill known children first, then the parent.
    pkill -P "$root_pid" 2>/dev/null || true
    kill_pid_graceful "$root_pid"
}

wait_for_port_release() {
    local max_wait="${1:-10}"
    for _ in $(seq 1 "$max_wait"); do
        if [ -z "$(find_port_pids)" ]; then
            return 0
        fi
        sleep 1
    done
    log_warn "Port $PORT still appears busy after ${max_wait}s"
    return 1
}

kill_hindering_processes() {
    local pids
    pids="$(find_port_pids)"
    if [ -n "$pids" ]; then
        log_warn "Found process(es) listening on port $PORT: $pids"
        for pid in $pids; do
            kill_process_tree "$pid"
        done
        sleep 1
        for pid in $pids; do
            kill_pid_force_if_alive "$pid"
        done
    fi

    local orphans
    orphans="$(find_orphan_server_pids)"
    if [ -n "$orphans" ]; then
        log_warn "Found orphan API process(es): $orphans"
        for pid in $orphans; do
            kill_process_tree "$pid"
        done
        sleep 1
        for pid in $orphans; do
            kill_pid_force_if_alive "$pid"
        done
    fi
}

start_api() {
    acquire_lock
    if is_running; then
        echo -e "${YELLOW}XO Cowork API is already running (PID: $(cat "$PID_FILE"))${NC}"
        return 0
    fi

    kill_hindering_processes
    wait_for_port_release 10 || true

    log "Starting XO Cowork API on ${HOST}:${PORT}..."
    nohup bash -c '
        cd "'"$SCRIPT_DIR"'"
        export HOST="'"$HOST"'"
        export PORT="'"$PORT"'"
        python server.py
    ' >> "$LOG_FILE" 2>&1 &

    local pid=$!
    echo "$pid" > "$PID_FILE"
    sleep 1

    if kill -0 "$pid" 2>/dev/null; then
        log_success "XO Cowork API started (PID: $pid, port: $PORT)"
        echo -e "Logs: ${CYAN}$LOG_FILE${NC}"
    else
        rm -f "$PID_FILE"
        log_error "Failed to start XO Cowork API. Check logs: $LOG_FILE"
        return 1
    fi
}

stop_api() {
    acquire_lock
    if is_running; then
        local pid
        pid=$(cat "$PID_FILE")
        log "Stopping XO Cowork API (PID: $pid)..."
        kill_process_tree "$pid"

        for _ in $(seq 1 10); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done

        kill_pid_force_if_alive "$pid"
        rm -f "$PID_FILE"
    else
        log_warn "Managed API process not running"
    fi

    kill_hindering_processes
    wait_for_port_release 10 || true
    log_success "XO Cowork API stopped"
}

restart_api() {
    acquire_lock
    stop_api
    sleep 1
    start_api
}

status_api() {
    if is_running; then
        echo -e "${GREEN}XO Cowork API is running${NC} (PID: $(cat "$PID_FILE"))"
    else
        echo -e "${RED}XO Cowork API is not running${NC}"
    fi

    local listeners
    listeners="$(find_port_pids)"
    if [ -n "$listeners" ]; then
        echo -e "${YELLOW}Port $PORT listener PID(s):${NC} $listeners"
    else
        echo -e "${CYAN}No process currently listening on port $PORT${NC}"
    fi
}

show_logs() {
    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE"
    else
        echo "No log file found at $LOG_FILE"
    fi
}

case "${1:-restart}" in
    start)   start_api ;;
    stop)    stop_api ;;
    restart) restart_api ;;
    status)  status_api ;;
    logs)    show_logs ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
