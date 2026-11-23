#!/usr/bin/env bash
# Body of the "Re-dispatch review on editor-changes-lost" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true

echo "Editor changes lost — dispatching fresh review iteration for PR #${PR_NUMBER}."

# Preflight dedup: same rationale as the post-commit retrigger
# above.  When EDITOR_CHANGES_LOST fires the editor produced no
# commit, so no pull_request.synchronize event follows from us —
# but a sibling run from an earlier retrigger or from a user push
# may still be in flight.  Skip dispatch if so; fail open on any
# detection failure.
peer_wait="${AUTOFIX_RETRIGGER_PEER_WAIT_SECS:-8}"
if ! [[ "${peer_wait}" =~ ^[0-9]+$ ]] || [ "${peer_wait}" -gt 60 ]; then
  peer_wait=8
fi
sleep "${peer_wait}"

if type autofix_retrigger_has_inflight_peer >/dev/null 2>&1 \
  && autofix_retrigger_has_inflight_peer "${PR_NUMBER}" "${TARGET_BRANCH}" "${CURRENT_RUN_ID}"; then
  echo "AUTOFIX_DISPATCH_SKIPPED reason=peer_inflight pr=${PR_NUMBER} current_run=${CURRENT_RUN_ID} source=editor_changes_lost_retrigger"
  emit_event "AUTOFIX_DISPATCH_SKIPPED" "reason=peer_inflight" "pr=${PR_NUMBER}" "current_run=${CURRENT_RUN_ID}" "source=editor_changes_lost_retrigger"
  echo "CHANGES_LOST_REDISPATCHED=skipped_peer_inflight" >> "$GITHUB_ENV"
  exit 0
fi

# Loop bound: one automated changes-lost retry per head SHA.  A
# changes-lost run pushes no commit, so the head cannot advance
# between the original run and its retry; if a completed
# non-cancelled review run already exists on this head, this run
# is that retry and must not dispatch again.  Fail closed (skip)
# when the head SHA or the probe is unavailable — that preserves
# the terminal blocked-comment behaviour instead of risking an
# unbounded dispatch loop.
#
# The retry is dispatched from the default branch (issue #4898), so its
# head_sha is the default branch's, not this head. The probe also counts
# completed review runs named for the PR since this head was pushed, bounded
# by the head's first GitHub-recorded run. The head's commit time is still
# passed but no longer used: the PR author sets it (issue #5523). The run's event
# lets it fail closed on a dispatch run that is not named for the PR, whose
# own retry could never count it.
REVIEWED_HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"
REVIEWED_HEAD_COMMIT_EPOCH="$(git log -1 --format=%ct HEAD 2>/dev/null || echo "")"
if [ -z "${REVIEWED_HEAD_SHA}" ] \
  || ! type autofix_changes_lost_head_retry_consumed >/dev/null 2>&1 \
  || autofix_changes_lost_head_retry_consumed "${PR_NUMBER}" "${TARGET_BRANCH}" "${CURRENT_RUN_ID}" "${REVIEWED_HEAD_SHA}" "${REVIEWED_HEAD_COMMIT_EPOCH}" "${GITHUB_EVENT_NAME:-}"; then
  echo "AUTOFIX_DISPATCH_SKIPPED reason=changes_lost_budget_unavailable_or_exhausted pr=${PR_NUMBER} head_sha=${REVIEWED_HEAD_SHA:-unknown} current_run=${CURRENT_RUN_ID} source=editor_changes_lost_retrigger"
  emit_event "AUTOFIX_DISPATCH_SKIPPED" "reason=changes_lost_budget_unavailable_or_exhausted" "pr=${PR_NUMBER}" "current_run=${CURRENT_RUN_ID}" "source=editor_changes_lost_retrigger"
  echo "CHANGES_LOST_REDISPATCHED=skipped_budget_unavailable_or_exhausted" >> "$GITHUB_ENV"
  exit 0
fi

echo "AUTOFIX_DISPATCH_ISSUED reason=no_peer_detected pr=${PR_NUMBER} current_run=${CURRENT_RUN_ID} source=editor_changes_lost_retrigger"
emit_event "AUTOFIX_DISPATCH_ISSUED" "reason=no_peer_detected" "pr=${PR_NUMBER}" "current_run=${CURRENT_RUN_ID}" "source=editor_changes_lost_retrigger"

# --- default-branch review dispatch (issue #4898) ---
# Dispatch from the default branch, never from the PR head (security,
# issue #4898, finding review-dispatches-unmerged-workflow). `--ref
# "${TARGET_BRANCH}"` ran the PR branch's own unmerged copy of the review
# workflow with `secrets: inherit` and write permissions, and after an
# [ai-autofix] push with allow_workflow_edits that copy can carry workflow
# edits the editor just made. With no --ref, GitHub runs the default
# branch's workflow file. review_autofix.yml checks out the PR head from the
# PR's metadata either way, and its pr-autofix-<N> concurrency group is keyed
# by PR number, not by ref. Only a validated PR number and the run's own
# allow_workflow_edits (normalised to true/false) are passed.
#
# Dispatch order: the PR-named review wrappers come first, because a
# default-branch run has the default branch as its head_branch, so only the
# wrapper's run name ("Internal: AI Review & Autofix [pr:<N>]" in this repo,
# "AI Review [pr:<N>]" in consumer repos) lets autofix_retrigger_has_inflight_peer
# and the poller's _pr_named_review_dispatch_runs see it. The caller
# workflow goes first when it is one of them (no failed call in the common
# case), then ai-review.yml and internal-review.yml, then a differently named
# caller workflow (consumer repos with a renamed wrapper, today's fallback),
# and review_autofix.yml last: it has no PR run name (issue #4701 AD-10).
dispatched=false
if ! [[ "${PR_NUMBER:-}" =~ ^[1-9][0-9]*$ ]]; then
  echo "::warning::Invalid PR number '${PR_NUMBER:-}'; not dispatching a review run."
else
  retrigger_allow_workflow_edits="false"
  if [ "${ALLOW_WORKFLOW_EDITS:-}" = "true" ]; then
    retrigger_allow_workflow_edits="true"
  fi
  caller_workflow=""
  if [ -n "${REVIEW_AUTOFIX_CALLER_WORKFLOW_REF:-}" ]; then
    caller_ref="${REVIEW_AUTOFIX_CALLER_WORKFLOW_REF}"
    caller_workflow="$(basename "${caller_ref%%@*}")"
  fi
  if ! [[ "${caller_workflow}" =~ ^[A-Za-z0-9._-]+\.ya?ml$ ]]; then
    caller_workflow="internal-review.yml"
  fi
  retrigger_candidates=()
  case "${caller_workflow}" in
    ai-review.yml|internal-review.yml) retrigger_candidates+=("${caller_workflow}") ;;
  esac
  for candidate in ai-review.yml internal-review.yml; do
    if [ "${candidate}" != "${caller_workflow}" ]; then
      retrigger_candidates+=("${candidate}")
    fi
  done
  case "${caller_workflow}" in
    ai-review.yml|internal-review.yml|review_autofix.yml) ;;
    *) retrigger_candidates+=("${caller_workflow}") ;;
  esac
  retrigger_candidates+=(review_autofix.yml)
  for candidate in "${retrigger_candidates[@]}"; do
    if gh workflow run "${candidate}" \
      -f pr_number="${PR_NUMBER}" \
      -f allow_workflow_edits="${retrigger_allow_workflow_edits}"; then
      echo "Dispatched ${candidate} from the default branch for PR #${PR_NUMBER} (head ${TARGET_BRANCH:-unknown})."
      dispatched=true
      break
    fi
    echo "Dispatch of ${candidate} failed; trying the next review workflow."
  done
fi
# --- end default-branch review dispatch ---

if [ "${dispatched}" = "false" ]; then
  echo "::warning::Could not dispatch review workflow for editor-changes-lost recovery. The next synchronize event or manual re-run is needed."
fi

echo "CHANGES_LOST_REDISPATCHED=${dispatched}" >> "$GITHUB_ENV"
