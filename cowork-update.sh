#!/usr/bin/env bash
# Safe updater for current branch:
# 1) stash local changes
# 2) pull latest from origin/<current-branch>
# 3) apply stash back (without dropping it if apply fails)
#
# Usage:
#   ./cowork-update.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()         { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

cd "$SCRIPT_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log_error "Current directory is not a git repository: $SCRIPT_DIR"
    exit 1
fi

current_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [ -z "$current_branch" ] || [ "$current_branch" = "HEAD" ]; then
    log_error "Could not determine current branch (detached HEAD?)"
    exit 1
fi

stash_name="cowork-update-$(date '+%Y%m%d-%H%M%S')"
stash_ref=""

log "Current branch: ${CYAN}${current_branch}${NC}"

if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    log "Stashing local changes..."
    git stash push -u -m "$stash_name" >/dev/null
    stash_ref="$(git stash list | awk -v msg="$stash_name" '$0 ~ msg {print $1; exit}')"
    if [ -n "$stash_ref" ]; then
        log_success "Local changes stashed as $stash_ref ($stash_name)"
    else
        log_warn "Created stash, but could not resolve stash reference by name"
    fi
else
    log "No local changes to stash."
fi

log "Pulling latest changes from origin/${current_branch}..."
if git pull --ff-only origin "$current_branch"; then
    log_success "Pull succeeded"
else
    log_error "Pull failed. Your stash (if created) is safe and still available."
    [ -n "$stash_ref" ] && log "Saved stash: $stash_ref"
    exit 1
fi

if [ -n "$stash_ref" ]; then
    log "Applying stashed changes back: $stash_ref"
    if git stash apply "$stash_ref"; then
        log_success "Stash applied successfully"
        log_warn "Stash entry is intentionally kept (not dropped): $stash_ref"
    else
        log_error "Stash apply failed. No data lost; stash is still preserved: $stash_ref"
        log "Resolve conflicts and apply manually when ready:"
        echo "  git stash apply $stash_ref"
        exit 1
    fi
fi

log_success "Update flow completed."
