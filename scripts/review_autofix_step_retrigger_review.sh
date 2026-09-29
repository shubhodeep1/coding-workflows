#!/usr/bin/env bash
# Body of the "Re-trigger review via workflow_dispatch" step in
# .github/workflows/review_autofix.yml. The step sources this file in its own
# shell, so the step's if:, env: and continue-on-error: stay in the workflow;
# edit those there. Moved verbatim to keep the workflow under the
# per-phase size cap (CLAUDE.md §27); the one GitHub expression it used,
# `github.workflow_ref`, arrives through the step's env as
# REVIEW_RETRIGGER_CALLER_WORKFLOW_REF.
set -euo pipefail
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true

# Self-triggered autofix skip (mirrors gate-job behavior):
# The gate job suppresses self-triggered autofix follow-up runs for
# pull_request.synchronize events (by matching subject [ai-autofix]
# against AUTOFIX_BOT_LOGIN-attributed .author.login/.committer.login),
# but workflow_dispatch does not go through that same event filter.
# Without this guard, the dispatch below could spawn a verification
# run for the autofix we just pushed and effectively bypass that
# suppression.
#
# This guard keys off this run's outcome flags, not commit identity:
# skip dispatch when the just-pushed change is a pure [ai-autofix]
# edit (DID_COMMIT=true and CONFLICT_RESOLVED!=true). Since these
# flags are set by the step that just performed the push, there is
# no spoofing vector here — no commit-API identity check required.
# [ai-merge-resolve] / conflict-resolved pushes still dispatch so
# the post-resolution reviewer pass runs (conflict-resolved code
# is higher-risk than a vanilla autofix edit).
#
# Autofix continuation (probably_unnecessary_but_read_if_stuck.md §20.4): when the just-pushed
# [ai-autofix] commit is a productive edit (DID_COMMIT=true,
# LEDGER_ONLY_COMMIT!=true, CONFLICT_RESOLVED!=true) and
# AUTOFIX_CONTINUATION_ENABLED is not `false`, the dispatch
# proceeds even though the gate job would suppress the parallel
# pull_request.synchronize event.  This closes the ~0–120 min
# stall window where a PR otherwise had to wait for the
# orchestrator stall cron before its next autofix iteration —
# critical for non-orchestrator PRs that the cron does not
# scan.  Ledger-only commits are still skipped here (the
# clean-review tail in the same run handles auto-merge, so no
# continuation is needed).  Setting
# AUTOFIX_CONTINUATION_ENABLED=false restores the
# pre-continuation path, where productive autofix commits skip
# this step only when AUTOFIX_SKIP_SELF_TRIGGERED=true.
continuation_enabled="${AUTOFIX_CONTINUATION_ENABLED:-true}"
is_ledger_only_commit="${LEDGER_ONLY_COMMIT:-false}"
if [ "${DID_COMMIT:-false}" = "true" ] \
  && [ "${CONFLICT_RESOLVED:-false}" != "true" ] \
  && { [ "${is_ledger_only_commit}" = "true" ] \
    || { [ "${AUTOFIX_SKIP_SELF_TRIGGERED:-false}" = "true" ] \
      && [ "${continuation_enabled}" = "false" ]; }; }; then
  echo "AUTOFIX_DISPATCH_SKIPPED reason=self_triggered_autofix pr=${PR_NUMBER} current_run=${CURRENT_RUN_ID} source=post_commit_retrigger continuation_enabled=${continuation_enabled} ledger_only=${is_ledger_only_commit}"
  emit_event "AUTOFIX_DISPATCH_SKIPPED" "reason=self_triggered_autofix" "pr=${PR_NUMBER}" "current_run=${CURRENT_RUN_ID}" "source=post_commit_retrigger" "continuation_enabled=${continuation_enabled}" "ledger_only=${is_ledger_only_commit}"
  exit 0
fi

# Continuation-specific pre-dispatch guards.  Only applied when
# the commit was a productive autofix and continuation is
# enabled; conflict-resolved and legacy pass-through paths
# retain their existing (pre-continuation) dispatch behaviour.
is_productive_autofix_commit="false"
if [ "${DID_COMMIT:-false}" = "true" ] \
  && [ "${CONFLICT_RESOLVED:-false}" != "true" ] \
  && [ "${is_ledger_only_commit}" != "true" ]; then
  is_productive_autofix_commit="true"
fi
is_continuation_dispatch="false"
if [ "${is_productive_autofix_commit}" = "true" ] && [ "${continuation_enabled}" != "false" ]; then
  # Settle delay: allow the push to propagate through GitHub's
  # indices before dispatching a run that will check out the
  # new HEAD SHA.  Tunable via
  # AUTOFIX_CONTINUATION_SETTLE_SECS (default 10, clamped 1..60).
  settle_secs="${AUTOFIX_CONTINUATION_SETTLE_SECS:-10}"
  if ! [[ "${settle_secs}" =~ ^[1-9][0-9]*$ ]] || [ "${settle_secs}" -gt 60 ]; then
    settle_secs=10
  fi
  sleep "${settle_secs}"
  is_continuation_dispatch="true"
fi

# Preflight dedup: the push above fires a pull_request.synchronize
# event that produces its own review_autofix run via the caller
# workflow.  Both runs land in the `pr-autofix-${PR}` concurrency
# group with cancel-in-progress: false, so a redundant dispatch can
# still queue, appear in the Actions UI, and consume a
# workflow_dispatch API call.  Give the synchronize event a few
# seconds to show up in the Actions API, then skip our dispatch if a
# peer run is already queued or running.  Fails open — any detection
# failure falls through to the original dispatch logic so we never
# silently break the cycle.
#
# Tunable via AUTOFIX_RETRIGGER_PEER_WAIT_SECS (default 8s).
peer_wait="${AUTOFIX_RETRIGGER_PEER_WAIT_SECS:-8}"
if ! [[ "${peer_wait}" =~ ^[0-9]+$ ]] || [ "${peer_wait}" -gt 60 ]; then
  peer_wait=8
fi
sleep "${peer_wait}"

# Continuation dispatches are the designated successor run for a
# productive [ai-autofix] push. The only same-branch peer the
# dedup can find is the gate-skipped pull_request.synchronize run
# produced by our own push (AUTOFIX_GATE_SKIP
# reason=self_triggered_autofix), whose `run` job is gated out
# because its `needs.gate.outputs.should_run == 'true'` condition
# is not met, so it never advances retrigger_guard or invokes
# rb_judge. Handing the iteration to that peer stalls the cycle
# until the orchestrator stall cron recovers — and the cron does
# not scan non-orchestrator PRs at all (probably_unnecessary_but_read_if_stuck.md §20.4). Bypass
# the peer check in the continuation case; legacy non-continuation
# dispatches keep their existing dedup semantics.
if [ "${is_continuation_dispatch}" = "true" ]; then
  echo "AUTOFIX_PEER_CHECK_BYPASSED pr=${PR_NUMBER} current_run=${CURRENT_RUN_ID} reason=continuation_dispatch source=post_commit_retrigger"
elif type autofix_retrigger_has_inflight_peer >/dev/null 2>&1 \
  && autofix_retrigger_has_inflight_peer "${PR_NUMBER}" "${TARGET_BRANCH}" "${CURRENT_RUN_ID}"; then
  echo "AUTOFIX_DISPATCH_SKIPPED reason=sync_event_inflight pr=${PR_NUMBER} current_run=${CURRENT_RUN_ID} source=post_commit_retrigger"
  emit_event "AUTOFIX_DISPATCH_SKIPPED" "reason=sync_event_inflight" "pr=${PR_NUMBER}" "current_run=${CURRENT_RUN_ID}" "source=post_commit_retrigger"
  exit 0
fi

if [ "${is_continuation_dispatch}" = "true" ]; then
  echo "AUTOFIX_CONTINUATION_DISPATCH_ISSUED pr=${PR_NUMBER} current_run=${CURRENT_RUN_ID} settle_secs=${settle_secs} source=post_commit_retrigger"
  echo "AUTOFIX_DISPATCH_ISSUED reason=no_peer_detected pr=${PR_NUMBER} current_run=${CURRENT_RUN_ID} source=post_commit_retrigger continuation=true"
  emit_event "AUTOFIX_DISPATCH_ISSUED" "reason=no_peer_detected" "pr=${PR_NUMBER}" "current_run=${CURRENT_RUN_ID}" "source=post_commit_retrigger" "continuation=true"
else
  echo "AUTOFIX_DISPATCH_ISSUED reason=no_peer_detected pr=${PR_NUMBER} current_run=${CURRENT_RUN_ID} source=post_commit_retrigger"
  emit_event "AUTOFIX_DISPATCH_ISSUED" "reason=no_peer_detected" "pr=${PR_NUMBER}" "current_run=${CURRENT_RUN_ID}" "source=post_commit_retrigger"
fi

dispatched=false

# 1. Try dispatching review_autofix.yml directly.  This works for
#    coding-workflows itself (the file exists locally) and for any
#    consumer repo that copies or syncs this file.
if gh workflow run "review_autofix.yml" \
  --ref "${TARGET_BRANCH}" \
  -f pr_number="${PR_NUMBER}" \
  -f allow_workflow_edits="${ALLOW_WORKFLOW_EDITS}"; then
  echo "Dispatched review_autofix.yml on ${TARGET_BRANCH} for PR #${PR_NUMBER}."
  dispatched=true
fi

# 2. Fall back to the caller workflow (e.g. ai-review.yml in a
#    consumer repo).  Only attempt if the direct dispatch failed
#    and the caller is a different file.
if [ "${dispatched}" = "false" ]; then
  caller_ref="${REVIEW_RETRIGGER_CALLER_WORKFLOW_REF}"
  caller_workflow="$(basename "${caller_ref%%@*}")"
  if [ -z "${caller_workflow}" ]; then
    caller_workflow="internal-review.yml"
  fi
  if [ "${caller_workflow}" != "review_autofix.yml" ]; then
    echo "Direct dispatch failed; trying caller workflow: ${caller_workflow}"
    if gh workflow run "${caller_workflow}" \
      --ref "${TARGET_BRANCH}" \
      -f pr_number="${PR_NUMBER}" \
      -f allow_workflow_edits="${ALLOW_WORKFLOW_EDITS}"; then
      echo "Dispatched ${caller_workflow} on ${TARGET_BRANCH} for PR #${PR_NUMBER}."
      dispatched=true
    fi
  else
    for candidate in ai-review.yml internal-review.yml; do
      echo "Direct dispatch failed; trying fallback workflow: ${candidate}"
      if gh workflow run "${candidate}" \
        --ref "${TARGET_BRANCH}" \
        -f pr_number="${PR_NUMBER}" \
        -f allow_workflow_edits="${ALLOW_WORKFLOW_EDITS}"; then
        echo "Dispatched ${candidate} on ${TARGET_BRANCH} for PR #${PR_NUMBER}."
        dispatched=true
        break
      fi
    done
  fi
fi

if [ "${dispatched}" = "false" ]; then
  echo "::warning::Could not dispatch review workflow via workflow_dispatch; the synchronize event from the push will trigger the next review run instead. To enable dispatch (avoids merge-ref resolution issues), add a workflow_dispatch trigger with a pr_number input and an optional allow_workflow_edits boolean input to your caller workflow. Ensure GH_PAT can dispatch workflows (classic PAT: repo+workflow; fine-grained PAT: Actions read/write)."
fi
