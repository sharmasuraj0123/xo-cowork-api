#!/usr/bin/env bash
# ==============================================================
# config/agents/antigravity/setup.sh — install + configure the
# antigravity (agy) agent.
#
# Invoked by xo-cowork-api's FastAPI lifespan (server.py's
# _run_agent_setup) when AGENT_NAME=antigravity. Runs on every
# server boot; each step is idempotent so repeat invocations are
# cheap once the first run is done.
#
# Unlike hermes / openclaw, antigravity has no gateway lifecycle
# script (no `agent.sh`) and no channels — the `agy` CLI is invoked
# directly by the adapter via subprocess, like claude_code.
#
# Auth model: agy uses a consumer Google OAuth *token file*
# (~/.gemini/antigravity-cli/antigravity-oauth-token), self-refreshing.
# There is NO headless `agy login`; first-time login is an interactive
# browser flow. This script only reports login state so a logged-out
# workspace surfaces in the boot log (and the adapter surfaces it at
# chat time + on /models/status), mirroring claude_code's login guard.
# ==============================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
AGY_HOME="$HOME/.gemini/antigravity-cli"
TOKEN_FILE="$AGY_HOME/antigravity-oauth-token"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()         { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] antigravity-setup: $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

# ==============================================================
# Step 1 — Materialise $REPO_ROOT/.env from environment vars if the
# file isn't already present. load_dotenv() reads this on import.
# Manual edits are preserved: never overwrites an existing .env.
# ==============================================================
write_env_file() {
    if [ -f "$ENV_FILE" ]; then
        log ".env already exists at $ENV_FILE — leaving it untouched"
        return 0
    fi
    log "Writing .env from environment (first time)"
    umask 077
    cat > "$ENV_FILE" <<ENVEOF
# Agent dispatch
AGENT_NAME=antigravity

# Pin the agy CLI version so flag/output shapes don't shift under us.
AGY_CLI_DISABLE_AUTO_UPDATE=1

# XO Integration
XO_AUTH_SESSION_ID=${XO_AUTH_SESSION_ID:-}
XO_POLL_TOKEN=${XO_POLL_TOKEN:-}
XO_API_KEY=${XO_API_KEY:-}
CHAT_API_BASE_URL=${CHAT_API_BASE_URL:-https://api-swarm-dev.xo.builders}
ENVEOF
    chmod 600 "$ENV_FILE"
    log_success ".env written (mode 600)"
}

# ==============================================================
# Step 2 — agy CLI sanity check. The binary is installed by the
# workspace template; this step only logs whether it landed on PATH so
# a missing install surfaces here instead of only at first prompt.
# ==============================================================
check_agy_cli() {
    export PATH="$HOME/.local/bin:$PATH"
    if command -v agy >/dev/null 2>&1; then
        local ver; ver="$(agy --version 2>/dev/null | head -1)"
        log_success "agy CLI on PATH at $(command -v agy) (version ${ver:-unknown})"
    else
        log_warn "agy CLI not on PATH — template install may have failed; chat will 500"
    fi
}

# ==============================================================
# Step 3 — Login state report. There is no headless `agy login`, so we
# cannot log the user in here; we only detect + report so the state is
# visible in the boot log. The token is usable when it exists and holds a
# refresh_token (agy self-refreshes) or an unexpired access_token.
# ==============================================================
check_login() {
    if [ ! -f "$TOKEN_FILE" ]; then
        log_warn "antigravity is LOGGED OUT ($TOKEN_FILE absent) — chat will error and /models/status will report 'error' until you run \`agy\` once interactively to complete Google sign-in."
        return 0
    fi
    # Best-effort usability check without leaking secret values.
    if command -v python3 >/dev/null 2>&1; then
        local state
        state="$(python3 - "$TOKEN_FILE" <<'PY' 2>/dev/null
import json, sys, datetime
try:
    d = json.load(open(sys.argv[1]))
    t = d.get("token") or {}
    if t.get("refresh_token"):
        print("ok"); sys.exit()
    exp = t.get("expiry")
    if t.get("access_token") and isinstance(exp, str):
        e = datetime.datetime.fromisoformat(exp.replace("Z", "+00:00"))
        print("ok" if e > datetime.datetime.now(datetime.timezone.utc) else "expired")
    else:
        print("ok" if t.get("access_token") else "invalid")
except Exception:
    print("invalid")
PY
)"
        case "$state" in
            ok)      log_success "antigravity login token present and usable" ;;
            expired) log_warn "antigravity token present but access_token expired and no refresh_token — re-run \`agy\` interactively" ;;
            *)       log_warn "antigravity token file unreadable/invalid ($state) — re-run \`agy\` interactively" ;;
        esac
    else
        log "antigravity token file present ($TOKEN_FILE)"
    fi
}

# ==============================================================
# Main
# ==============================================================
log "Starting antigravity agent bootstrap"
write_env_file
check_agy_cli
check_login
log_success "antigravity agent bootstrap complete"
exit 0
