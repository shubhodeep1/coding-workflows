#!/usr/bin/env bash
# shellcheck shell=bash
# Body of the "Phase 7: Close PR and verify cancel_on_pr_close fires" step of the e2e-smoke-test job in
# .github/workflows/test-and-mark-stable.yml, moved here verbatim (#9794)
# to keep that workflow under the 430,000-byte headroom check
# (tests/test_test_and_mark_stable_step_scripts_contract.py). The step keeps
# its name, id, if and env in the workflow; its run: block checks
# STABLE_E2E_STEP_SCRIPTS_VERIFIED and sources a github.sha-pinned copy
# of this file from RUNNER_TEMP, so exit/GITHUB_OUTPUT act on the step.
set -euo pipefail
BRANCH="ai/issue-${ISSUE_NUMBER}"

# List internal-cancel-on-pr-close runs into PHASE7_CANCEL_RUNS_JSON.
# One transient GitHub error must not fail Phase 7: run 36389883126
# got a single HTTP 502 while the cancel run it was polling was
# already in progress, and the step exited lookup_failed. Retry up to
# 3 times, 5 s apart; only 3 consecutive failures return 1, and the
# caller then reports lookup_failed as before.
phase7_list_cancel_runs() {
  local per_page="$1"
  local _attempt
  for _attempt in 1 2 3; do
    if PHASE7_CANCEL_RUNS_JSON=$(gh api \
      "repos/${TEST_REPO}/actions/workflows/internal-cancel-on-pr-close.yml/runs?per_page=${per_page}"); then
      return 0
    fi
    if [ "${_attempt}" -lt 3 ]; then
      echo "::warning::Listing internal-cancel-on-pr-close runs failed (attempt ${_attempt}/3); retrying in 5s."
      sleep 5
    fi
  done
  return 1
}

# ── Phase 7 wait deadline helpers (#9794) ──
# PHASE7_WAIT_BUDGET_MINUTES counts from the first poll that sees the
# cancel run out of queued/pending/waiting/requested, so time spent
# waiting for a runner no longer uses it up: in release gate run
# 44490333814 the cancel run was still queued when the 10m budget ran
# out. Until a run is found the budget counts from the start of the
# wait, as before. A run that never leaves the queue is waited on for
# at most PHASE7_QUEUE_WAIT_CAP_MINUTES from the start of the wait.
# Every deadline also stops E2E_FINALIZATION_RESERVE_MINUTES before
# the job timeout, but never earlier than the old start + budget.
phase7_run_is_queued() {
  case "${1:-}" in
    queued|pending|waiting|requested) return 0 ;;
  esac
  return 1
}

# phase7_wait_deadline <wait_start_epoch> <run_status>: sets
# PHASE7_WAIT_DEADLINE, and PHASE7_RUN_STARTED_AT on the first
# non-queued status. An empty status (no run yet, or a failed read)
# never starts the budget clock.
phase7_wait_deadline() {
  local wait_start="$1" run_status="${2:-}" now budget_secs queue_secs floor job_cap
  now=$(date +%s)
  budget_secs=$(( PHASE7_WAIT_BUDGET_MINUTES * 60 ))
  queue_secs=$(( ${PHASE7_QUEUE_WAIT_CAP_MINUTES:-60} * 60 ))
  floor=$(( wait_start + budget_secs ))
  if [ -n "${run_status}" ] && ! phase7_run_is_queued "${run_status}" && [ -z "${PHASE7_RUN_STARTED_AT:-}" ]; then
    PHASE7_RUN_STARTED_AT="${now}"
  fi
  if [ -n "${PHASE7_RUN_STARTED_AT:-}" ]; then
    PHASE7_WAIT_DEADLINE=$(( PHASE7_RUN_STARTED_AT + budget_secs ))
  elif phase7_run_is_queued "${run_status}"; then
    PHASE7_WAIT_DEADLINE=$(( wait_start + queue_secs ))
  else
    PHASE7_WAIT_DEADLINE="${floor}"
  fi
  if [[ "${E2E_JOB_STARTED_EPOCH:-}" =~ ^[0-9]+$ ]] && [[ "${E2E_JOB_TIMEOUT_MINUTES:-}" =~ ^[0-9]+$ ]]; then
    job_cap=$(( E2E_JOB_STARTED_EPOCH + (E2E_JOB_TIMEOUT_MINUTES - ${E2E_FINALIZATION_RESERVE_MINUTES:-20}) * 60 ))
    if [ "${PHASE7_WAIT_DEADLINE}" -gt "${job_cap}" ]; then
      PHASE7_WAIT_DEADLINE="${job_cap}"
    fi
  fi
  if [ "${PHASE7_WAIT_DEADLINE}" -lt "${floor}" ]; then
    PHASE7_WAIT_DEADLINE="${floor}"
  fi
}
# ── end Phase 7 wait deadline helpers ──
if ! [[ "${PHASE7_QUEUE_WAIT_CAP_MINUTES:-60}" =~ ^[0-9]+$ ]]; then
  echo "::warning::PHASE7_QUEUE_WAIT_CAP_MINUTES is not a whole number; using 60."
  PHASE7_QUEUE_WAIT_CAP_MINUTES=60
fi
PHASE7_RUN_STARTED_AT=""

# The PR may already be closed/merged by the time Phase 7 runs —
# earlier phases (orchestrator auto-merge after review approval)
# can close it asynchronously. PATCH-ing state=closed on an
# already-closed PR is a no-op and fires no `pull_request.closed`
# webhook, so the poll below would time out even though
# cancel_on_pr_close already ran successfully on the original
# close. Detect that case and verify the cancel run that fired
# for the prior close instead.
PR_INFO=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" \
  --jq '{state, head_sha: .head.sha, closed_at, mergeable, mergeable_state}' 2>/dev/null) || PR_INFO="{}"
PR_STATE=$(echo "${PR_INFO}" | jq -r '.state // ""')
PR_HEAD_SHA=$(echo "${PR_INFO}" | jq -r '.head_sha // ""')
PR_CLOSED_AT=$(echo "${PR_INFO}" | jq -r '.closed_at // ""')
PR_MERGEABLE=$(echo "${PR_INFO}" | jq -r 'if .mergeable == null then "unknown" else (.mergeable | tostring) end')
PR_MERGEABLE_STATE=$(echo "${PR_INFO}" | jq -r '.mergeable_state // "unknown"')
if [ -z "${PR_STATE}" ]; then
  echo "::error::Could not read state of PR #${PR_NUMBER} (gh api returned empty)"
  echo "status=lookup_failed" >> "$GITHUB_OUTPUT"
  exit 1
fi
echo "Pre-close PR diagnostics: mergeable=${PR_MERGEABLE}, mergeable_state=${PR_MERGEABLE_STATE}"
if [ "${PR_STATE}" = "closed" ]; then
  if ! [[ "${PR_HEAD_SHA}" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "::error::Closed PR head is not a valid 40-hex SHA."
    echo "status=lookup_failed" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  echo "PR #${PR_NUMBER} is already closed (head_sha=${PR_HEAD_SHA}); looking up the cancel-on-close run that fired for that closure."
  if [ -z "${PR_CLOSED_AT}" ]; then
    echo "::error::Closed PR #${PR_NUMBER} has no closed_at timestamp."
    echo "status=lookup_failed" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  if ! phase7_list_cancel_runs 50; then
    echo "::error::Could not list internal-cancel-on-pr-close runs for already-closed PR #${PR_NUMBER}."
    echo "status=lookup_failed" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  EXISTING_RUNS_JSON="${PHASE7_CANCEL_RUNS_JSON}"
  EXISTING=$(printf '%s' "${EXISTING_RUNS_JSON}" | jq -c \
    --arg head_sha "${PR_HEAD_SHA}" \
    --arg closed_at "${PR_CLOSED_AT}" \
    '[.workflow_runs[]
      | select(
          (.event == "pull_request" and .head_sha == $head_sha) or
          (.event == "schedule" and .created_at >= $closed_at)
        )]
      | sort_by(.created_at)
      | last')
  if [ -z "${EXISTING}" ] || [ "${EXISTING}" = "null" ]; then
    echo "::error::PR #${PR_NUMBER} already closed but no event or scheduled internal-cancel-on-pr-close run was found after closed_at=${PR_CLOSED_AT}"
    echo "status=no_run" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  EXISTING_RUN_ID=$(echo "${EXISTING}" | jq -r '.id // ""')
  EXISTING_STATUS=$(echo "${EXISTING}" | jq -r '.status // ""')
  EXISTING_CONCLUSION=$(echo "${EXISTING}" | jq -r '.conclusion // ""')
  echo "  found pre-existing cancel-on-close run #${EXISTING_RUN_ID}: status=${EXISTING_STATUS}, conclusion=${EXISTING_CONCLUSION}"
  # Match the open-PR poll budget so a slow/queued run
  # doesn't flake the smoke test if it would have eventually
  # succeeded.
  # The budget starts once the run leaves the queue (see the deadline
  # helpers above).
  PHASE7_WAIT_START=$(date +%s)
  PHASE7_KNOWN_EXISTING_STATUS="${EXISTING_STATUS}"
  phase7_wait_deadline "${PHASE7_WAIT_START}" "${EXISTING_STATUS}"
  WAIT_DEADLINE="${PHASE7_WAIT_DEADLINE}"
  while [ "${EXISTING_STATUS}" != "completed" ] && [ "$(date +%s)" -lt "${WAIT_DEADLINE}" ]; do
    # Poll a queued run every 15 s: the queue cap allows up to 60m of
    # waiting, often while API limits are tight (run 44490333814).
    if phase7_run_is_queued "${EXISTING_STATUS}"; then
      sleep 15
    else
      sleep 5
    fi
    # Fetch once, then parse locally. A transient failure still
    # produces an empty string rather than malformed JSON for the
    # local jq parses below.
    EXISTING_RUN_JSON=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" 2>/dev/null) || EXISTING_RUN_JSON=""
    EXISTING_STATUS=$(printf '%s' "${EXISTING_RUN_JSON}" | jq -r '.status // ""' 2>/dev/null || echo "")
    EXISTING_CONCLUSION=$(printf '%s' "${EXISTING_RUN_JSON}" | jq -r '.conclusion // ""' 2>/dev/null || echo "")
    echo "  cancel-on-close run #${EXISTING_RUN_ID}: status=${EXISTING_STATUS:-?}, conclusion=${EXISTING_CONCLUSION:-?}"
    # A failed read must not reset the deadline to the plain budget or hide
    # a still-queued run from the queued_timeout check: keep the last status.
    if [ -z "${EXISTING_STATUS}" ]; then
      EXISTING_STATUS="${PHASE7_KNOWN_EXISTING_STATUS:-}"
    fi
    PHASE7_KNOWN_EXISTING_STATUS="${EXISTING_STATUS}"
    phase7_wait_deadline "${PHASE7_WAIT_START}" "${EXISTING_STATUS}"
    WAIT_DEADLINE="${PHASE7_WAIT_DEADLINE}"
  done
  if [ -z "${PHASE7_RUN_STARTED_AT:-}" ] && phase7_run_is_queued "${EXISTING_STATUS}"; then
    echo "::error::cancel-on-close run #${EXISTING_RUN_ID} never left the queue within $(( (WAIT_DEADLINE - PHASE7_WAIT_START) / 60 ))m (status=${EXISTING_STATUS}); no runner picked it up"
    echo "status=queued_timeout" >> "$GITHUB_OUTPUT"
    echo "cancel_run_id=${EXISTING_RUN_ID}" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  if [ "${EXISTING_STATUS}" != "completed" ]; then
    echo "::error::cancel-on-close run #${EXISTING_RUN_ID} did not complete within ${PHASE7_WAIT_BUDGET_MINUTES}m (status=${EXISTING_STATUS:-?})"
    echo "status=timeout" >> "$GITHUB_OUTPUT"
    echo "cancel_run_id=${EXISTING_RUN_ID}" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  if [ "${EXISTING_CONCLUSION}" != "success" ]; then
    echo "::error::cancel-on-close run #${EXISTING_RUN_ID} concluded ${EXISTING_CONCLUSION:-no_run}"
    echo "status=${EXISTING_CONCLUSION:-no_run}" >> "$GITHUB_OUTPUT"
    echo "cancel_run_id=${EXISTING_RUN_ID}" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  echo "✓ cancel-on-close run #${EXISTING_RUN_ID} succeeded (fired on prior PR close)"
  echo "status=success" >> "$GITHUB_OUTPUT"
  echo "cancel_run_id=${EXISTING_RUN_ID}" >> "$GITHUB_OUTPUT"
  exit 0
fi

# PR is still open — exercise the close path end to end.

# ── Phase 7a: bring a conflicted smoke PR back to mergeable ──────
# GitHub never runs `pull_request` workflows for a PR that has a
# merge conflict (docs: events-that-trigger-workflows), so closing a
# conflicted smoke PR emits no `pull_request.closed` run and Phase 7
# fails `no_run` although the cancel wrapper is healthy. Run
# 35672590166 hit exactly that: the forward-merge of stable into
# main landed 75 s before the close and rewrote the same hunk of
# then-tracked .ai/.workspace_source_manifest.txt that the smoke PR's
# workspace init had regenerated. New smoke PRs no longer commit it.
# The scheduled cleanup sweep (#4258) is the
# production backstop for such closures, not a substitute for the
# close trigger, and its observed cadence (median ~12 min) exceeds
# PHASE7_WAIT_BUDGET_MINUTES, so a conflicted PR would still fail.
#
# Before closing, merge the PR's base into the throwaway ai/issue-N
# branch through the merges API. On a 409, overwrite every PR file
# that the base also changed since the merge base with the base's
# version (the smoke PR's own content is no longer load-bearing once
# Phase 4b has verified the canary), retry the merge once, then wait
# for GitHub to recompute mergeability. A branch that still cannot be
# made mergeable is closed anyway and left to the scheduled fallback;
# the outcome is recorded either way so a later `no_run` is
# diagnosable from the log (PHASE7_UNCONFLICT_* lines).
#
# API calls (§15): one mergeability poll (≤18 calls at 5 s while
# GitHub reports mergeable=null), one `/merges` (two on conflict),
# and on conflict one `/compare`, one `/pulls/N/files` page, and up
# to four calls per overlapping file (base contents, merge-base blob,
# branch blob, PUT). The snapshot and close calls below are unchanged.
PHASE7_UNCONFLICT_OUTCOME="not_needed"
PHASE7_BASE_REF=""
PHASE7_MERGEABLE="unknown"
PHASE7_MERGEABLE_STATE="unknown"
_phase7_refresh_mergeability()
{
  # Sets PHASE7_MERGEABLE / PHASE7_MERGEABLE_STATE / PHASE7_BASE_REF,
  # polling while GitHub still reports mergeable=null (it recomputes
  # lazily after any push to the base or the head).
  local deadline info
  deadline=$(( $(date +%s) + 90 ))
  while :; do
    info=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" \
      --jq '{mergeable, mergeable_state, base_ref: .base.ref}' 2>/dev/null) || info="{}"
    PHASE7_MERGEABLE=$(printf '%s' "${info}" | jq -r 'if .mergeable == null then "unknown" else (.mergeable | tostring) end')
    PHASE7_MERGEABLE_STATE=$(printf '%s' "${info}" | jq -r '.mergeable_state // "unknown"')
    PHASE7_BASE_REF=$(printf '%s' "${info}" | jq -r '.base_ref // ""')
    if [ "${PHASE7_MERGEABLE}" != "unknown" ] || [ "$(date +%s)" -ge "${deadline}" ]; then
      return 0
    fi
    sleep 5
  done
}
_phase7_merge_base_into_branch()
{
  # 0 = merged or already up to date, 2 = merge conflict, 1 = other error.
  local err
  if err=$(gh api "repos/${TEST_REPO}/merges" \
      -f base="${BRANCH}" -f head="${PHASE7_BASE_REF}" \
      -f commit_message="[E2E Smoke Test] merge ${PHASE7_BASE_REF} into ${BRANCH} before close (run ${GITHUB_RUN_ID})" \
      2>&1 >/dev/null); then
    return 0
  fi
  if printf '%s' "${err}" | grep -Eqi 'HTTP 409|status code 409|conflict'; then
    return 2
  fi
  echo "::warning::PHASE7_UNCONFLICT merge of ${PHASE7_BASE_REF} into ${BRANCH} failed with a non-conflict error: ${err}"
  return 1
}
_phase7_take_base_version()
{
  # Overwrite one PR file on the branch with the base's version,
  # taken from the contents JSON the caller already fetched (one
  # base read per file, reused for the blob comparison and the
  # payload). The commit subject is prefixed `[E2E Smoke Test]`,
  # never `[ai-autofix]`, for the same reason as the bait commit.
  local path="$1" base_json="$2" branch_sha content
  local -a sha_args=()
  content=$(printf '%s' "${base_json}" | jq -r '.content // empty' 2>/dev/null | tr -d '\n')
  if [ -z "${content}" ]; then
    echo "PHASE7_UNCONFLICT_FILE path=${path} outcome=skipped reason=base_content_unavailable"
    return 1
  fi
  branch_sha=$(gh api "repos/${TEST_REPO}/contents/${path}?ref=${BRANCH}" --jq '.sha // empty' 2>/dev/null) || branch_sha=""
  if [ -n "${branch_sha}" ]; then
    sha_args=(-f "sha=${branch_sha}")
  fi
  if ! gh api -X PUT "repos/${TEST_REPO}/contents/${path}" \
      -f message="[E2E Smoke Test] take ${PHASE7_BASE_REF} version of ${path} to clear the merge conflict (run ${GITHUB_RUN_ID})" \
      -f content="${content}" \
      "${sha_args[@]}" \
      -f branch="${BRANCH}" >/dev/null 2>&1; then
    echo "PHASE7_UNCONFLICT_FILE path=${path} outcome=put_failed"
    return 1
  fi
  echo "PHASE7_UNCONFLICT_FILE path=${path} outcome=replaced_with_base"
  return 0
}

_phase7_refresh_mergeability
echo "PHASE7_UNCONFLICT_CHECK pr=${PR_NUMBER} branch=${BRANCH} base=${PHASE7_BASE_REF} mergeable=${PHASE7_MERGEABLE} mergeable_state=${PHASE7_MERGEABLE_STATE}"
if [ "${PHASE7_MERGEABLE}" = "false" ] && [ -n "${PHASE7_BASE_REF}" ]; then
  PHASE7_UNCONFLICT_OUTCOME="merge_failed"
  PHASE7_MERGE_RC=0
  _phase7_merge_base_into_branch || PHASE7_MERGE_RC=$?
  if [ "${PHASE7_MERGE_RC}" -eq 2 ]; then
    PHASE7_MERGE_BASE_SHA=$(gh api "repos/${TEST_REPO}/compare/${PHASE7_BASE_REF}...${BRANCH}" \
      --jq '.merge_base_commit.sha // ""' 2>/dev/null) || PHASE7_MERGE_BASE_SHA=""
    PHASE7_PR_FILES=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}/files?per_page=100" \
      --jq '.[].filename' 2>/dev/null) || PHASE7_PR_FILES=""
    while IFS= read -r phase7_path; do
      [ -n "${phase7_path}" ] || continue
      phase7_base_json=$(gh api "repos/${TEST_REPO}/contents/${phase7_path}?ref=${PHASE7_BASE_REF}" 2>/dev/null) || phase7_base_json=""
      phase7_base_blob=$(printf '%s' "${phase7_base_json}" | jq -r '.sha // ""' 2>/dev/null || echo "")
      phase7_merge_base_blob=""
      if [ -n "${PHASE7_MERGE_BASE_SHA}" ]; then
        phase7_merge_base_blob=$(gh api "repos/${TEST_REPO}/contents/${phase7_path}?ref=${PHASE7_MERGE_BASE_SHA}" \
          --jq '.sha // ""' 2>/dev/null) || phase7_merge_base_blob=""
      fi
      if [ -z "${PHASE7_MERGE_BASE_SHA}" ]; then
        echo "PHASE7_UNCONFLICT_FILE path=${phase7_path} outcome=kept reason=merge_base_unavailable"
      elif [ -z "${phase7_merge_base_blob}" ]; then
        echo "PHASE7_UNCONFLICT_FILE path=${phase7_path} outcome=kept reason=merge_base_blob_unavailable"
      elif [ -z "${phase7_base_blob}" ]; then
        echo "PHASE7_UNCONFLICT_FILE path=${phase7_path} outcome=kept reason=base_blob_unavailable"
      elif [ -n "${phase7_base_blob}" ] && [ "${phase7_base_blob}" != "${phase7_merge_base_blob}" ]; then
        _phase7_take_base_version "${phase7_path}" "${phase7_base_json}" || true
      else
        echo "PHASE7_UNCONFLICT_FILE path=${phase7_path} outcome=kept reason=base_unchanged_since_merge_base"
      fi
    done <<< "${PHASE7_PR_FILES}"
    PHASE7_MERGE_RC=0
    _phase7_merge_base_into_branch || PHASE7_MERGE_RC=$?
  fi
  if [ "${PHASE7_MERGE_RC}" -eq 0 ]; then
    _phase7_refresh_mergeability
    if [ "${PHASE7_MERGEABLE}" = "true" ]; then
      PHASE7_UNCONFLICT_OUTCOME="unconflicted"
    else
      PHASE7_UNCONFLICT_OUTCOME="still_${PHASE7_MERGEABLE_STATE}"
    fi
  elif [ "${PHASE7_MERGE_RC}" -eq 2 ]; then
    PHASE7_UNCONFLICT_OUTCOME="still_conflicted"
  fi
  echo "PHASE7_UNCONFLICT_RESULT pr=${PR_NUMBER} branch=${BRANCH} outcome=${PHASE7_UNCONFLICT_OUTCOME} mergeable=${PHASE7_MERGEABLE} mergeable_state=${PHASE7_MERGEABLE_STATE}"
  if [ "${PHASE7_UNCONFLICT_OUTCOME}" != "unconflicted" ]; then
    echo "::warning::Smoke PR #${PR_NUMBER} could not be made mergeable before close (outcome=${PHASE7_UNCONFLICT_OUTCOME}); GitHub will not fire pull_request.closed for a conflicted PR, so Phase 7 now depends on the scheduled cleanup fallback."
  fi
elif [ "${PHASE7_MERGEABLE}" != "true" ]; then
  PHASE7_UNCONFLICT_OUTCOME="still_${PHASE7_MERGEABLE_STATE}"
  echo "PHASE7_UNCONFLICT_RESULT pr=${PR_NUMBER} branch=${BRANCH} outcome=${PHASE7_UNCONFLICT_OUTCOME} mergeable=${PHASE7_MERGEABLE} mergeable_state=${PHASE7_MERGEABLE_STATE}"
  echo "::warning::Smoke PR #${PR_NUMBER} mergeability could not be confirmed before close (outcome=${PHASE7_UNCONFLICT_OUTCOME}, base=${PHASE7_BASE_REF:-unknown}); Phase 7 may depend on the scheduled cleanup fallback."
fi
echo "unconflict=${PHASE7_UNCONFLICT_OUTCOME}" >> "$GITHUB_OUTPUT"

# Snapshot the most recent cancel-on-close run BEFORE we close the
# PR so we can detect a fresh run by monotonic ID after the close.
if ! phase7_list_cancel_runs 1; then
  echo "::error::Could not snapshot internal-cancel-on-pr-close runs before closing PR #${PR_NUMBER}."
  echo "status=lookup_failed" >> "$GITHUB_OUTPUT"
  exit 1
fi
PRE_RUNS_JSON="${PHASE7_CANCEL_RUNS_JSON}"
PRE_RUN_ID=$(printf '%s' "${PRE_RUNS_JSON}" | jq -r '.workflow_runs[0].id // 0')
echo "Pre-close cancel run head: ${PRE_RUN_ID}"

# Close the PR (this is what triggers internal-cancel-on-pr-close).
CLOSE_RESPONSE=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" \
  -X PATCH -f state=closed --jq '{closed_at}' 2>/dev/null) \
  || { echo "::error::Could not close PR #${PR_NUMBER}"; echo "status=close_failed" >> "$GITHUB_OUTPUT"; exit 1; }
PR_CLOSED_AT=$(printf '%s' "${CLOSE_RESPONSE}" | jq -r '.closed_at // ""')
if [ -z "${PR_CLOSED_AT}" ]; then
  echo "::error::Close response for PR #${PR_NUMBER} did not include closed_at."
  echo "status=close_failed" >> "$GITHUB_OUTPUT"
  exit 1
fi
echo "✓ Closed PR #${PR_NUMBER}"

# Event-propagation grace: GitHub's webhook delivery for the
# `pull_request.closed` event is usually sub-second but has been
# observed to lag 2-3 seconds under heavy queue load. Sleep 5s
# before the first poll so we don't false-fail by reading the
# `actions/workflows/.../runs` index before the cancel run has
# been created. The named Phase 7 budget below absorbs this.
sleep 5

# Wait for a new cancel-on-close run to register and complete.
# Internal-cancel-on-pr-close fires near-immediately on the
# pull_request[closed] webhook but the API can take a few
# seconds to index the new run. Poll every 3s for the first 30s
# to catch quick runs (the cancel job is short — it only
# cancels other workflows), then wait 10s, 20s and 30s for every
# later poll, never sleeping past DEADLINE. Total budget
# defaults to 10m.
DEADLINE=$(( $(date +%s) + (PHASE7_WAIT_BUDGET_MINUTES * 60) ))
POLL_START=$(date +%s)
NEW_RUN_ID=""
NEW_CONCLUSION=""
PHASE7_POLL_COUNT=0
PHASE7_SLOW_POLL_COUNT=0
while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
  if ! phase7_list_cancel_runs 10; then
    echo "::error::Could not list internal-cancel-on-pr-close runs after closing PR #${PR_NUMBER}."
    echo "status=lookup_failed" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  PHASE7_POLL_COUNT=$(( PHASE7_POLL_COUNT + 1 ))
  CANDIDATE_RUNS_JSON="${PHASE7_CANCEL_RUNS_JSON}"
  CANDIDATE=$(printf '%s' "${CANDIDATE_RUNS_JSON}" | jq -c \
    --argjson pre_run_id "${PRE_RUN_ID}" \
    --arg closed_at "${PR_CLOSED_AT}" \
    '[.workflow_runs[]
      | select(.id > $pre_run_id)
      | select(
          .event == "pull_request" or
          (.event == "schedule" and .created_at >= $closed_at)
        )]
      | sort_by(.created_at)
      | last')
  if [ -n "${CANDIDATE}" ] && [ "${CANDIDATE}" != "null" ]; then
    NEW_RUN_ID=$(echo "${CANDIDATE}" | jq -r '.id // ""')
    STATUS=$(echo "${CANDIDATE}" | jq -r '.status // ""')
    NEW_CONCLUSION=$(echo "${CANDIDATE}" | jq -r '.conclusion // ""')
    echo "  cancel-on-close run #${NEW_RUN_ID}: status=${STATUS}, conclusion=${NEW_CONCLUSION}"
    if [ "${STATUS}" = "completed" ]; then
      break
    fi
  fi
  # Queued time does not use the budget (deadline helpers above).
  phase7_wait_deadline "${POLL_START}" "${STATUS:-}"
  DEADLINE="${PHASE7_WAIT_DEADLINE}"
  ELAPSED=$(( $(date +%s) - POLL_START ))
  if [ "${ELAPSED}" -lt 30 ]; then
    PHASE7_POLL_SLEEP=3
  else
    PHASE7_SLOW_POLL_COUNT=$(( PHASE7_SLOW_POLL_COUNT + 1 ))
    case "${PHASE7_SLOW_POLL_COUNT}" in
      1) PHASE7_POLL_SLEEP=10 ;;
      2) PHASE7_POLL_SLEEP=20 ;;
      *) PHASE7_POLL_SLEEP=30 ;;
    esac
  fi
  PHASE7_POLL_REMAINING=$(( DEADLINE - $(date +%s) ))
  if [ "${PHASE7_POLL_REMAINING}" -le 0 ]; then
    break
  fi
  if [ "${PHASE7_POLL_SLEEP}" -ge "${PHASE7_POLL_REMAINING}" ]; then
    # Wake one second before DEADLINE so the loop makes one last
    # listing instead of exiting straight after the sleep.
    PHASE7_POLL_SLEEP=$(( PHASE7_POLL_REMAINING - 1 ))
  fi
  if [ "${PHASE7_POLL_SLEEP}" -lt 1 ]; then
    # With one second left the clamp gives 0: the listing this
    # iteration just made is the last one inside the budget, so
    # stop instead of sleeping past DEADLINE or listing again.
    break
  fi
  sleep "${PHASE7_POLL_SLEEP}"
done

if [ -z "${NEW_RUN_ID}" ]; then
  # No-run diagnostic. Run fields are data, so control
  # characters become spaces and any line or value that starts
  # with a workflow command is prefixed with [neutralised].
  echo "::group::Phase 7 no-run diagnostic"
  PHASE7_DIAG_ELAPSED=$(( $(date +%s) - POLL_START ))
  printf '%s\n' "pr=${PR_NUMBER} closed_at=${PR_CLOSED_AT} pre_run_id=${PRE_RUN_ID} polls=${PHASE7_POLL_COUNT} elapsed_secs=${PHASE7_DIAG_ELAPSED}" \
    | tr -d '\r' | sed -e 's/^\(::\|##\[\)/[neutralised]\1/' || true
  PHASE7_DIAG_RUNS=""
  if PHASE7_DIAG_RUNS=$(printf '%s' "${CANDIDATE_RUNS_JSON:-}" | jq -r '
      def safe: tostring | gsub("[[:cntrl:]]"; " ") | if startswith("::") then "[neutralised]" + . else . end;
      .workflow_runs[]? | "  run id=\(.id|safe) event=\(.event|safe) status=\(.status|safe) conclusion=\(.conclusion // ""|safe) head_sha=\(.head_sha // ""|safe) created_at=\(.created_at // ""|safe)"' 2>/dev/null); then
    if [ -n "${PHASE7_DIAG_RUNS}" ]; then
      printf '%s\n' "${PHASE7_DIAG_RUNS}" | sed -e 's/^\(::\|##\[\)/[neutralised]\1/' || echo "  runs: unrenderable"
    else
      echo "  runs: none"
    fi
  else
    echo "  runs: unrenderable"
  fi
  # §15: the runs listing above carries no workflow state, so
  # this one read, made only on this failure path, is the only
  # extra call. A failed read prints workflow_state=unknown and
  # never changes the step's outcome.
  PHASE7_CANCEL_WORKFLOW_STATE=$(gh api "repos/${TEST_REPO}/actions/workflows/internal-cancel-on-pr-close.yml" --jq '.state // ""' 2>/dev/null || true)
  if ! [[ "${PHASE7_CANCEL_WORKFLOW_STATE}" =~ ^[a-z_]+$ ]]; then
    PHASE7_CANCEL_WORKFLOW_STATE="unknown"
  fi
  echo "  workflow_state=${PHASE7_CANCEL_WORKFLOW_STATE}"
  echo "::endgroup::"
  echo "::error::internal-cancel-on-pr-close did not register a new run within timeout"
  echo "status=no_run" >> "$GITHUB_OUTPUT"
  exit 1
fi
# A run was found but never reached `completed` inside the budget
# (for example still `pending` behind its concurrency group). This
# used to write an empty `status=`, which the results table showed
# as SKIPPED (run 40794181365). A run that never left the queue gets
# its own status so runner starvation reads apart from a slow run.
if [ -z "${PHASE7_RUN_STARTED_AT:-}" ] && phase7_run_is_queued "${STATUS:-}"; then
  echo "::error::cancel-on-close run #${NEW_RUN_ID} never left the queue within $(( (DEADLINE - POLL_START) / 60 ))m (status=${STATUS}); no runner picked it up"
  echo "status=queued_timeout" >> "$GITHUB_OUTPUT"
  echo "cancel_run_id=${NEW_RUN_ID}" >> "$GITHUB_OUTPUT"
  exit 1
fi
if [ "${STATUS:-}" != "completed" ]; then
  echo "::error::cancel-on-close run #${NEW_RUN_ID} did not complete within ${PHASE7_WAIT_BUDGET_MINUTES}m (status=${STATUS:-?})"
  echo "status=timeout" >> "$GITHUB_OUTPUT"
  echo "cancel_run_id=${NEW_RUN_ID}" >> "$GITHUB_OUTPUT"
  exit 1
fi
if [ "${NEW_CONCLUSION}" != "success" ]; then
  echo "::error::cancel-on-close run #${NEW_RUN_ID} concluded ${NEW_CONCLUSION}"
  echo "status=${NEW_CONCLUSION}" >> "$GITHUB_OUTPUT"
  echo "cancel_run_id=${NEW_RUN_ID}" >> "$GITHUB_OUTPUT"
  exit 1
fi
echo "✓ cancel-on-close run #${NEW_RUN_ID} succeeded"
echo "status=success" >> "$GITHUB_OUTPUT"
echo "cancel_run_id=${NEW_RUN_ID}" >> "$GITHUB_OUTPUT"
