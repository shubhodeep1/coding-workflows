#!/usr/bin/env bash
# Body of the "Count autofix iterations" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail

# ----- Orchestrator PR mode classification -----
orch_pr_mode="other"
if [ "${ORCH_PR_AUTOFIX_FLOW_ENABLED}" = "true" ]; then
  _head_ref_class=""
  _base_ref_class=""
  if [ -f "${PR_META_FILE}" ]; then
    _head_ref_class="$(jq -r '.headRefName // ""' "${PR_META_FILE}" 2>/dev/null || echo "")"
    _base_ref_class="$(jq -r '.baseRefName // ""' "${PR_META_FILE}" 2>/dev/null || echo "")"
  fi
  # Pre-validate ORCH_INTEGRATION_BRANCH_PATTERN before classifying:
  #   - Empty pattern would match every ref (`grep -Eq -- ""` matches
  #     everything), misclassifying every PR as orch_intermediate or
  #     orch_final.
  #   - Invalid POSIX ERE makes `grep -Eq` exit with status 2 rather
  #     than 1; if a misconfigured regex slipped past code review,
  #     every classifier call would log a noisy regex error.
  # Both cases fall open to orch_pr_mode=other (legacy behavior) with
  # one ::warning:: log line, so a misconfigured repo var cannot
  # brick `review_autofix` runs for non-orchestrator PRs.
  _pattern_ok="true"
  if [ -z "${ORCH_INTEGRATION_BRANCH_PATTERN}" ]; then
    echo "::warning::ORCH_INTEGRATION_BRANCH_PATTERN is empty; falling back to orch_pr_mode=other for this run."
    _pattern_ok="false"
  else
    _pattern_check_exit=0
    printf 'x\n' | grep -Eq -- "${ORCH_INTEGRATION_BRANCH_PATTERN}" >/dev/null 2>&1 || _pattern_check_exit=$?
    if [ "${_pattern_check_exit}" -ge 2 ]; then
      echo "::warning::ORCH_INTEGRATION_BRANCH_PATTERN is not a valid POSIX ERE (grep exit ${_pattern_check_exit}); falling back to orch_pr_mode=other for this run."
      _pattern_ok="false"
    fi
  fi
  # Use `printf` + `grep -Eq --` so a repo-configured pattern that
  # happens to start with `-` (e.g. `-orchestrator/...`) is parsed
  # as the regex argument and not as a grep option / echo flag.
  if [ "${_pattern_ok}" = "true" ] && [ -n "${_head_ref_class}" ] && printf '%s\n' "${_head_ref_class}" | grep -Eq -- "${ORCH_INTEGRATION_BRANCH_PATTERN}"; then
    if [ -n "${_base_ref_class}" ] && printf '%s\n' "${_base_ref_class}" | grep -Eq -- "${ORCH_INTEGRATION_BRANCH_PATTERN}"; then
      orch_pr_mode="orch_intermediate"
    else
      orch_pr_mode="orch_final"
    fi
  fi
fi
echo "Orchestrator PR mode: ${orch_pr_mode} (head='${_head_ref_class:-?}' base='${_base_ref_class:-?}', flow_enabled=${ORCH_PR_AUTOFIX_FLOW_ENABLED})"
echo "orch_pr_mode=${orch_pr_mode}" >> "$GITHUB_OUTPUT"

# All modes use MAX_AUTOFIX_ITERATIONS (default 5).  The
# former orch_intermediate cap of 1 was removed: catching blocking
# issues per sub-issue PR (where the diff is small and the linked-
# issue context is narrow) is cheaper and more reliable than
# front-loading the entire review burden onto the final integration
# → default-branch merge.
effective_max_iterations="${MAX_AUTOFIX_ITERATIONS}"

# Pre-walk shallow safeguard: the codex-agent's "Checkout repo"
# step uses fetch-depth: 100 to cap clone size on history-heavy
# consumer repos.  Both the [ai-autofix] walk below and the
# [judge-fix] walk further down expect to see all PR-unique
# history.  Depth 100 is enough for any realistic PR, but on
# the off chance a PR has accumulated >100 commits (e.g. a
# long-running orchestrator integration PR) we deepen by a
# bounded amount once before walking so the counts are not
# silently truncated.
#
# Explicit refspec on TARGET_BRANCH (the PR head ref): without
# one, `git fetch ... origin` would defer to the default
# `remote.origin.fetch = +refs/heads/*:refs/remotes/origin/*`
# and pull every remote branch, undoing the whole point of
# fetch-depth: 100.  The walks only need HEAD's first-parent
# history, which lives on TARGET_BRANCH.
#
# No-op when the repo is already a full clone, when
# TARGET_BRANCH is unset (Checkout PR head branch hit its
# validation early-exit), or when the deepen fetch fails
# (fail-open: counts may be undercounted but the workflow
# proceeds).
if [ -f "$(git rev-parse --git-dir)/shallow" ]; then
  if [ -n "${TARGET_BRANCH:-}" ]; then
    echo "Shallow repository detected — deepening TARGET_BRANCH=${TARGET_BRANCH} once (+200) so autofix/judge-fix walks see PR-unique history beyond the initial fetch-depth boundary."
    if ! git fetch --no-tags --prune --deepen=200 origin "+refs/heads/${TARGET_BRANCH}:refs/remotes/origin/${TARGET_BRANCH}" 2>/dev/null; then
      echo "::warning::git fetch --deepen=200 origin +refs/heads/${TARGET_BRANCH}:refs/remotes/origin/${TARGET_BRANCH} failed before autofix/judge-fix walks; walk counts may be undercounted on PRs with >100 commits."
    fi
  else
    echo "::warning::TARGET_BRANCH unset (Checkout PR head branch likely early-exited); skipping pre-walk deepen safeguard. Walk counts may be undercounted on PRs with >100 commits."
  fi
fi

# Count consecutive [ai-autofix] commits from HEAD backwards.
# [claude-autofix] rounds (left by the retired Claude-fixer
# hand-off on older PR heads) count the same way; [claude-intervention]
# and [claude-merge-resolve] commits break the run, as [judge-fix]
# and [ai-merge-resolve] do.
autofix_count=0
while true; do
  msg="$(git log -1 --format='%s' "HEAD~${autofix_count}" 2>/dev/null || true)"
  if echo "${msg}" | grep -Eq '^\[(ai|claude)-autofix\]'; then
    autofix_count=$((autofix_count + 1))
  else
    break
  fi
done

echo "Consecutive autofix commits: ${autofix_count} (max: ${effective_max_iterations})"
echo "autofix_iteration=${autofix_count}" >> "$GITHUB_OUTPUT"

# skip_judge is retained as a step output for backward compat with
# downstream gate expressions (e.g. the rb_judge step's
# `steps.retrigger_guard.outputs.skip_judge != 'true'` guard) but
# is now always emitted as `false` — the orch_intermediate
# short-circuit was removed so every mode runs the per-PR judge
# after autofix exhaustion.  The force_rb_judge stall-recovery
# escape hatch (dispatched by the orchestrator stall poller via
# review_rb_judge_dispatch.yml) is unaffected; FORCE_RB_JUDGE=true
# still forces max_iterations_reached=true below so the rb_judge
# step fires directly.
echo "skip_judge=false" >> "$GITHUB_OUTPUT"

# force_rb_judge short-circuit: the stall poller dispatches the
# review workflow with this input set when a linked issue has
# been stuck at ai:review-blocked past the stall threshold.
# Force max_iterations_reached=true so the reviewer/editor/
# commit steps (all gated on max_iterations_reached != 'true')
# are skipped and the rb_judge step fires directly against the
# existing PR state.
if [ "${FORCE_RB_JUDGE}" = "true" ]; then
  echo "force_rb_judge=true — forcing max_iterations_reached=true to run only the review-blocked judge."
  echo "max_iterations_reached=true" >> "$GITHUB_OUTPUT"
elif [ "${autofix_count}" -ge "${effective_max_iterations}" ]; then
  echo "Max autofix iterations reached — skipping review/editor."
  echo "max_iterations_reached=true" >> "$GITHUB_OUTPUT"
else
  echo "max_iterations_reached=false" >> "$GITHUB_OUTPUT"
fi

# Count [judge-fix] commits in the branch history for retry tracking.
judge_fix_count=0
while IFS= read -r line; do
  if echo "${line}" | grep -q '^\[judge-fix\]'; then
    judge_fix_count=$((judge_fix_count + 1))
  fi
done < <(git log --format='%s' 2>/dev/null || true)
echo "Judge-fix commits in history: ${judge_fix_count}"
echo "judge_fix_count=${judge_fix_count}" >> "$GITHUB_OUTPUT"
