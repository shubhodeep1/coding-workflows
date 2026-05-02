#!/bin/bash
set -euo pipefail

# Only run in Claude Code on the web. Local sessions are unaffected.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

log() { printf '[session-start] %s\n' "$*"; }

install_gh() {
  if command -v gh >/dev/null 2>&1; then
    log "gh already installed: $(gh --version | head -n1)"
    return 0
  fi

  log "Installing gh CLI..."
  if ! command -v apt-get >/dev/null 2>&1; then
    log "apt-get not available; skipping gh install."
    return 0
  fi

  local sudo=""
  if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
      sudo="sudo -n"
    else
      log "Insufficient privileges (not root and no passwordless sudo); skipping gh install."
      return 0
    fi
  fi

  $sudo install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | $sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  $sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | $sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  $sudo apt-get update -y >/dev/null
  $sudo apt-get install -y gh >/dev/null
  log "gh installed: $(gh --version | head -n1)"
}

verify_token() {
  if [ -z "${GH_TOKEN:-}" ] && [ -z "${GITHUB_TOKEN:-}" ]; then
    log "WARNING: neither GH_TOKEN nor GITHUB_TOKEN is set."
    log "  Add GH_TOKEN to your Claude Code cloud environment variables to enable"
    log "  Actions log access (scopes: repo + workflow, or fine-grained actions:read)."
    return 0
  fi

  if ! command -v gh >/dev/null 2>&1; then
    log "WARNING: token is set but 'gh' is not installed; skipping auth check."
    return 0
  fi

  if gh api /user --silent >/dev/null 2>&1; then
    log "gh authenticated OK; Actions logs are readable via 'gh run view --log'."
  else
    log "WARNING: 'gh api /user' failed. Token may be invalid, expired, or missing required permissions."
  fi
}

install_gh || log "gh install failed (non-fatal)"
verify_token || true
