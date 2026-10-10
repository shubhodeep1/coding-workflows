#!/usr/bin/env bash
# Body of the "Post review-blocked comment on PR (autofix exhaustion)" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true

RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

# Reason-specific body. The generic "max autofix iterations"
# body posted unconditionally (pre-fix) confused operators
# when the actual refusal was a transient gate — e.g. the
# merge_with_followup self-deadlock observed on tele-funtoken-
# msg-scoring PR #2989, where the judge approved the merge but
# the check-runs gate refused it because the rb_judge's own
# host job (review / codex-agent) was still in_progress.
# judge_skip_reason now carries the proximate cause from
# scripts/review_rb_judge.sh so the comment can explain what
# actually happened. Empty reason = legacy / pre-fix script,
# falls through to the original body.
case "${JUDGE_SKIP_REASON:-}" in
  isolation_unavailable)
    BODY_HEADING="AI review/autofix — judge deferred: isolation unavailable"
    BODY_DETAIL="The judge could not prepare its credential-free sandbox (for example, the PR snapshot exceeded the isolation size limit or support files are missing). It refused to run a host writer on PR content. Linked issues remain labelled \`ai:review-blocked\`; stall recovery will retry the judge. If this repeats on the same head, a human must review."
    ;;
  missing_followup_details)
    BODY_HEADING="AI review/autofix — judge omitted follow-up issue details"
    BODY_DETAIL="The review-blocked judge chose \`merge_with_followup\` but returned an empty \`followup_issue.title\` and/or \`.body\`, so the workflow refused the action rather than merge code without durable tracking for the deferred gap. Stall recovery will re-fire the judge; if this repeats, inspect the judge output in the workflow log."
    ;;
  blocking_check_runs)
    BODY_HEADING="AI review/autofix — merge gated by in-flight check-runs"
    BODY_DETAIL="The review-blocked judge approved this PR but other CI check-runs on the head commit had not yet completed when the merge was attempted. Stall recovery will re-fire the judge after the remaining check-runs settle; no action is required unless the blocking checks themselves are stuck. Linked issues remain labelled \`ai:review-blocked\` so the recovery ladder will retry."
    ;;
  check_runs_query_failed)
    BODY_HEADING="AI review/autofix — judge could not query check-runs"
    BODY_DETAIL="The review-blocked judge could not fetch the check-runs API for the PR head commit and refused the merge rather than land code against unvalidated CI state. This is usually transient; stall recovery will re-fire the judge. If it persists, check the workflow run log for the GitHub API error."
    ;;
  unresolved_head_sha)
    BODY_HEADING="AI review/autofix — judge could not resolve PR head SHA"
    BODY_DETAIL="The review-blocked judge could not resolve the PR head SHA from the GitHub PR JSON and refused the merge to avoid landing unjudged code. Stall recovery will re-fire the judge. If this recurs, the GitHub PR API may be returning a partial payload — check the workflow run log."
    ;;
  sync_merge_failed)
    BODY_HEADING="AI review/autofix — judge approved merge but \`gh pr merge\` failed"
    BODY_DETAIL="The review-blocked judge approved this PR and all check-runs had settled, but the synchronous \`gh pr merge --squash\` call failed. Typical causes: branch-protection rules, a merge queue requirement, missing permissions on the GH token, GitHub returning 422, or a concurrent push that changed HEAD. Stall recovery will re-fire the judge; the operator may need to merge manually if the underlying cause is permission/policy."
    ;;
  auto_merge_disabled)
    BODY_HEADING="AI review/autofix — judge approved merge but auto-merge is disabled"
    BODY_DETAIL="The review-blocked judge approved this PR but \`ENABLE_AUTO_MERGE\` is false in repo configuration, so the judge cannot land the merge itself. Please merge this PR manually; the follow-up tracking issue will be created on the next judge run after the merge lands."
    ;;
  merge_conflict)
    BODY_HEADING="AI review/autofix — PR has merge conflicts"
    BODY_DETAIL="The review-blocked judge could not act because the PR has merge conflicts against its base branch (mergeable=false). Please resolve the conflicts (rebase or merge the base branch) and push; the autofix loop will re-run."
    ;;
  mergeability_pending)
    BODY_HEADING="AI review/autofix — mergeability still computing"
    BODY_DETAIL="The review-blocked judge could not act because GitHub was still computing the PR's mergeability state when the gate ran. Stall recovery will re-fire the judge shortly; no action is required unless the state remains unknown for an extended period."
    ;;
  followup_issue_create_failed)
    BODY_HEADING="AI review/autofix — merge landed but follow-up issue creation failed"
    BODY_DETAIL="The review-blocked judge approved \`merge_with_followup\` and the PR merge succeeded, but GitHub failed while creating the follow-up issue. The deferred gap is not yet tracked; please create the follow-up manually if needed, or let stall recovery / a subsequent judge run retry issue creation."
    ;;
  *)
    BODY_HEADING="AI review/autofix blocked — needs human intervention"
    BODY_DETAIL="This PR has reached the maximum number of autofix iterations (${MAX_AUTOFIX_ITERATIONS}) without resolving all review issues. The remaining problems could not be auto-fixed and require manual attention."
    ;;
esac

BODY="$(cat <<EOF
**${BODY_HEADING}**

${BODY_DETAIL}

**What to do:**
- Review the editor summary comments above for details on what was fixed and what was not
- Address the remaining issues manually if needed
- Push a new commit to re-trigger the review workflow if you have made changes

Linked issues have been labeled \`ai:review-blocked\`.

Run: ${RUN_URL}
EOF
)"

if ! gh api "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${BODY}" >/dev/null; then
  echo "::warning::Failed to post review-blocked comment on PR #${PR_NUMBER}."
fi

# Hand-over (replace-claude-sessions plan Phase 7, Q13): when the
# judge itself gave up (no usable output, an unsafe action it may
# not take, or a follow-up it could not describe), the PR gets
# ai:needs-human so the unblock scan picks it up. Transient gates
# keep retrying through stall recovery without a label.
case "${JUDGE_SKIP_REASON:-}" in
  llm_failed*|json_parse_failed*|missing_followup_details|merged_pr_unsafe_action|auto_merge_disabled)
    if ! gh api -X POST "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels" -f "labels[]=ai:needs-human" >/dev/null; then
      echo "::warning::Failed to add ai:needs-human to PR #${PR_NUMBER} for the unblock judge."
    fi
    echo "UNBLOCK_HANDOVER pr=${PR_NUMBER} stop=rb_judge reason=${JUDGE_SKIP_REASON} outcome=labelled"
    ;;
esac
