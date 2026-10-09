#!/usr/bin/env bash
# Body of the "Post editor summary comment" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
# GitHub does not substitute workflow expressions in a sourced script, so the
# step passes github.server_url, github.repository and github.run_id in as
# AUTOFIX_STEP_SERVER_URL, AUTOFIX_STEP_REPOSITORY and AUTOFIX_STEP_RUN_ID.
set -euo pipefail
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

# Empty-editor-output short-circuit.
#
# If the editor produced no structured summary AND no file
# changes were committed, this run has nothing meaningful to
# report — the synthetic fallback summary (below) would be a
# false positive that masks a codex crash/timeout/empty-prompt.
# Post a distinct, retry-friendly comment, signal the
# failure handlers to NOT apply ai:review-blocked for this
# specific cause, and fail the step so the stall poller can
# retry the review later instead of poisoning the linked
# issue with an unrecoverable label.
if [ ! -s "${EDITOR_SUMMARY_FILE}" ] && [ ! -s "${COMMITTED_FILES_FILE:-/dev/null}" ]; then
  echo "::warning::Editor produced no summary and no committed changes — treating as retryable no-op."
  RUN_URL="${AUTOFIX_STEP_SERVER_URL}/${AUTOFIX_STEP_REPOSITORY}/actions/runs/${AUTOFIX_STEP_RUN_ID}"
  # A failed reviewer step skips the editor, so its empty output is
  # only the symptom (PR #4323: every reviewer slot and the
  # summariser exited 226 and the run was reported as
  # editor_empty_noop). Name the earliest failing phase and keep the
  # slot / summariser exit codes as evidence; the retry handling
  # below is unchanged.
  autofix_empty_failure_reason="editor_empty_noop"
  noop_explanation="The editor stage completed without a structured summary and without committing any file changes. This typically indicates a transient agent crash, timeout, or empty prompt rather than a reviewable defect in the PR."
  if [ "${REVIEWERS_STEP_OUTCOME:-}" = "failure" ]; then
    autofix_empty_failure_reason="reviewers_failed"
    noop_explanation="The reviewer stage failed, so the editor never ran and no file changes were committed. The reviewer and summariser exit codes are in the workflow run log."
    echo "AUTOFIX_REVIEWERS_FAILED=true" >> "$GITHUB_ENV"
    if [ -n "${AUTOFIX_FAILURE_HEAL_PY:-}" ] && [ -f "${AUTOFIX_FAILURE_HEAL_PY}" ] && [ -n "${RUNTIME_DIR:-}" ]; then
      reviewers_failure_log_args=()
      for reviewers_failure_log in "${PREVIOUS_REVIEWS_DIR:-/nonexistent}"/*.log "${RUNTIME_DIR}"/summariser_*.log; do
        [ -f "${reviewers_failure_log}" ] && reviewers_failure_log_args+=(--log-file "${reviewers_failure_log}")
      done
      PYTHONDONTWRITEBYTECODE=1 python3 "${AUTOFIX_FAILURE_HEAL_PY}" reviewer-failure-evidence "${reviewers_failure_log_args[@]}" \
        > "${RUNTIME_DIR}/reviewers_failure_evidence.txt" 2>/dev/null || : > "${RUNTIME_DIR}/reviewers_failure_evidence.txt"
      echo "AUTOFIX_REVIEWERS_FAILED reason=reviewers_failed $(grep -m1 '^dominant_rc=' "${RUNTIME_DIR}/reviewers_failure_evidence.txt" 2>/dev/null || echo dominant_rc=unknown)"
    fi
  fi
  # review-autofix-failure:v1 marker for the gate's identical-failure
  # fingerprint cap. Same evidence files and reason precedence as
  # "Assemble failure evidence", so every failure comment of one run
  # carries one fingerprint. Empty when the helper is unavailable.
  AUTOFIX_FAILURE_MARKER_SUFFIX=""
  if [ -n "${AUTOFIX_FAILURE_HEAL_PY:-}" ] && [ -f "${AUTOFIX_FAILURE_HEAL_PY}" ]; then
    autofix_failure_marker_line="$(PYTHONDONTWRITEBYTECODE=1 python3 "${AUTOFIX_FAILURE_HEAL_PY}" autofix-failure-fingerprint \
      --failure-reason "${autofix_empty_failure_reason}" \
      --summary-line-file "${RUNTIME_DIR:-}/review_autofix_run_summary_line.txt" \
      --evidence-file "${RUNTIME_DIR:-}/reviewers_failure_evidence.txt" \
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
  NOOP_BODY="$(cat <<EOF
**AI review/autofix produced no output — will retry**

${noop_explanation} No \`ai:review-blocked\` label is being applied for this run.

[View workflow run](${RUN_URL}) for details. The stall poller will retry the review on a subsequent cycle.
EOF
)"
  NOOP_BODY+="${AUTOFIX_FAILURE_MARKER_SUFFIX}"
  gh_retry gh api "repos/${AUTOFIX_STEP_REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${NOOP_BODY}" >/dev/null 2>&1 || true
  echo "AUTOFIX_EDITOR_EMPTY_NOOP=true" >> "$GITHUB_ENV"
  exit 1
fi

if [ ! -s "${EDITOR_SUMMARY_FILE}" ]; then
  cat > "${EDITOR_SUMMARY_FILE}" <<'__EDITOR_SUMMARY__'
Changes made:
- none (no editor summary output was generated)

Already satisfied (suggested but already present):
- none (no editor summary output was generated)

Ignored suggestions (with short reason):
- no summary available from editor stage

Reviewer files processed:
- none (no editor summary output was generated)

Review file issue audit:
- none (no editor summary output was generated)

Regression fingerprint:
- unavailable (editor fallback)

Runtime failure path:
- unavailable (editor fallback)
__EDITOR_SUMMARY__
fi

{
  echo "AI autofix editor summary"
  echo
  cat "${EDITOR_SUMMARY_FILE}"
  echo
  echo "Files changed / commit status:"
  if [ -s "${COMMITTED_FILES_FILE}" ]; then
    cat "${COMMITTED_FILES_FILE}"
  else
    echo "- none"
  fi
  echo
} > "${PR_EDITOR_COMMENT_FILE}"

type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

if gh_retry gh api repos/${AUTOFIX_STEP_REPOSITORY}/issues/"${PR_NUMBER}"/comments -f body="$(cat "${PR_EDITOR_COMMENT_FILE}")" >/dev/null; then
  echo "Posted editor summary comment."
  # Signal to the failure-comment step (line ~3269) that the
  # editor summary already landed on the PR thread. The generic
  # "AI review/autofix failed" body speculates about editor /
  # dependency / infra failure modes and would contradict the
  # success-looking summary above when the editor in fact ran
  # to completion and a later step (push, conflict resolver,
  # auto-merge config, telemetry) is what actually failed.
  # Set only on a successful post; on retry exhaustion the
  # editor summary is not visible to the reader, so the
  # generic body is the right thing to post. See PR #1476
  # follow-up for the contradictory-comment regression this
  # signal resolves.
  echo "EDITOR_SUMMARY_POSTED=true" >> "$GITHUB_ENV"
else
  echo "::warning::Unable to post editor summary comment after retries."
fi
