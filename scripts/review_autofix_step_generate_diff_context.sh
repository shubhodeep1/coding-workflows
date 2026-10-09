#!/usr/bin/env bash
# Body of the "Generate diff context" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail

echo "Generating reviewer diff context"
bash "${SUPPORT_SCRIPTS_DIR}/git_ref_health_check.sh" repair

# Helper: ensure the merge-base between HEAD and origin/<base>
# is reachable in the local (possibly shallow) clone.
# `git diff origin/<base>...HEAD` and the merge precheck both
# need this merge-base; on a shallow clone (codex-agent uses
# fetch-depth: 100) it may sit below the boundary for long-
# running PRs.  Deepens iteratively, falling back to
# --unshallow as a last resort.  Fail-open: callers that need
# the merge-base check `git rev-parse --verify origin/<base>`
# themselves and degrade gracefully.
#
# Refspec discipline: every `git fetch` here passes explicit
# refspecs limited to BASE_BRANCH + (when known) TARGET_BRANCH.
# Without them, `git fetch ... origin` defers to the default
# `remote.origin.fetch = +refs/heads/*:refs/remotes/origin/*`
# and would pull every remote branch on each deepen call —
# undoing the fetch-depth: 100 budget this PR is trying to
# enforce.  Either side of the merge-base reaching its full
# ancestor is sufficient for git merge-base to resolve, so
# deepening just these two refs is enough.
_ensure_merge_base_reachable() {
  local _remote_ref="$1"
  local _local_ref="${2:-HEAD}"
  local _iter=0
  local -a _refspecs=("+refs/heads/${BASE_BRANCH}:refs/remotes/origin/${BASE_BRANCH}")
  if [ -n "${TARGET_BRANCH:-}" ]; then
    _refspecs+=("+refs/heads/${TARGET_BRANCH}:refs/remotes/origin/${TARGET_BRANCH}")
  fi
  while [ "${_iter}" -lt 4 ]; do
    if [ ! -f "$(git rev-parse --git-dir)/shallow" ]; then
      return 0
    fi
    if git merge-base "${_remote_ref}" "${_local_ref}" >/dev/null 2>&1; then
      return 0
    fi
    _iter=$((_iter + 1))
    echo "merge-base ${_remote_ref}…${_local_ref} unreachable in shallow clone — deepening (attempt ${_iter}/4) on refs: ${_refspecs[*]}."
    if ! git fetch --no-tags --prune --deepen=200 origin "${_refspecs[@]}" 2>/dev/null; then
      echo "::warning::git fetch --deepen=200 origin ${_refspecs[*]} failed during deepen-on-miss; falling back to --unshallow on the same refs."
      git fetch --no-tags --prune --unshallow origin "${_refspecs[@]}" 2>/dev/null || true
      return 0
    fi
  done
  if ! git merge-base "${_remote_ref}" "${_local_ref}" >/dev/null 2>&1; then
    echo "::warning::merge-base ${_remote_ref}…${_local_ref} still unreachable after 4 deepen attempts; trying final --unshallow on refs: ${_refspecs[*]}."
    git fetch --no-tags --prune --unshallow origin "${_refspecs[@]}" 2>/dev/null || true
  fi
}

PR_DIFF_ATTEMPTED_PATHS="${PR_DIFF_ATTEMPTED_PATHS:-gh_pr_diff:${PR_DIFF_FILE}}"
if [ ! -s "${PR_DIFF_FILE}" ]; then
  echo "Primary gh pr diff output is empty. Attempting git fallback diff generation."
  PR_DIFF_ATTEMPTED_PATHS="${PR_DIFF_ATTEMPTED_PATHS};git_diff_origin_base_head:${PR_DIFF_FILE}"
  if ! git fetch --no-tags --prune origin "+refs/heads/${BASE_BRANCH}:refs/remotes/origin/${BASE_BRANCH}"; then
    echo "Warning: git fetch origin ${BASE_BRANCH} failed in fallback path; fallback git diff requires an existing origin/${BASE_BRANCH} ref."
  fi
  if git rev-parse --verify "origin/${BASE_BRANCH}" >/dev/null 2>&1; then
    _ensure_merge_base_reachable "origin/${BASE_BRANCH}" "HEAD"
    git diff "origin/${BASE_BRANCH}...HEAD" > "${PR_DIFF_FILE}" || true
  else
    echo "Warning: origin/${BASE_BRANCH} is unavailable in fallback path; skipping fallback git diff generation."
    : > "${PR_DIFF_FILE}"
  fi
fi

if [ ! -s "${PR_DIFF_FILE}" ]; then
  echo "Both primary and fallback diff generation produced empty output. Writing placeholder diff context."
  printf '%s\n' "[NO_PR_DIFF_AVAILABLE] Unable to generate PR patch content from both gh pr diff and git diff origin/<base>...HEAD for this run." > "${PR_DIFF_FILE}"
  echo "HAS_PR_DIFF=false" >> "$GITHUB_ENV"
  echo "PR_DIFF_SOURCE=placeholder_no_diff_available" >> "$GITHUB_ENV"
else
  if [ "${PR_DIFF_SOURCE:-gh_pr_diff_empty}" = "gh_pr_diff_empty" ]; then
    echo "PR_DIFF_SOURCE=git_fallback_origin_base_head" >> "$GITHUB_ENV"
  fi
  echo "HAS_PR_DIFF=true" >> "$GITHUB_ENV"
fi
echo "PR_DIFF_ATTEMPTED_PATHS=${PR_DIFF_ATTEMPTED_PATHS}" >> "$GITHUB_ENV"

cp "${PR_DIFF_FILE}" "${ORIGINAL_PR_DIFF_FILE}"

pr_diff_bytes="$(wc -c < "${PR_DIFF_FILE}" | tr -d '[:space:]')"
original_pr_diff_bytes="$(wc -c < "${ORIGINAL_PR_DIFF_FILE}" | tr -d '[:space:]')"
echo "Final PR diff bytes (${PR_DIFF_FILE}): ${pr_diff_bytes}"
echo "Final PR diff bytes (${ORIGINAL_PR_DIFF_FILE}): ${original_pr_diff_bytes}"
echo "Final PR diff sha256 (${PR_DIFF_FILE}): $(sha256sum "${PR_DIFF_FILE}" | awk '{print $1}')"
echo "Final PR diff sha256 (${ORIGINAL_PR_DIFF_FILE}): $(sha256sum "${ORIGINAL_PR_DIFF_FILE}" | awk '{print $1}')"
echo "Final PR diff preview (${PR_DIFF_FILE}) suppressed in logs for security."

if ! git fetch --no-tags --prune origin "+refs/heads/${BASE_BRANCH}:refs/remotes/origin/${BASE_BRANCH}"; then
  echo "Warning: git fetch origin ${BASE_BRANCH} failed before changed-files generation; continuing with current refs."
fi

if git rev-parse --verify "origin/${BASE_BRANCH}" >/dev/null 2>&1; then
  _ensure_merge_base_reachable "origin/${BASE_BRANCH}" "HEAD"
  git diff --name-only "origin/${BASE_BRANCH}...HEAD" > "${PR_CHANGED_FILES_FILE}" || true
else
  echo "Warning: origin/${BASE_BRANCH} is unavailable; writing empty changed-files list."
  : > "${PR_CHANGED_FILES_FILE}"
fi
git show --stat HEAD > "${LAST_COMMIT_STAT_FILE}" || true

# LAST_RUN_DIFF must reflect actual prior AI autofix work on PR head,
# not whatever HEAD~1 happens to be. The downstream OSCILLATION GUARD
# in the editor prompt (scripts/review_apply_fixes.sh:441-462) tells
# the model to leave any hunk visible in LAST_RUN_DIFF alone unless
# it can attach a "regression fingerprint" + "runtime failure path".
# The previous unconditional `git diff HEAD~1...HEAD` fed the editor
# the diff of impl commits, smoke-test bait commits, and manual
# pushes — falsely tripping the guardrail on first-iteration runs.
# Smoke run 25254574828 hit this: bait commit was at HEAD,
# last_run_diff showed the bait line, OSCILLATION GUARD activated,
# editor produced empty output across 6 attempts (3 + 3 retry) at
# reasoning=low → Phase 4b bait-removed verification failed.
# Walk back recent commits and only use the diff of the most recent
# `[ai-autofix]`/`[judge-fix]` commit on the PR head's first-parent
# chain, matching the iteration counter at line 2096-2105 and the
# in-step retry gate further down in this file (see the
# `_prior_autofix_count` block in the `Apply fixes with editor model`
# step). `--first-parent` is load-bearing: without it, `git log`
# walks merged-in history and could pick up an unrelated autofix
# commit from base (e.g. a `[ai-autofix]` commit on `main` pulled
# in by a merge), polluting LAST_RUN_DIFF on a first-iteration PR.
LAST_AUTOFIX_SHA="$(git log --first-parent --format='%H%x09%s' -n 50 2>/dev/null \
  | awk -F'\t' '$2 ~ /^\[(ai-autofix|judge-fix)\]/ { print $1; exit }' \
  || true)"
if [ -n "${LAST_AUTOFIX_SHA}" ] && git rev-parse "${LAST_AUTOFIX_SHA}^" >/dev/null 2>&1; then
  git diff "${LAST_AUTOFIX_SHA}^...${LAST_AUTOFIX_SHA}" > "${LAST_RUN_DIFF_FILE}" || true
  git diff --name-only "${LAST_AUTOFIX_SHA}^...${LAST_AUTOFIX_SHA}" > "${LAST_RUN_CHANGED_FILES_FILE}" || true
  git diff --stat "${LAST_AUTOFIX_SHA}^...${LAST_AUTOFIX_SHA}" > "${LAST_RUN_DIFF_STAT_FILE}" || true
else
  echo "No previous AI autofix run on PR head" > "${LAST_RUN_DIFF_FILE}"
  echo "No previous AI autofix run on PR head" > "${LAST_RUN_CHANGED_FILES_FILE}"
  echo "FIRST_RUN_NO_PREVIOUS_AI_CHANGES" > "${LAST_RUN_DIFF_STAT_FILE}"
fi
