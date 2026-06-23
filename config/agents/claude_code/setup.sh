#!/usr/bin/env bash
# ==============================================================
# config/agents/claude_code/setup.sh — install + configure the
# claude_code agent.
#
# Invoked by xo-cowork-api's FastAPI lifespan (server.py's
# _run_agent_setup) when AGENT_NAME=claude_code. Runs every time
# the xo-cowork-api server starts; each step is idempotent so
# repeat invocations are cheap (≈ seconds) once the first run is
# done.
#
# This script absorbs the claude_code bootstrap work that used to
# live in the Coder template's startup_script (main-claude.tf), so
# the template can become a thin shell. Reads its inputs from
# environment vars that the Coder k8s pod injects.
#
# Unlike hermes / openclaw, claude_code has no separate gateway
# lifecycle script (no `claude.sh`), no channels, and no shims —
# the Claude CLI is invoked directly by xo-cowork-api's
# ClaudeCodeClient via subprocess. So this setup is intentionally
# shorter than its siblings.
#
# Required env (server will misbehave without one of these):
#     ANTHROPIC_API_KEY  or  CLAUDE_CODE_OAUTH_TOKEN
#
# Optional env:
#     XO_AUTH_SESSION_ID / XO_POLL_TOKEN / XO_API_KEY
#     CHAT_API_BASE_URL   default: https://api-swarm-dev.xo.builders
#     NODE_VERSION        default: 22
# ==============================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()         { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] claude-setup: $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

# Decide whether sudo is needed and available. Many container images run
# the app as the same user that owns / so sudo isn't always required.
if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    SUDO=""
fi

# ==============================================================
# Step 1 — apt prereqs (jq, unzip).
# `jq` and `unzip` are not strictly required by the Claude CLI itself,
# but several xo-cowork-api routes (and the Claude installer for some
# distros) lean on them. Keep this list lean: anything that's needed
# *before* cowork-api can boot already lives in the template.
# ==============================================================
install_apt_prereqs() {
    local missing=()
    command -v jq    >/dev/null 2>&1 || missing+=("jq")
    command -v unzip >/dev/null 2>&1 || missing+=("unzip")

    if [ "${#missing[@]}" -eq 0 ]; then
        log "apt prereqs already present (jq, unzip)"
        return 0
    fi

    if ! command -v apt-get >/dev/null 2>&1; then
        log_warn "apt-get not available — cannot install: ${missing[*]} (continuing anyway)"
        return 0
    fi

    log "Installing apt prereqs: ${missing[*]}"
    $SUDO apt-get update -y || log_warn "apt-get update failed (continuing)"
    if $SUDO apt-get install -y "${missing[@]}"; then
        log_success "apt prereqs installed"
    else
        log_warn "apt-get install failed for: ${missing[*]} — claude_code may degrade"
    fi
}

# ==============================================================
# Step 2 — NVM + Node. The Claude CLI binary itself is self-
# contained, but main-claude.tf has always installed Node so that
# users have a working Node toolchain inside the workspace (common
# for the kinds of repos this agent edits). Keep parity.
# Skip if `node` and `npm` are already on PATH.
# ==============================================================
install_node() {
    # Try to load existing NVM into this shell first
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    # shellcheck disable=SC1091
    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" >/dev/null 2>&1 || true

    if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
        log "node $(node -v) and npm $(npm -v) already available"
        return 0
    fi

    if [ ! -d "$NVM_DIR" ]; then
        log "Installing NVM..."
        if ! curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.0/install.sh | bash; then
            log_error "NVM install failed — Node toolchain will be unavailable"
            return 1
        fi
    fi

    # shellcheck disable=SC1091
    . "$NVM_DIR/nvm.sh"

    local node_version="${NODE_VERSION:-22}"
    log "Installing Node.js $node_version via NVM..."
    nvm install "$node_version" || { log_error "nvm install $node_version failed"; return 1; }
    nvm alias default "$node_version" >/dev/null
    nvm use default >/dev/null

    # Make NVM auto-load for future shells (idempotent)
    $SUDO tee /etc/profile.d/nvm.sh >/dev/null <<'EOF' || true
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
EOF
    $SUDO chmod +x /etc/profile.d/nvm.sh 2>/dev/null || true

    log_success "node $(node -v), npm $(npm -v) ready"
}

# ==============================================================
# Step 3 — Materialise $REPO_ROOT/.env from environment vars if
# the file isn't already present. xo-cowork-api's load_dotenv()
# reads this on import; the Claude CLI is then invoked with the
# resulting process env.
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
AGENT_NAME=claude_code

# Claude / Anthropic auth — claude_code prefers CLAUDE_CODE_OAUTH_TOKEN,
# but falls back to ANTHROPIC_API_KEY if that's all the user supplied.
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-${ANTHROPIC_API_KEY:-}}

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
# Step 4 — Claude CLI sanity check. The CLI itself is installed by
# the Coder template's startup_script (parity with the hermes and
# openclaw templates, which all install Claude CLI as a general
# coding tool). This step only logs whether it landed on PATH so
# missing installs surface in the cowork-api boot log instead of
# only at first /ask_question.
# ==============================================================
check_claude_cli() {
    export PATH="$HOME/.claude/bin:$HOME/.local/bin:$PATH"
    if command -v claude >/dev/null 2>&1; then
        log_success "claude CLI on PATH at $(command -v claude)"
    else
        log_warn "claude CLI not on PATH — template install may have failed; /ask_question will 500"
    fi
}

# ==============================================================
# Main
# ==============================================================
log "Starting claude_code agent bootstrap"
install_apt_prereqs
install_node
write_env_file
check_claude_cli
log_success "claude_code agent bootstrap complete"
exit 0
