#!/usr/bin/env bash
# Body of the review_autofix.yml "Post review-blocked comment on PR (workflow
# failure)" step (sourced by the step; moved out to keep the workflow under
# GitHub's 512,000-byte limit). Issue #6633 adds the provider-outage body.
set -euo pipefail
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

RUN_URL="${REVIEW_FAILURE_RUN_URL}"
# Pick a body variant that doesn't contradict an editor
# summary comment when one was already posted.
# EDITOR_SUMMARY_POSTED=true only guarantees the summary
# comment is visible on the PR thread; it does not by
# itself prove editor execution succeeded (fallback summaries
# can also be posted). Keep this branch wording limited to
# that invariant.
if [ "${EDITOR_SUMMARY_POSTED:-false}" = "true" ]; then
  BODY="$(cat <<EOF
**AI review/autofix encountered a post-editor failure — needs human intervention**

An editor summary comment is visible above, but a downstream stage still failed before completion. Possible failure domains include push, conflict resolver, label/auto-merge, or telemetry plumbing.

[View workflow run](${RUN_URL}) for details.

Linked issues have been labeled \`ai:review-blocked\`.
EOF
)"
else
  BODY="$(cat <<EOF
**AI review/autofix failed — needs human intervention**

The review/autofix workflow encountered an error and could not complete. This may be due to an editor failure, missing dependencies, or an infrastructure issue.

[View workflow run](${RUN_URL}) for details.

Linked issues have been labeled \`ai:review-blocked\`.
EOF
)"
fi
# Model-provider outage (issue #6633): no label was applied and the scheduled
# sweep resumes review once the provider answers again.
if [ "${AUTOFIX_FAILURE_REASON:-}" = "provider_unavailable" ]; then
  BODY="$(cat <<EOF
**AI review/autofix paused — model provider unavailable**

The model provider failed (status \`${AUTOFIX_PROVIDER_STATUS:-unknown}\`, confirmed by a live probe). This is a repo-wide outage, not a defect in this PR: no \`ai:review-blocked\` label was applied and this failure does not count toward the identical-failure cap. The outage is tracked on the \`ai:provider-outage\` issue, and review resumes automatically when the provider recovers.

[View workflow run](${RUN_URL}) for details.
EOF
)"
fi
# Name what failed ("Assemble failure evidence"): the failed step and
# its first specific error line, so the cause is readable from the PR.
if [ -n "${AUTOFIX_FAILED_STEP:-}" ] || [ -n "${AUTOFIX_FAILURE_FIRST_ERROR:-}" ]; then
  BODY+=$'\n'
  if [ -n "${AUTOFIX_FAILED_STEP:-}" ]; then
    BODY+=$'\n'"**Failed step:** \`${AUTOFIX_FAILED_STEP//\`/\'}\`"
  fi
  if [ -n "${AUTOFIX_FAILURE_FIRST_ERROR:-}" ]; then
    BODY+=$'\n'"**First error:** \`${AUTOFIX_FAILURE_FIRST_ERROR//\`/\'}\`"
  fi
fi
# review-autofix-failure:v1 marker ("Assemble failure evidence").
if [ -n "${AUTOFIX_FAILURE_MARKER:-}" ]; then
  BODY+=$'\n\n'"${AUTOFIX_FAILURE_MARKER}"
fi

gh_retry gh api "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${BODY}" >/dev/null 2>&1 || true
