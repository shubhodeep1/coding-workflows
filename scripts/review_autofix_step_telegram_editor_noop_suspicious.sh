#!/usr/bin/env bash
# Body of the "Telegram editor-noop-suspicious warning" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# well under the 430,000-byte contract-test ceiling and GitHub's 512,000-byte
# workflow file limit (#10166). The step sources this file in its own shell,
# so the step's if:, env: and continue-on-error: stay in the workflow; edit
# those there.
# GitHub does not substitute workflow expressions in a sourced script, so the
# step passes github.server_url, github.repository and github.run_id in as
# AUTOFIX_STEP_SERVER_URL, AUTOFIX_STEP_REPOSITORY and AUTOFIX_STEP_RUN_ID;
# they default to the runner's GITHUB_* values when unset.
# The inline body ran under the runner's default `bash -e` (no -u, no
# pipefail). The step-script contract requires the body to open with
# `set -euo pipefail`; the next line restores the original options so the
# body behaves exactly as it did inline.
set -euo pipefail
set +u +o pipefail
AUTOFIX_STEP_SERVER_URL="${AUTOFIX_STEP_SERVER_URL:-${GITHUB_SERVER_URL:-https://github.com}}"
AUTOFIX_STEP_REPOSITORY="${AUTOFIX_STEP_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
AUTOFIX_STEP_RUN_ID="${AUTOFIX_STEP_RUN_ID:-${GITHUB_RUN_ID:-}}"
source "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh"
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
PR_URL="${AUTOFIX_STEP_SERVER_URL}/${AUTOFIX_STEP_REPOSITORY}/pull/${PR_NUMBER}"
RUN_URL="${AUTOFIX_STEP_SERVER_URL}/${AUTOFIX_STEP_REPOSITORY}/actions/runs/${AUTOFIX_STEP_RUN_ID}"

# review-autofix-failure:v1 marker for the gate's identical-failure
# fingerprint cap. Same evidence files and reason precedence as
# "Assemble failure evidence", so every failure comment of one run
# carries one fingerprint. Empty when the helper is unavailable.
AUTOFIX_FAILURE_MARKER_SUFFIX=""
if [ -n "${AUTOFIX_FAILURE_HEAL_PY:-}" ] && [ -f "${AUTOFIX_FAILURE_HEAL_PY}" ]; then
  autofix_failure_marker_line="$(PYTHONDONTWRITEBYTECODE=1 python3 "${AUTOFIX_FAILURE_HEAL_PY}" autofix-failure-fingerprint \
    --summary-line-file "${RUNTIME_DIR:-}/review_autofix_run_summary_line.txt" \
    --evidence-file "${RUNTIME_DIR:-}/reviewers_failure_evidence.txt" \
    --evidence-file "${RUNTIME_DIR:-}/sandbox_prepare_failure_evidence.txt" \
    --evidence-file "${RUNTIME_DIR:-}/editor_stage_stderr.txt" \
    --evidence-file "${RUNTIME_DIR:-}/collect_metadata_stderr.txt" \
    --evidence-file "${RUNTIME_DIR:-}/review_autofix_run_summary_line.txt" \
    --head-sha "${AUTOFIX_FAILURE_HEAD_SHA:-}" \
    --support-sha "${REVIEW_SUPPORT_SHA:-}" \
    --run-id "${GITHUB_RUN_ID:-}" 2>/dev/null | sed -n 's/^marker=//p' | head -n 1 || true)"
  if [ -n "${autofix_failure_marker_line}" ]; then
    AUTOFIX_FAILURE_MARKER_SUFFIX=$'\n\n'"${autofix_failure_marker_line}"
  fi
fi

# Branch the operator-facing alert on EDITOR_NOOP_REFUSAL (set by
# the "Validate editor no-op disposition" step's Check 1b when the
# editor model returns a safety-policy refusal). A refusal is
# transient — re-running after the provider-side prompt cache
# expires usually clears it — so the alert points the operator at a
# re-run instead of the generic "editor failed silently / audit
# inconsistent" causes, which do not apply to a refusal. The flag
# was added for exactly this branch (see
# probably_unnecessary_but_read_if_stuck.md §20.10.1). Auto-merge
# stays blocked in both branches (the EDITOR_NOOP_SUSPICIOUS gate is
# unchanged — a refusal always also sets SUSPICIOUS=true via Check
# 1). The PR comment BODY keeps the "⚠️ **Editor no-op suspicious**"
# literal in BOTH branches because the orchestrator-poll
# noop-suspicious recovery sweep (scripts/orchestrate_poll_process.sh)
# greps PR comments for it to auto-re-dispatch the run — which is
# precisely the remediation a refusal needs. Dropping it from the
# refusal branch would strand refusal PRs (no auto-re-dispatch, no
# force-merge). Do NOT rename the flags or that literal (CLAUDE.md §6).
if [ "${EDITOR_NOOP_REFUSAL:-false}" = "true" ]; then
  MSG="⚠️ Editor safety-policy refusal: #${PR_NUMBER}"
  MSG+=$'\n'
  MSG+="Editor model returned a safety-policy refusal — no changes applied."
  MSG+=$'\n'
  MSG+="Auto-merge blocked. Re-run is likely to succeed once the provider-side cache expires."
  MSG+=$'\n'
  MSG+="PR: ${PR_URL}"
  MSG+=$'\n'
  MSG+="Run: ${RUN_URL}"
  tg_send_tracked "${PR_NUMBER}" "${MSG}" "DEBUG"

  BODY="⚠️ **Editor no-op suspicious** — the editor model returned a safety-policy refusal (safety filter), so no fix was applied this pass."
  BODY+=$'\n\n'
  BODY+="This is typically transient. Re-running the AI Review workflow after the provider-side prompt cache expires usually clears it; manual review is only needed if the refusal recurs."
  BODY+=$'\n'
  BODY+="Auto-merge has been **blocked**."
  BODY+="${AUTOFIX_FAILURE_MARKER_SUFFIX}"
  gh_retry gh api "repos/${AUTOFIX_STEP_REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${BODY}" >/dev/null 2>&1 || true
elif [ "${EDITOR_NOOP_RECOVERABLE_FAILURE:-false}" = "true" ]; then
  # Every editor attempt failed and review_apply_fixes.sh wrote
  # the recoverable_failure partial-finalize summary (validator
  # Check 1c). The editor produced no output, so "claimed no
  # changes needed" would misdirect the operator; name the
  # attempt count and the final attempt's provider error
  # instead. Observed on PR #4077 (runs 34692519987,
  # 34700918528, 34702442346): the broker answered HTTP 429 on
  # every attempt for 21 consecutive runs while the alert kept
  # asking for a "manual review or re-run". The attempt stderr
  # files are the editor_attempt_<n>.err copies the retry loop
  # leaves in PREVIOUS_REVIEWS_DIR; only the final `Error:` line
  # (or the last opencode level=ERROR value) is quoted, clipped
  # to 240 characters, never the whole stderr.
  editor_attempts=0
  editor_last_error=""
  if [ -n "${PREVIOUS_REVIEWS_DIR:-}" ] && [ -d "${PREVIOUS_REVIEWS_DIR}" ]; then
    editor_attempts="$(ls -1 "${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*.err 2>/dev/null | wc -l | tr -d '[:space:]' || true)"
    last_err_file="$(ls -1 "${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*.err 2>/dev/null | sort -V | tail -n 1 || true)"
    if [ -n "${last_err_file}" ] && [ -s "${last_err_file}" ]; then
      editor_last_error="$(grep -aE '^Error: ' "${last_err_file}" | tail -n 1 | tr -d '\r\140' | cut -c1-240 || true)"
      if [ -z "${editor_last_error}" ]; then
        editor_last_error="$(grep -aoE 'level=ERROR .*error="[^"]*"' "${last_err_file}" | tail -n 1 | sed -E 's/.*error="([^"]*)".*/\1/' | tr -d '\140' | cut -c1-240 || true)"
      fi
    fi
  fi
  [ -n "${editor_last_error}" ] || editor_last_error="not captured — see the editor step log of the linked run"
  attempts_label="every attempt"
  if [ "${editor_attempts:-0}" -eq 1 ] 2>/dev/null; then
    attempts_label="1 attempt"
  elif [ "${editor_attempts:-0}" -gt 1 ] 2>/dev/null; then
    attempts_label="all ${editor_attempts} attempts"
  fi
  MSG="⚠️ Editor failed on ${attempts_label}: #${PR_NUMBER}"
  MSG+=$'\n'
  MSG+="No editor output was produced (recoverable failure). Last provider error: ${editor_last_error}"
  MSG+=$'\n'
  MSG+="Auto-merge blocked. Re-dispatch happens automatically; if the same error recurs the cause needs a fix, not a re-run."
  MSG+=$'\n'
  MSG+="PR: ${PR_URL}"
  MSG+=$'\n'
  MSG+="Run: ${RUN_URL}"
  tg_send_tracked "${PR_NUMBER}" "${MSG}" "DEBUG"

  BODY="⚠️ **Editor no-op suspicious** — the editor failed on ${attempts_label} and produced no output (recoverable failure), so no fix was applied this pass."
  BODY+=$'\n\n'
  BODY+="Last provider error from the final attempt: \`${editor_last_error}\`"
  BODY+=$'\n\n'
  BODY+="Auto-merge has been **blocked**. The review run is re-dispatched automatically; if the same error recurs, inspect the editor step log of the linked run — a re-run will not clear a deterministic failure."
  BODY+="${AUTOFIX_FAILURE_MARKER_SUFFIX}"
  gh_retry gh api "repos/${AUTOFIX_STEP_REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${BODY}" >/dev/null 2>&1 || true
else
  MSG="⚠️ Editor no-op suspicious: #${PR_NUMBER}"
  MSG+=$'\n'
  MSG+="Editor claimed no changes needed but disposition could not be verified."
  MSG+=$'\n'
  MSG+="Auto-merge blocked. The no-op recovery sweep re-dispatches the review automatically."
  MSG+=$'\n'
  MSG+="PR: ${PR_URL}"
  MSG+=$'\n'
  MSG+="Run: ${RUN_URL}"
  # DEBUG for all three no-op branches: the orchestrator's no-op
  # recovery sweep (or, for orchestrator sub-issue PRs, the stall
  # ladder) re-dispatches; its force-merge gate failures page once.
  tg_send_tracked "${PR_NUMBER}" "${MSG}" "DEBUG"

  BODY="⚠️ **Editor no-op suspicious** — the editor claimed no changes were needed but the disposition could not be verified."
  BODY+=$'\n\n'
  BODY+="Possible causes: editor failed silently, did not read reviewer files, or audit counts are inconsistent."
  BODY+=$'\n'
  BODY+="Auto-merge has been **blocked**. Please review the editor summary and re-run the workflow if appropriate."
  BODY+="${AUTOFIX_FAILURE_MARKER_SUFFIX}"
  gh_retry gh api "repos/${AUTOFIX_STEP_REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${BODY}" >/dev/null 2>&1 || true
fi
