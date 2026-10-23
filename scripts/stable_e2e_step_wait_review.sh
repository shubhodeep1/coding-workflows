#!/usr/bin/env bash
# shellcheck shell=bash
# Body of the "Phase 4: Wait for review & autofix to complete" step of the e2e-smoke-test job in
# .github/workflows/test-and-mark-stable.yml, moved here verbatim (#7991)
# to keep that workflow well under the 480,000-byte guard
# (tests/test_workflow_file_size_limit.py). The step keeps its name, id,
# if and env in the workflow; its run: block checks
# STABLE_E2E_STEP_SCRIPTS_VERIFIED and sources a github.sha-pinned copy
# of this file from RUNNER_TEMP, so exit/GITHUB_OUTPUT act on the step.
set -euo pipefail
if [ -n "${BAIT_SHA:-}" ] && ! [[ "${BAIT_SHA}" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "::error::Bait commit output is not a valid 40-hex SHA."
  exit 1
fi
if [ -n "${BAIT_CREATED_AT:-}" ] \
    && ! [[ "${BAIT_CREATED_AT}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
  echo "::error::Bait commit timestamp is not valid UTC ISO-8601."
  exit 1
fi
echo "Waiting for review workflow on PR #${PR_NUMBER}..."
if [ -n "${BAIT_SHA:-}" ]; then
  echo "Pinning to review runs with head_sha=${BAIT_SHA:0:7} (bait commit)"
else
  echo "No bait SHA — using legacy latest-by-created_at filter"
fi
echo "Inactivity timeout: ${REVIEW_TIMEOUT} minutes"
INACTIVITY_LIMIT=$((REVIEW_TIMEOUT * 60))
# Per-step wall-clock cap (see REVIEW_STEP_TIMEOUT env above):
# a single in-progress review step may run this long before the
# inactivity check treats it as a stall, since GitHub does not
# stream in-progress step logs and the cached activity signals
# freeze for the step's whole duration. Fall back to 75m when the
# env is unset/non-numeric so the arithmetic below never errors.
REVIEW_STEP_TIMEOUT="${REVIEW_STEP_TIMEOUT:-75}"
[[ "$REVIEW_STEP_TIMEOUT" =~ ^[0-9]+$ ]] && [ "$REVIEW_STEP_TIMEOUT" -gt 0 ] || REVIEW_STEP_TIMEOUT=75
REVIEW_STEP_TIMEOUT_MAX=90
if [ "$REVIEW_STEP_TIMEOUT" -gt "$REVIEW_STEP_TIMEOUT_MAX" ]; then
  echo "::warning::REVIEW_STEP_TIMEOUT=${REVIEW_STEP_TIMEOUT} exceeds ${REVIEW_STEP_TIMEOUT_MAX}m cap; clamping so wait-review still reports genuine stalls before the job timeout"
  REVIEW_STEP_TIMEOUT="$REVIEW_STEP_TIMEOUT_MAX"
fi
STEP_INACTIVITY_LIMIT=$((REVIEW_STEP_TIMEOUT * 60))
echo "Per-step stall cap: ${REVIEW_STEP_TIMEOUT} minutes"
REVIEW_PHASE_WALL_BUDGET_MINUTES=$((REVIEW_STEP_TIMEOUT + REVIEW_PHASE_HANDOFF_BUDGET_MINUTES))
if [ "$REVIEW_TIMEOUT" -gt "$REVIEW_PHASE_WALL_BUDGET_MINUTES" ]; then
  REVIEW_PHASE_WALL_BUDGET_MINUTES="$REVIEW_TIMEOUT"
fi
REVIEW_PHASE_STARTED_AT=$(date +%s)
REVIEW_PHASE_DEADLINE=$((REVIEW_PHASE_STARTED_AT + (REVIEW_PHASE_WALL_BUDGET_MINUTES * 60)))
echo "Review total wall-clock budget: ${REVIEW_PHASE_WALL_BUDGET_MINUTES} minutes"
LAST_ACTIVITY="$REVIEW_PHASE_STARTED_AT"
PREV_STATE=""
LAST_STEP_GUARD_RUN_ID=""
LAST_STEP_GUARD_NAME=""
LAST_STEP_GUARD_STARTED_AT=""
ELAPSED=0
RATE_LIMIT_BACKOFF=0
# BEGIN live-log cooldown (wfopt-0922-3)
# Run 35696603462 made 275 failed /actions/jobs/{id}/logs fetches in
# about an hour. After LIVE_LOG_FAILURE_THRESHOLD failures in a row,
# skip every live-log fetch for LIVE_LOG_COOLDOWN_SECS of ELAPSED;
# status/conclusion polling continues. A success resets the count.
LIVE_LOG_FETCH_FAILURES=0
LIVE_LOG_COOLDOWN_UNTIL=0
LIVE_LOG_FAILURE_THRESHOLD=3
LIVE_LOG_COOLDOWN_SECS=300
live_log_fetch_allowed() {
  [ "$ELAPSED" -ge "$LIVE_LOG_COOLDOWN_UNTIL" ]
}
live_log_fetch_record() {
  if [ "$1" = "ok" ]; then
    LIVE_LOG_FETCH_FAILURES=0
    return 0
  fi
  LIVE_LOG_FETCH_FAILURES=$((LIVE_LOG_FETCH_FAILURES + 1))
  if [ "$LIVE_LOG_FETCH_FAILURES" -ge "$LIVE_LOG_FAILURE_THRESHOLD" ]; then
    LIVE_LOG_COOLDOWN_UNTIL=$((ELAPSED + LIVE_LOG_COOLDOWN_SECS))
    LIVE_LOG_FETCH_FAILURES=0
    echo "  ⏸ Live-log fetch failed ${LIVE_LOG_FAILURE_THRESHOLD} times in a row; skipping live-log probes for ${LIVE_LOG_COOLDOWN_SECS}s (status polling continues)" >&2
  fi
  return 0
}
# END live-log cooldown (wfopt-0922-3)
# Mutable pin tracking the head SHA the loop is currently polling
# against. Initialised to BAIT_SHA; advances to the new PR head
# whenever the pinned run reports `cancelled` AND the PR head has
# moved past the pin. internal-review.yml uses `cancel-in-progress:
# true` on a `head_ref`-keyed group, so the editor's fix-commit
# push fires a fresh `pull_request: synchronize` event whose
# successor review run cancels the bait-triggered predecessor.
# Without advancing the pin, the BAIT_SHA-anchored jq filter never
# matches the successor (its head_sha is the editor's fix SHA, not
# the bait SHA) and the loop polls the dead cancelled run until
# the 30-minute inactivity timeout fires — observed on
# run-25428461223 (PR #2165), where the editor pushed 8234a6e at
# +10m, the bait run was cancelled at +10m22s, and wait-review
# then idled for the full REVIEW_TIMEOUT before failing.
PIN_SHA="${BAIT_SHA:-}"
BAIT_UNCOVERED_RUN_IDS=""
declare -A BAIT_REVIEW_COVERAGE_CACHE=()
declare -A BAIT_REVIEW_UNKNOWN_COUNTS=()

. ./scripts/comprehensive_test_and_release_gh_api.sh

# The runs API reports the trigger SHA, not the SHA checked out by
# codex-agent. Only a completed job log can prove leg (b) coverage.
# Cache conclusive verdicts; an API/log failure stays retryable.
review_run_checked_out_bait() {
  local coverage_run_id="$1" coverage_jobs="" coverage_job_id=""
  local coverage_log="" coverage_checked="" coverage_status="" coverage_display="none"
  local coverage_verdict="unknown" coverage_reason="jobs_unavailable"
  local coverage_read=0 coverage_failed=0 coverage_cooldown=0
  BAIT_REVIEW_COOLDOWN_SKIP=0
  if ! [[ "$coverage_run_id" =~ ^[0-9]+$ ]]; then
    BAIT_REVIEW_VERDICT="unknown"
    echo "E2E_BAIT_REVIEW_COVERAGE run=invalid verdict=unknown checked_out=none reason=invalid_run_id"
    return
  fi
  if [ -n "${BAIT_REVIEW_COVERAGE_CACHE[$coverage_run_id]:-}" ]; then
    BAIT_REVIEW_VERDICT="${BAIT_REVIEW_COVERAGE_CACHE[$coverage_run_id]}"
    return
  fi
  if [ "$RUN_STATUS" = "in_progress" ] && [ -n "${JOBS_JSON:-}" ] && [ "${RUN_ID:-}" = "$coverage_run_id" ]; then
    coverage_jobs="$JOBS_JSON"
  else
    coverage_jobs=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/actions/runs/${coverage_run_id}/jobs?per_page=10" || echo "")
  fi
  if [ -n "$coverage_jobs" ] && printf '%s' "$coverage_jobs" | jq -e '(.jobs | type == "array") and (.jobs | length > 0) and (.total_count == null or .total_count <= (.jobs | length))' >/dev/null 2>&1; then
    while IFS= read -r coverage_job_id; do
      coverage_log="$(mktemp)"
      # Live-log cooldown: a skipped fetch leaves the verdict unknown
      # (reason=live_log_cooldown) and is not a strike, so the log is
      # read again after the pause.
      if ! live_log_fetch_allowed; then
        coverage_failed=1
        coverage_cooldown=1
      elif ! gh_api_safe_quiet_print "repos/${TEST_REPO}/actions/jobs/${coverage_job_id}/logs" > "$coverage_log"; then
        coverage_failed=1
        live_log_fetch_record fail
      else
        live_log_fetch_record ok
        coverage_read=$((coverage_read + 1))
        # Match only a resolved 40-hex SHA, never the echoed shell
        # source line containing a literal ${INITIAL_HEAD_SHA}.
        coverage_checked=$(grep -am1 -oE 'Captured INITIAL_HEAD_SHA=[0-9a-fA-F]{40}([^0-9a-fA-F]|$)' "$coverage_log" | grep -m1 -oE '[0-9a-fA-F]{40}' || true)
      fi
      rm -f "$coverage_log"
      if [ -n "$coverage_checked" ]; then break; fi
    done < <(printf '%s' "$coverage_jobs" | jq -r '.jobs[] | select(.status == "completed" and .conclusion != "skipped") | .id | select(type == "number" and . > 0 and . == floor) | tostring')
    if [ -n "$coverage_checked" ]; then
      if [ "${coverage_checked,,}" = "${BAIT_SHA,,}" ]; then
        coverage_verdict="covered"; coverage_reason="bait_sha"
      else
        coverage_status=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/compare/${BAIT_SHA}...${coverage_checked}" --jq '.status' || echo "")
        case "$coverage_status" in
          ahead|identical) coverage_verdict="covered"; coverage_reason="bait_ancestor" ;;
          behind|diverged) coverage_verdict="not_covered"; coverage_reason="pre_bait_checkout" ;;
          *) coverage_reason="compare_unavailable" ;;
        esac
      fi
    elif [ "$coverage_cooldown" -eq 1 ]; then
      coverage_reason="live_log_cooldown"
      BAIT_REVIEW_COOLDOWN_SKIP=1
    elif [ "$coverage_failed" -eq 1 ] || [ "$coverage_read" -eq 0 ]; then
      coverage_reason="logs_unavailable"
    elif [ "$RUN_STATUS" = "completed" ]; then
      coverage_verdict="not_covered"; coverage_reason="no_checkout_marker"
    else
      coverage_reason="checkout_pending"
    fi
  fi
  BAIT_REVIEW_VERDICT="$coverage_verdict"
  if [ "$coverage_verdict" != "unknown" ]; then
    BAIT_REVIEW_COVERAGE_CACHE[$coverage_run_id]="$coverage_verdict"
  fi
  if [ -n "$coverage_checked" ]; then coverage_display="${coverage_checked:0:7}"; fi
  echo "E2E_BAIT_REVIEW_COVERAGE run=${coverage_run_id} verdict=${coverage_verdict} checked_out=${coverage_display} reason=${coverage_reason}"
}

# A completed pre-bait-triggered run may finish after bait injection
# without ever checking out bait (run 37395952357). Apply this gate
# before *every* exit that reports a review run, including the
# in-progress failed-step and editor-noop shortcuts.
require_bait_review_coverage() {
  local candidate_head=""
  if [ -z "${BAIT_SHA:-}" ]; then return 0; fi
  candidate_head=$(printf '%s' "$REVIEW_RUN" | jq -r '.head_sha // ""')
  if [ "$candidate_head" = "$PIN_SHA" ] || [ "$candidate_head" = "$BAIT_SHA" ]; then return 0; fi
  review_run_checked_out_bait "$RUN_ID"
  if [ "$BAIT_REVIEW_VERDICT" = "covered" ]; then return 0; fi
  if [ "$BAIT_REVIEW_VERDICT" = "not_covered" ]; then
    echo "::warning::Review run ${RUN_ID} checked out a pre-bait head or had no checkout marker; not a review of the bait"
    BAIT_UNCOVERED_RUN_IDS="${BAIT_UNCOVERED_RUN_IDS:+${BAIT_UNCOVERED_RUN_IDS},}${RUN_ID}"
    LAST_ACTIVITY=$NOW
    PREV_STATE=""
  elif [ "$RUN_STATUS" = "completed" ] && [ "${BAIT_REVIEW_COOLDOWN_SKIP:-0}" != "1" ]; then
    # A live-log cooldown skip is not a strike: no log was read.
    # A completed run outranks the queued bait-head run in the
    # selector, so persistently unverifiable coverage must not pin
    # Phase 4 to it. Exclude it (never accept it) after 3 unknowns.
    BAIT_REVIEW_UNKNOWN_COUNTS[$RUN_ID]=$(( ${BAIT_REVIEW_UNKNOWN_COUNTS[$RUN_ID]:-0} + 1 ))
    if [ "${BAIT_REVIEW_UNKNOWN_COUNTS[$RUN_ID]}" -ge 3 ]; then
      echo "::warning::Review run ${RUN_ID} bait coverage stayed unverifiable; waiting for a bait-head review instead"
      BAIT_UNCOVERED_RUN_IDS="${BAIT_UNCOVERED_RUN_IDS:+${BAIT_UNCOVERED_RUN_IDS},}${RUN_ID}"
      PREV_STATE=""
    fi
  fi
  sleep "$POLL_INTERVAL"
  ELAPSED=$((ELAPSED + POLL_INTERVAL))
  return 1
}

# Give the review workflow a moment to start
sleep 5

while true; do
  NOW=$(date +%s)
  IDLE=$((NOW - LAST_ACTIVITY))
  if [ "$NOW" -ge "$REVIEW_PHASE_DEADLINE" ]; then
    echo "::error::Review phase exceeded its ${REVIEW_PHASE_WALL_BUDGET_MINUTES}-minute total wall-clock budget"
    echo "status=timeout" >> "$GITHUB_OUTPUT"
    exit 1
  fi

  # Find the review workflow run for this PR's head branch
  PR_BRANCH="ai/issue-${ISSUE_NUMBER}"

  # Look for workflow runs on this branch.
  # When BAIT_SHA is set the filter pins to head_sha == BAIT_SHA
  # so the wait loop cannot mistake a stale implement-phase
  # review run (head_sha = the implement commit, not the bait
  # commit) for a bait-triggered run. per_page is widened to
  # 100 because a doc-only PR's head can carry several extra
  # workflow runs (CI, Internal: AI Review & Autofix, …) and
  # the bait-triggered run could get crowded out of a small
  # page. Falls back to the legacy latest-by-created_at filter
  # only when the bait-injection step did not produce a SHA
  # (status=skipped path).
  #
  # The URL no longer pins event=pull_request: Phase 3c also
  # dispatches ${REVIEW_WORKFLOW_FILE} as a Bug B fallback, and
  # those runs carry event=workflow_dispatch. Dropping the
  # event filter from the URL lets head_sha=BAIT_SHA match
  # both paths uniformly. (We still rely on the jq name match
  # to avoid latching onto e.g. CI runs that happen to share
  # the bait SHA.) The dispatched run's head_sha matches
  # BAIT_SHA because we dispatch with --ref "${BRANCH}" and
  # GitHub records the ref's tip SHA at dispatch time.
  #
  # GitHub's "Running Copilot Code Review" run is excluded
  # (event == "dynamic" / name contains "copilot"): it is a
  # code-REVIEW agent, not the review-AND-AUTOFIX workflow, yet
  # its name matches the broad `Review` token above. On v1.19.0
  # (run 27926259717) the Copilot run (head=pre-bait SHA,
  # created 02:56:26, updated 02:58:40) straddled the bait
  # timestamp (02:58:37) so leg (b)'s created<=bait<=updated
  # window admitted it; tier-1 preference then picked that
  # already-completed Copilot success over the still-in-progress
  # real autofix run, so Phase 4 declared success ~6s in and
  # Phase 4b then failed on the unrestored canary. Excluding the
  # Copilot reviewer keeps the loop watching the actual
  # internal-review.yml / review_autofix.yml run instead.
  #
  # `self-healing` is part of the regex because review_autofix.yml
  # ("Codex PR Self-Healing Semantic Agent") is dispatched directly
  # by its own post_commit_retrigger after the editor commits the
  # fix — that engine run is the canonical successor at PIN_SHA on
  # repos where review_autofix.yml exists locally (coding-workflows
  # itself, plus consumer repos that copy/sync it). Without this
  # token the engine's name does not match `Review|review|autofix`,
  # so the wait loop never sees the post-commit successor run and
  # ages out at REVIEW_TIMEOUT — observed on run-25445414047 where
  # PIN_SHA advanced to the editor commit but the engine
  # workflow_dispatch run at that SHA was filtered out and the
  # poll timed out 30m later.
  if [ -n "${PIN_SHA:-}" ]; then
    # Candidate set ($m): name-matched review runs that EITHER
    #   (a) carry head_sha == PIN_SHA — the Phase 3c
    #       workflow_dispatch fallback and the bait
    #       pull_request:synchronize run; OR
    #   (b) were in flight when the bait commit landed:
    #       created_at <= BAIT_CREATED_AT AND the run is still
    #       active — either updated_at >= BAIT_CREATED_AT OR
    #       status == "in_progress".
    # Leg (b) recovers the original PR-open review run: it is
    # triggered on the pre-bait head SHA (so its run-level
    # head_sha never equals BAIT_SHA and leg (a) misses it),
    # but its checkout may predate the bait push. Before
    # accepting its result, verify Captured INITIAL_HEAD_SHA
    # from its job log includes the bait (run 37395952357).
    # Crucially,
    # the bait-SHA runs in leg (a) all serialise behind that
    # original run in review_autofix.yml's `pr-autofix-${PR}`
    # concurrency group (cancel-in-progress: false), so when
    # the original review runs long they stay queued/pending
    # past REVIEW_TIMEOUT and the loop, pinned to a 0-activity
    # pending sibling, ages out — the failure on runs
    # 27137020958 / 27244093614 / 27328808462 (v1.17.0). Leg (b)
    # lets the loop watch the actually-running review instead;
    # its growing job log registers as activity each poll and
    # resets the inactivity timer until it completes.
    # The BAIT_CREATED_AT guard (!= "") keeps leg (b) inert
    # when no bait was injected.
    #
    # The `or .status == "in_progress"` half is load-bearing:
    # the original updated_at >= BAIT_CREATED_AT bound alone
    # produced a FALSE NEGATIVE on run 27328808462. There the
    # PR-open review's codex-agent job started ~2m BEFORE the
    # bait and then ran a single ~37m job with no further
    # run-level state transitions, so the runs-list API reported
    # its `updated_at` frozen at the pre-bait job-start time
    # (< BAIT_CREATED_AT) for the whole 30m window. The bound
    # therefore rejected the very run leg (b) exists to catch,
    # the loop fell through to the queued-sibling fallback, and
    # the phase aged out. A run created at/before the bait that
    # is STILL in_progress was necessarily in flight when the
    # bait landed (it has not finished), so status alone is a
    # sound candidate signal independent of the frozen timestamp,
    # but never proof that the checkout saw the bait.
    #
    # Both legs still exclude the historical stale-implement-
    # review false match (run 25147636528) which COMPLETED
    # before the bait: a completed run is neither updated_at >=
    # BAIT_CREATED_AT (it stopped before the bait) nor
    # status == "in_progress".
    #
    # Preference order over $m:
    #   1. newest completed non-cancelled run (terminal accept);
    #   2. oldest in_progress run created at/before the bait —
    #      this is the original long-running PR-open review
    #      (in flight when the bait landed), and it must win
    #      over a newer caller workflow run that is merely
    #      blocked behind it. (No updated_at bound here: see
    #      the run 27328808462 frozen-timestamp note above.)
    #   3. newest in_progress run (generic active-run fallback);
    #   4. newest non-cancelled run (queued/pending fallback);
    #   5. newest run overall (all cancelled).
    # Tier 1 still precedes 2 so an already-completed success
    # wins over an active sibling (preserves the run-26989483268
    # fix).
    #
    # select(.name != .path) drops phantom "workflow file issue"
    # runs. GitHub names a run after its file path only when it
    # cannot load the workflow (e.g. a file over the 512,000-byte
    # limit), since every workflow here and in workflow-templates/
    # sets name:. Such a zero-job run on the pinned head_sha
    # matched the name regex and was accepted instantly as
    # "completed with failure" before any editor ran (stable gate
    # run 35903885958, phantom .github/workflows/review_autofix.yml
    # runs after #4327).
    REVIEW_RUN=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/actions/runs?branch=${PR_BRANCH}&per_page=100" \
      --jq "[.workflow_runs[] | select(.name | test(\"Review|review|autofix|self-healing\"; \"i\")) | select((.event == \"dynamic\" or (.name | test(\"copilot\"; \"i\"))) | not) | select(.name != .path) | select(.head_sha == \"${PIN_SHA}\" or (\"${BAIT_CREATED_AT:-}\" != \"\" and .created_at <= \"${BAIT_CREATED_AT:-}\" and (.updated_at >= \"${BAIT_CREATED_AT:-}\" or .status == \"in_progress\"))) | select((.id | tostring) as \$rid | (\"${BAIT_UNCOVERED_RUN_IDS:-}\" | split(\",\") | index(\$rid)) == null)] as \$m | (\$m | map(select(.status == \"completed\" and .conclusion != \"cancelled\")) | sort_by(.created_at) | last) // (\$m | map(select(.status == \"in_progress\" and \"${BAIT_CREATED_AT:-}\" != \"\" and .created_at <= \"${BAIT_CREATED_AT:-}\")) | sort_by(.created_at) | first) // (\$m | map(select(.status == \"in_progress\")) | sort_by(.created_at) | last) // (\$m | map(select(.conclusion != \"cancelled\")) | sort_by(.created_at) | last) // (\$m | sort_by(.created_at) | last)" || echo "")
  else
    REVIEW_RUN=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/actions/runs?branch=${PR_BRANCH}&event=pull_request&per_page=5" \
      --jq '[.workflow_runs[] | select(.name | test("Review|review|autofix|self-healing"; "i")) | select((.event == "dynamic" or (.name | test("copilot"; "i"))) | not) | select(.name != .path)] | sort_by(.created_at) | last' || echo "")
  fi

  # If rate-limited, skip this cycle entirely (don't corrupt state)
  if [ "$RATE_LIMIT_BACKOFF" -gt 0 ] && [ -z "$REVIEW_RUN" ]; then
    ELAPSED=$((ELAPSED + POLL_INTERVAL))
    continue
  fi

  RUN_STATUS="none"
  RUN_CONCLUSION="null"
  RUN_ID="unknown"
  FAILED_STEPS="0"

  if [ -n "$REVIEW_RUN" ] && [ "$REVIEW_RUN" != "null" ]; then
    RUN_STATUS=$(echo "$REVIEW_RUN" | jq -r '.status // "unknown"')
    RUN_CONCLUSION=$(echo "$REVIEW_RUN" | jq -r '.conclusion // "null"')
    RUN_ID=$(echo "$REVIEW_RUN" | jq -r '.id // "unknown"')

    echo "  Review run #${RUN_ID}: status=${RUN_STATUS}, conclusion=${RUN_CONCLUSION}"

    if [ "$RUN_STATUS" = "completed" ]; then
      if [ "$RUN_CONCLUSION" = "success" ]; then
        if ! require_bait_review_coverage; then continue; fi
        echo "✓ Review & autofix completed successfully"
        echo "status=success" >> "$GITHUB_OUTPUT"
        echo "review_run_id=${RUN_ID}" >> "$GITHUB_OUTPUT"
        exit 0
      elif [ "$RUN_CONCLUSION" = "failure" ]; then
        if ! require_bait_review_coverage; then continue; fi
        echo "::warning::Review workflow completed with failure — this may be acceptable if reviewers found issues"
        echo "status=completed_with_findings" >> "$GITHUB_OUTPUT"
        echo "review_run_id=${RUN_ID}" >> "$GITHUB_OUTPUT"
        exit 0
      elif [ "$RUN_CONCLUSION" = "cancelled" ]; then
        echo "Review run was cancelled — checking for newer run..."
        # internal-review.yml's concurrency group is keyed on
        # `head_ref` with `cancel-in-progress: true`, so the
        # autofix editor pushing a fix commit fires a fresh
        # `pull_request: synchronize` event whose successor run
        # cancels this one. When that happens the PR head has
        # already moved past the pin, and the successor's
        # head_sha equals the new PR head — advance the pin so
        # the next iteration latches onto the successor instead
        # of polling this dead run forever (the failure mode on
        # run-25428461223). Reset the activity timer because
        # the pivot itself is meaningful progress.
        PIN_HEAD_NOW=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' || echo "")
        if [ -n "${PIN_HEAD_NOW}" ] && ! [[ "${PIN_HEAD_NOW}" =~ ^[0-9a-fA-F]{40}$ ]]; then
          echo "::error::PR head API response is not a valid 40-hex SHA."
          exit 1
        fi
        if [ -n "${PIN_SHA:-}" ] && [ -n "${PIN_HEAD_NOW}" ] && [ "${PIN_HEAD_NOW}" != "${PIN_SHA}" ]; then
          echo "  ↪ PR head advanced past pin (${PIN_SHA:0:7} → ${PIN_HEAD_NOW:0:7}); advancing pin and polling successor run next iteration"
          PIN_SHA="${PIN_HEAD_NOW}"
          LAST_ACTIVITY=$NOW
          PREV_STATE=""
          sleep "$POLL_INTERVAL"
          ELAPSED=$((ELAPSED + POLL_INTERVAL))
          continue
        fi
      fi
    fi

    # While the run is still in_progress, probe job steps to detect
    # early failure. The review workflow uses if:always() cleanup steps
    # that keep the run in_progress long after the main work has died.
    if [ "$RUN_STATUS" = "in_progress" ] && [ "$RUN_ID" != "unknown" ]; then
      # Fetch jobs once and reuse for failed-steps check, current step, and log size
      JOBS_JSON=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/actions/runs/${RUN_ID}/jobs?per_page=10" || echo "")
      if [ -n "$JOBS_JSON" ]; then
        FAILED_STEPS=$(echo "$JOBS_JSON" | jq '[.jobs[].steps[] | select(.conclusion == "failure")] | length' 2>/dev/null || echo "0")
      else
        FAILED_STEPS="0"
      fi
      if [ "$FAILED_STEPS" -gt 0 ]; then
        if ! require_bait_review_coverage; then continue; fi
        echo "  ⚠ Review run has ${FAILED_STEPS} failed step(s) — main work has failed, cleanup still running"
        echo "::warning::Review workflow has failed steps while cleanup is still in_progress"
        echo "status=completed_with_findings" >> "$GITHUB_OUTPUT"
        echo "review_run_id=${RUN_ID}" >> "$GITHUB_OUTPUT"
        exit 0
      fi

      # Check live logs for reviewer progress
      # and for the editor-noop fallback marker (early-exit shortcut).
      # Both are gated behind ELAPSED >= 600 so we only pay the live-
      # log API cost after the editor step is plausibly running —
      # reviewers + editor together take well over 10 minutes in
      # every observed run, so anything earlier is wasted I/O.
      if [ "$ELAPSED" -ge 600 ]; then
        # Select the job that actually runs the reviewer/editor
        # LLM steps, NOT `.jobs[0]`. When the review run comes
        # from the wrapper workflow (internal-review.yml calling
        # review_autofix.yml — the default REVIEW_WORKFLOW_FILE),
        # the run carries several jobs and jobs[0] by creation
        # order is `review / gate`, a ~4s gate job whose log can
        # never contain the editor-noop annotation or the
        # "Reviewer … succeeded" counter lines — on v1.27.0 run
        # 32824674139 every live-log probe hit the gate job
        # (97733868951) while `review / codex-agent`
        # (97733894241) did the actual work, so both shortcuts
        # were permanently inert. Prefer the codex-agent job by
        # exact name/path component plus any parenthesized suffix
        # (a job rename is a breaking change per CLAUDE.md §6,
        # so the literal is safe), then any in-progress job, then
        # jobs[0] as the legacy single-job fallback.
        JOB_ID=$(echo "$JOBS_JSON" | jq -r '([.jobs[] | select(.name | test("(^| / )codex-agent( \\([^)]*\\))?$"))] | last | .id) // ([.jobs[] | select(.status == "in_progress")] | last | .id) // .jobs[0].id // empty' 2>/dev/null || echo "")
        if [ -n "$JOB_ID" ]; then
          # Stream the job log to a tempfile rather than capturing
          # into a shell variable. `gh_api_safe .../actions/jobs/
          # {id}/logs` returns the raw log bytes (multi-MB on long
          # autofix runs) and bash command-substitution `$(...)`
          # silently drops NUL bytes from the captured output —
          # which can occur when an inner tool (codex, jq, gh)
          # emits binary content into stderr/stdout. Dropped NULs
          # could merge adjacent lines and cause both the editor-
          # noop grep and the reviewer-success counter to silently
          # miss otherwise-valid matches. Writing once to a
          # tempfile and grepping the file is byte-exact and uses
          # no more API calls than before. See PR #1798 review by
          # copilot-pull-request-reviewer.
          LOG_FILE="$(mktemp)"
          # On API failure (rate-limit or transient blip),
          # gh_api_safe writes nothing to stdout and returns
          # non-zero. Truncate LOG_FILE to zero bytes so the
          # downstream greps see "no matches" rather than
          # stale content from a previous iteration's mktemp
          # collision (mktemp returns a fresh path so this is
          # belt-and-braces, not strictly required). Emit a
          # diagnostic so operators can spot API blips without
          # having to correlate the missing shortcuts against
          # the rate-limit log lines gh_api_safe emits to
          # stderr — surfaces the case Grok-4.1 flagged in
          # the claude-branch-review on PR #1798.
          # Stderr is intentionally NOT redirected: gh_api_safe
          # writes its own rate-limit / transient-failure
          # diagnostics to >&2 (e.g. "⏳ Rate limited — backing
          # off ${RATE_LIMIT_BACKOFF}s"), and operators rely on
          # those lines to distinguish a genuinely-stuck review
          # workflow from a slow smoke run caused by API blips.
          # During a live-log cooldown the log counts as empty,
          # like a failed fetch, but the skip is not a failure.
          if ! live_log_fetch_allowed; then
            : > "$LOG_FILE"
          elif gh_api_safe_quiet_print "repos/${TEST_REPO}/actions/jobs/${JOB_ID}/logs" > "$LOG_FILE"; then
            live_log_fetch_record ok
          else
            : > "$LOG_FILE"
            echo "  ⚠ Live-log fetch failed for job ${JOB_ID} — editor-noop shortcut and reviewer-count shortcut are inactive this iteration; the wait loop continues with status/conclusion polling only" >&2
            live_log_fetch_record fail
          fi

          # Editor-noop fallback marker shortcut.
          #
          # When `Validate editor no-op disposition` in
          # review_autofix.yml emits the literal warning
          # "Editor summary contains failure/fallback markers",
          # the editor never produced a validated summary and the
          # downstream merge-conflict / resolver / push steps
          # (now gated on EDITOR_NOOP_SUSPICIOUS != 'true') will
          # all skip — there is nothing left for the e2e bait
          # check to verify, so we can short-circuit the wait
          # loop the moment the marker appears in the live log
          # rather than waiting for the cleanup steps to wind
          # down (which can take another ~3.5 minutes and was
          # the proximate cause of the run-25126757724 cascade
          # masking the real editor failure).
          #
          # `grep -qaF` for an exact substring match — the
          # annotation text is stable per analysis/e2e-smoke-
          # failure-25126757724.md (a rename in
          # review_autofix.yml is a breaking change per
          # CLAUDE.md §6, so this literal is safe to depend on).
          # The `-a` flag forces grep to treat the input as text
          # even if a NUL byte in the streamed log would
          # otherwise make grep classify it as binary and
          # short-circuit to "Binary file ... matches" without
          # honouring `-q`'s quiet exit-code-only contract; do
          # not drop `-a` when editing this block.
          #
          # The `::warning::` prefix is part of the literal so
          # this only matches the validator's annotation, not
          # any other log line that happens to mention the
          # phrase (e.g. the in-step retry's own `::notice::`
          # before its widened gate fix landed — see PR #1798
          # review). The validator emits exactly this prefix
          # (`echo "::warning::Editor summary contains
          # failure/fallback markers — editor never completed
          # a validated review."`); GitHub Actions preserves
          # workflow-command prefixes verbatim in the raw log
          # bytes returned by /actions/jobs/{id}/logs.
          if grep -qaF '::warning::Editor summary contains failure/fallback markers' "$LOG_FILE"; then
            if ! require_bait_review_coverage; then
              rm -f "$LOG_FILE"
              continue
            fi
            # When the editor's no-op was caused by a model
            # safety-policy refusal (an OpenAI-style "I'm
            # sorry, but I cannot assist" final-channel
            # output), the validator additionally emits the
            # `::notice::Editor returned a safety-policy
            # refusal` annotation (review_autofix.yml,
            # `Validate editor no-op disposition`). Re-running
            # after the provider-side prompt cache expires
            # typically clears the trip, so the operator-facing
            # warning carries that hint. The generic-noop
            # warning literal stays unchanged for non-refusal
            # cases to avoid breaking any downstream grep
            # consumer (CLAUDE.md §6).
            if grep -qaF '::notice::Editor returned a safety-policy refusal' "$LOG_FILE"; then
              echo "  ⚠ Editor safety-refusal detected in live log — model returned a refusal; provider-side cache will serve identical refusals briefly; aborting wait early"
              echo "::warning::Review workflow editor refused (safety filter) — re-run after a few minutes (EDITOR_NOOP_REFUSAL marker observed); no autofix commit will be pushed"
            else
              echo "  ⚠ Editor noop marker detected in live log — editor never produced a validated commit; aborting wait early"
              echo "::warning::Review workflow editor produced no validated summary (EDITOR_NOOP_SUSPICIOUS marker observed); no autofix commit will be pushed"
            fi
            echo "status=completed_with_findings" >> "$GITHUB_OUTPUT"
            echo "review_run_id=${RUN_ID}" >> "$GITHUB_OUTPUT"
            rm -f "$LOG_FILE"
            exit 0
          fi

          # `-a` treats the input as text even if grep otherwise
          # heuristically classified it as binary (any NUL byte
          # would normally make grep fall through to "Binary file
          # ... matches" instead of printing/counting matches).
          SUCCEEDED=$(grep -caE "Reviewer .* succeeded" "$LOG_FILE" || true)
          FAILED_R=$(grep -caE "Reviewer .* failed after" "$LOG_FILE" || true)
          rm -f "$LOG_FILE"
          SUCCEEDED="${SUCCEEDED:-0}"; SUCCEEDED="${SUCCEEDED##*$'\n'}"
          FAILED_R="${FAILED_R:-0}"; FAILED_R="${FAILED_R##*$'\n'}"
          [[ "$SUCCEEDED" =~ ^[0-9]+$ ]] || SUCCEEDED=0
          [[ "$FAILED_R" =~ ^[0-9]+$ ]] || FAILED_R=0
          TOTAL_DONE=$((SUCCEEDED + FAILED_R))
          if [ "$SUCCEEDED" -ge 3 ] && [ $((SUCCEEDED * 2)) -gt "$TOTAL_DONE" ]; then
            echo "  ... ${SUCCEEDED}/${TOTAL_DONE} reviewers succeeded (>50%); waiting for authoritative review completion and editor verification..."
          elif [ "$TOTAL_DONE" -gt 0 ]; then
            echo "  ... ${SUCCEEDED}/${TOTAL_DONE} reviewers succeeded so far, waiting for more..."
          fi
        fi
      fi
    fi
  fi

  # ── Adaptive activity signals ──────────────────────────
  # Beyond run status/conclusion, probe deeper indicators
  # that the review workflow is genuinely making progress.

  # 1) Current step name — reuse JOBS_JSON if already fetched.
  #    Also capture the in-progress step's started_at so the
  #    inactivity check can tell a healthy long step (whose logs
  #    GitHub does not stream) apart from a genuinely hung run —
  #    see the REVIEW_STEP_TIMEOUT guard below.
  CURRENT_STEP=""
  CURRENT_STEP_STARTED_AT=""
  STEP_METADATA_FETCH_OK=0
  if [ "$RUN_ID" != "unknown" ]; then
    if [ -n "${JOBS_JSON:-}" ]; then
      CURRENT_STEP=$(echo "$JOBS_JSON" | jq -r '[.jobs[].steps[] | select(.status == "in_progress")] | last | .name // ""' 2>/dev/null || echo "")
      CURRENT_STEP_STARTED_AT=$(echo "$JOBS_JSON" | jq -r '[.jobs[].steps[] | select(.status == "in_progress")] | last | .started_at // ""' 2>/dev/null || echo "")
      if [ -n "${CURRENT_STEP_STARTED_AT:-}" ]; then
        STEP_METADATA_FETCH_OK=1
      fi
    else
      JOBS_STEP_JSON=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/actions/runs/${RUN_ID}/jobs?per_page=10" || echo "")
      if [ -n "$JOBS_STEP_JSON" ]; then
        CURRENT_STEP=$(echo "$JOBS_STEP_JSON" | jq -r '[.jobs[].steps[] | select(.status == "in_progress")] | last | .name // ""' 2>/dev/null || echo "")
        CURRENT_STEP_STARTED_AT=$(echo "$JOBS_STEP_JSON" | jq -r '[.jobs[].steps[] | select(.status == "in_progress")] | last | .started_at // ""' 2>/dev/null || echo "")
        if [ -n "${CURRENT_STEP_STARTED_AT:-}" ]; then
          STEP_METADATA_FETCH_OK=1
        fi
      fi
    fi
  fi
  if [ -n "${CURRENT_STEP_STARTED_AT:-}" ]; then
    LAST_STEP_GUARD_RUN_ID="$RUN_ID"
    LAST_STEP_GUARD_NAME="${CURRENT_STEP:-}"
    LAST_STEP_GUARD_STARTED_AT="$CURRENT_STEP_STARTED_AT"
  elif [ "${RUN_STATUS:-}" = "in_progress" ] && [ "$STEP_METADATA_FETCH_OK" != "1" ] && [ "${LAST_STEP_GUARD_RUN_ID:-}" = "$RUN_ID" ] && [ -n "${LAST_STEP_GUARD_STARTED_AT:-}" ] && { [ -z "${CURRENT_STEP:-}" ] || [ "${CURRENT_STEP}" = "${LAST_STEP_GUARD_NAME:-}" ]; }; then
    CURRENT_STEP_STARTED_AT="$LAST_STEP_GUARD_STARTED_AT"
    CURRENT_STEP="${CURRENT_STEP:-${LAST_STEP_GUARD_NAME:-}}"
    echo "  ⚠ Jobs API returned no in-progress step timing for run ${RUN_ID}; reusing last-seen step metadata for this poll" >&2
  elif [ "${RUN_STATUS:-}" = "in_progress" ] && [ "$STEP_METADATA_FETCH_OK" != "1" ]; then
    echo "  ⚠ Jobs API returned no in-progress step timing for run ${RUN_ID}; per-step stall guard is unavailable this poll" >&2
  fi

  # 2) Commit count on PR branch — editor pushes trigger changes
  PR_HEAD_SHA=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/pulls/${PR_NUMBER}" \
    --jq '.head.sha // ""' || echo "")

  # 3) Review comment count on the PR
  REVIEW_COMMENT_COUNT=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/pulls/${PR_NUMBER}/comments" \
    --jq 'length' || echo "0")

  # 4) Job log byte size — grows while reviewers/editor produce output
  # Same job selection as the live-log shortcut block above:
  # prefer the codex-agent job (the one producing log output),
  # then any in-progress job, then jobs[0] — in wrapper runs
  # jobs[0] is the completed `review / gate` job whose log size
  # never changes, which starved this activity signal on run
  # 32824674139 (log_sz pinned at 0b for the whole phase).
  LOG_SIZE="0"
  if [ "$RUN_ID" != "unknown" ]; then
    JOB_ID_FOR_SIZE=""
    if [ -n "${JOBS_JSON:-}" ]; then
      JOB_ID_FOR_SIZE=$(echo "$JOBS_JSON" | jq -r '([.jobs[] | select(.name | test("(^| / )codex-agent( \\([^)]*\\))?$"))] | last | .id) // ([.jobs[] | select(.status == "in_progress")] | last | .id) // .jobs[0].id // empty' 2>/dev/null || echo "")
    else
      JOB_ID_FOR_SIZE=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/actions/runs/${RUN_ID}/jobs?per_page=10" \
        --jq '([.jobs[] | select(.name | test("(^| / )codex-agent( \\([^)]*\\))?$"))] | last | .id) // ([.jobs[] | select(.status == "in_progress")] | last | .id) // .jobs[0].id // empty' || echo "")
    fi
    if [ -n "$JOB_ID_FOR_SIZE" ] && live_log_fetch_allowed; then
      if LOG_SIZE=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/actions/jobs/${JOB_ID_FOR_SIZE}/logs" \
        | wc -c); then
        live_log_fetch_record ok
      else
        LOG_SIZE="0"
        live_log_fetch_record fail
      fi
      LOG_SIZE="${LOG_SIZE##*$'\n'}"
      [[ "$LOG_SIZE" =~ ^[0-9]+$ ]] || LOG_SIZE="0"
    fi
  fi

  CUR_STATE="run=${RUN_STATUS};conclusion=${RUN_CONCLUSION};failed_steps=${FAILED_STEPS};step=${CURRENT_STEP};head=${PR_HEAD_SHA};reviews=${REVIEW_COMMENT_COUNT};log_sz=${LOG_SIZE}"

  if [ "$CUR_STATE" != "$PREV_STATE" ]; then
    if [ -n "$PREV_STATE" ]; then
      echo "  ↻ Activity detected (state changed) — resetting inactivity timer"
    fi
    LAST_ACTIVITY=$NOW
    IDLE=0
    PREV_STATE="$CUR_STATE"
  fi

  # Inactivity timeout. When BAIT_SHA is set and the loop
  # never matched a review run for that SHA, surface a
  # distinct status so verify-bait-removed's failure isn't
  # the first place the operator hears that the bait push
  # never spawned a downstream review_autofix run. Dump the
  # review-run inventory for the branch so the diagnosis
  # doesn't require an out-of-band gh api call.
  if [ $IDLE -ge $INACTIVITY_LIMIT ]; then
    # Structural guard against GitHub not streaming in-progress
    # step logs: when the pinned review run is still in_progress
    # and its current step has been running for less than the
    # per-step cap, the inactivity silence is explained by one
    # long healthy step (e.g. "Run reviewer models", which ran
    # 41m on v1.20.1 run 28282500673 with log_sz frozen at
    # 52333b) rather than a genuine stall. Defer the stall check
    # and keep polling. Once the step reaches the cap (or its
    # timestamp cannot be trusted), execution falls through to the
    # normal stall path below, so a truly-hung run still fails —
    # just bounded by REVIEW_STEP_TIMEOUT instead of the global
    # inactivity window.
    if [ "${RUN_STATUS:-}" = "in_progress" ] && [ -n "${CURRENT_STEP_STARTED_AT:-}" ]; then
      STEP_STARTED_EPOCH=$(date -d "${CURRENT_STEP_STARTED_AT}" +%s 2>/dev/null || echo 0)
      if [ "$STEP_STARTED_EPOCH" -gt 0 ]; then
        STEP_ELAPSED=$((NOW - STEP_STARTED_EPOCH))
        if [ "$STEP_ELAPSED" -ge 0 ] && [ "$STEP_ELAPSED" -lt "$STEP_INACTIVITY_LIMIT" ]; then
          STEP_REMAINING=$(( (STEP_INACTIVITY_LIMIT - STEP_ELAPSED + 59) / 60 ))
          echo "  ⏳ idle ${IDLE}s ≥ ${REVIEW_TIMEOUT}m, but review run ${RUN_ID} step '${CURRENT_STEP:-?}' has only run ${STEP_ELAPSED}s (< ${REVIEW_STEP_TIMEOUT}m cap; GitHub does not stream in-progress step logs) — deferring stall check (~${STEP_REMAINING}m of step budget left)"
          sleep "$POLL_INTERVAL"
          ELAPSED=$((ELAPSED + POLL_INTERVAL))
          continue
        fi
      else
        echo "  ⚠ Could not parse in-progress step started_at='${CURRENT_STEP_STARTED_AT}' for run ${RUN_ID}; per-step stall guard is unavailable this poll" >&2
      fi
    fi
    # jq's `last` on an empty array yields the literal string
    # "null" rather than empty, so both branches are needed.
    if [ -n "${BAIT_SHA:-}" ] && { [ -z "${REVIEW_RUN:-}" ] || [ "${REVIEW_RUN}" = "null" ]; }; then
      if [ "${PIN_SHA:-}" != "${BAIT_SHA:-}" ]; then
        echo "::error::Review run pin was advanced from BAIT_SHA=${BAIT_SHA} to PIN_SHA=${PIN_SHA} after a cancellation, but no successor run with head_sha=${PIN_SHA} appeared within ${REVIEW_TIMEOUT}m — the editor's fix-commit push did not produce a review_autofix run"
      else
        echo "::error::No review workflow run (Internal: AI Review & Autofix) with head_sha=${BAIT_SHA} ever appeared within ${REVIEW_TIMEOUT}m — neither the bait push's pull_request:synchronize event nor the Phase 3c workflow_dispatch fallback produced a review run"
      fi
      echo "status=no_review_triggered" >> "$GITHUB_OUTPUT"
      echo "Diagnostic: workflow runs visible on branch ${PR_BRANCH}:" >&2
      gh_api_safe_quiet_print "repos/${TEST_REPO}/actions/runs?branch=${PR_BRANCH}&per_page=20" \
        --jq '.workflow_runs[] | "  \(.id) \(.name) event=\(.event) status=\(.status) conclusion=\(.conclusion // "null") head_sha=\(.head_sha[0:7])"' >&2 \
        || echo "  (inventory fetch failed)" >&2
      exit 1
    fi
    echo "::error::Review phase stalled — no activity for ${REVIEW_TIMEOUT} minutes"
    echo "status=timeout" >> "$GITHUB_OUTPUT"
    exit 1
  fi

  REMAINING=$(( (INACTIVITY_LIMIT - IDLE + 59) / 60 ))
  echo "  ... idle ${IDLE}s (timeout in ~${REMAINING}m) — step: ${CURRENT_STEP:-?}, head: ${PR_HEAD_SHA:0:7}, reviews: ${REVIEW_COMMENT_COUNT}, log: ${LOG_SIZE}b"
  sleep "$POLL_INTERVAL"
  ELAPSED=$((ELAPSED + POLL_INTERVAL))
done
