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

  local sudo_cmd=()
  if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
      sudo_cmd=(sudo -n)
    else
      log "Insufficient privileges (not root and no passwordless sudo); skipping gh install."
      return 0
    fi
  fi

  "${sudo_cmd[@]}" install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | "${sudo_cmd[@]}" tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  "${sudo_cmd[@]}" chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | "${sudo_cmd[@]}" tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  "${sudo_cmd[@]}" apt-get update -y >/dev/null
  "${sudo_cmd[@]}" apt-get install -y gh >/dev/null
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

  # Two-stage probe so a missing actions:read scope doesn't get reported as
  # "token broken" — gh remains usable for PRs, issues, commits, and file
  # contents even when Actions logs are gated.
  if ! gh auth status >/dev/null 2>&1; then
    log "WARNING: 'gh auth status' failed. Token is set but gh cannot authenticate (likely invalid or expired)."
    return 0
  fi

  log "gh authenticated; PRs, issues, commits, and file contents are readable via 'gh' CLI."

  if gh run list -L 1 >/dev/null 2>&1; then
    log "gh has actions:read for this repo; Actions logs are readable via 'gh run view --log <id>'."
  else
    log "NOTE: 'gh run list' failed — token likely lacks actions:read for this repo. Other gh reads (PRs/issues/files) still work; use mcp__github__get_workflow_run_logs for Actions logs."
  fi
}

install_gh || log "gh install failed (non-fatal)"
verify_token || true
