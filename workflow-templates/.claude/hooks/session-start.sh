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

# Last two path segments of the remote URL, with `.git` and trailing
# slashes stripped. Handles github.com URLs and Claude Code Web's local
# proxy form (http://...@127.0.0.1:PORT/git/<owner>/<repo>) the same way.
extract_repo_slug() {
  local url="$1"
  url="${url%.git}"
  url="${url%/}"
  printf '%s\n' "${url}" | awk -F'[/:]' '{ if (NF >= 2 && $NF != "" && $(NF-1) != "") print $(NF-1) "/" $NF }'
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

  log "gh authenticated; PR/issue/commit/file reads should work via 'gh' for any repo this token can access."

  # In Claude Code Web the only git remote points at a local proxy
  # (http://...@127.0.0.1:PORT/git/<owner>/<repo>), so `gh` can't infer
  # the GitHub repo and bare `gh run list` fails with "failed to determine
  # base repo" — unrelated to actions:read. Derive <owner>/<repo> from the
  # remote and pass `-R` so the probe actually tests scope. Surface the
  # resolved slug so model + user know the right `-R` value to pass on
  # every `gh` call that needs repo context.
  local remote_url repo_slug=""
  if remote_url=$(git config --get remote.origin.url 2>/dev/null) && [ -n "${remote_url}" ]; then
    repo_slug=$(extract_repo_slug "${remote_url}")
  fi

  if [ -z "${repo_slug}" ]; then
    log "NOTE: could not derive <owner>/<repo> from git remote; actions:read probe skipped. 'gh' is still usable; pass '-R <owner>/<repo>' explicitly on calls that need repo context."
    return 0
  fi

  if gh run list -L 1 -R "${repo_slug}" >/dev/null 2>&1; then
    log "gh has actions:read for ${repo_slug}; Actions logs readable via 'gh run view --log <id> -R ${repo_slug}'."
  else
    log "NOTE: 'gh run list -R ${repo_slug}' failed — token likely lacks actions:read for ${repo_slug}. Other gh reads (PRs/issues/files) still work via 'gh ... -R ${repo_slug}'; use mcp__github__get_workflow_run_logs for Actions logs."
  fi
}

install_gh || log "gh install failed (non-fatal)"
verify_token || true
