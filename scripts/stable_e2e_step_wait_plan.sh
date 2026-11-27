#!/usr/bin/env bash
# shellcheck shell=bash
# Body of the "Phase 2: Wait for plan to complete" step of the e2e-smoke-test job in
# .github/workflows/test-and-mark-stable.yml, moved here verbatim (#9473)
# to keep that workflow well under the 480,000-byte guard
# (tests/test_workflow_file_size_limit.py). The step keeps its name, id
# and env in the workflow; its run: block checks
# STABLE_E2E_STEP_SCRIPTS_VERIFIED and sources a github.sha-pinned copy
# of this file from RUNNER_TEMP, so exit/GITHUB_OUTPUT act on the step.
# The one edit: the three inline steps.create-issue.outputs.created_after
# expressions read PLAN_CREATED_AFTER, which the step's env: now sets.
# Review autofix later narrowed the #9473 ZOMBIE_RUN_IGNORED logging below
# to the runs the guard counts, logging each run once.
set -euo pipefail
echo "Waiting for plan to finish on issue #${ISSUE_NUMBER}..."
echo "Inactivity timeout: ${PLAN_PHASE_TIMEOUT} minutes"
INACTIVITY_LIMIT=$((PLAN_PHASE_TIMEOUT * 60))
LAST_ACTIVITY=$(date +%s)
PREV_STATE=""
RATE_LIMIT_BACKOFF=0

if [ -z "${ISSUE_TITLE}" ]; then
  echo "::error::Missing issue title for Plan run scoping"
  echo "status=plan_failed" >> "$GITHUB_OUTPUT"
  exit 1
fi

. ./scripts/comprehensive_test_and_release_gh_api.sh

fetch_plan_runs_json() {
  gh_api_safe_print "repos/${TEST_REPO}/actions/runs?per_page=100&created=>${PLAN_CREATED_AFTER}"
}

# Page-aware variant of fetch_plan_runs_json. Every issue_comment in
# the repo creates a (mostly skipped) run, so by the time Plan labels
# the issue our Plan run can sit past the first 100 runs of the
# created=> window: run 36374918973 found it at index 96, then lost
# it seconds later and reported Plan success with an empty run ID
# (issue #4723). Capped at 10 pages, GitHub's 1,000-result ceiling
# for this list.
PLAN_RUN_LOOKUP_MAX_PAGES=10
fetch_plan_runs_page_json() {
  gh_api_safe_print "repos/${TEST_REPO}/actions/runs?per_page=100&page=$1&created=>${PLAN_CREATED_AFTER}"
}

# Walks pages newest-first and returns the field of the newest
# matching run from the first page that holds a match, so the common
# case stays one API call. Stops at a short page (end of the window)
# or after the optional third argument's page count (default
# PLAN_RUN_LOOKUP_MAX_PAGES). Returns 1 when a page cannot be read
# (find_latest_scoped_run_field shape-checks each page, because
# gh_api_safe_print echoes its "::error::gh api call failed" line to
# stdout; PR #4730 conformance audit), and 2 when every page up to
# the cap was full and none matched. The paging itself lives in
# find_latest_scoped_run_field in
# scripts/comprehensive_test_and_release_gh_api.sh.
latest_scoped_run_field() {
  local name_re="$1"
  local field="$2"
  local lookup_max_pages="${3:-$PLAN_RUN_LOOKUP_MAX_PAGES}"
  find_latest_scoped_run_field "${TEST_REPO}" "${PLAN_CREATED_AFTER}" "$name_re" "$ISSUE_TITLE" "$field" "$lookup_max_pages"
}

# Both success exits need the issue-scoped Plan run ID: without it
# the soft-error analyser is skipped and deep verification fails the
# release later with zero failed steps. Fail here instead, where the
# cause is visible.
require_plan_run_id() {
  local captured_plan_run_id="$1"
  local plan_label="$2"
  if [[ "$captured_plan_run_id" =~ ^[0-9]+$ ]]; then
    return 0
  fi
  echo "::error::Issue #${ISSUE_NUMBER} reached ${plan_label} but no non-skipped Plan run titled '${ISSUE_TITLE}' was found in the first ${PLAN_RUN_LOOKUP_MAX_PAGES} pages of runs — failing at Plan capture instead of reporting success without a run ID"
  echo "status=run_id_missing" >> "$GITHUB_OUTPUT"
  exit 1
}

# The "unable to confirm concurrent Plan runs" retries `continue`
# before the loop's inactivity check, so they apply the limit
# themselves: a runs page that stays unreadable fails the step as a
# stall after PLAN_PHASE_TIMEOUT minutes instead of polling until the
# job's timeout-minutes (PR #4730 review). Rate limits already back
# off inside gh_api_safe.
fail_plan_confirm_retry_if_idle() {
  local plan_confirm_idle=$(( $(date +%s) - LAST_ACTIVITY ))
  if [ "$plan_confirm_idle" -ge "$INACTIVITY_LIMIT" ]; then
    echo "::error::Plan phase stalled — no activity for ${PLAN_PHASE_TIMEOUT} minutes while concurrent Plan runs could not be confirmed"
    echo "status=timeout" >> "$GITHUB_OUTPUT"
    exit 1
  fi
}

# Robust workflow run-ID capture — retries on empty results to ride out
# the brief race window where the matching run isn't yet indexed by the
# API, and pages past the first 100 runs (find_latest_scoped_run_field) in case of high concurrent
# workflow run volume on the target repo. Filters by display_title so we
# only pick up the Plan run that fired for OUR issue (display_title for
# issue_comment events is the issue title). Returns the run id on stdout
# (empty string if all attempts fail).
capture_run_id() {
  local name_re="$1"
  local result=""
  local plan_lookup_rc=0
  local _attempt
  for _attempt in 1 2 3 4 5; do
    plan_lookup_rc=0
    result=$(latest_scoped_run_field "$name_re" "id") || plan_lookup_rc=$?
    if [ -n "$result" ] && [ "$result" != "null" ]; then
      printf '%s' "$result"
      return 0
    fi
    # Every page GitHub serves for this list was full and none held
    # our run: a retry would repeat the same 10 reads (PR #4730
    # review), so only a short or unreadable window is retried.
    if [ "$plan_lookup_rc" -eq 2 ]; then
      break
    fi
    sleep 2
  done
  printf ''
  return 0
}

while true; do
  NOW=$(date +%s)
  IDLE=$((NOW - LAST_ACTIVITY))

  LABELS=$(gh_api_safe_print "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" \
    --jq '[.labels[].name] | join(",")' || echo "")

  # If rate-limited, skip this cycle (don't corrupt state)
  if [ "$RATE_LIMIT_BACKOFF" -gt 0 ] && [ -z "$LABELS" ]; then
    continue
  fi

  COMMENTS_JSON=$(gh_api_safe_print "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}/comments?per_page=100&direction=desc" || echo "[]")
  COMMENT_COUNT=$(echo "${COMMENTS_JSON}" | jq 'length' 2>/dev/null || echo "0")
  # Track latest comment updated_at so progress comment edits reset the timer
  LATEST_COMMENT_UPDATED=$(echo "${COMMENTS_JSON}" | jq -r '[.[].updated_at] | sort | last // ""' 2>/dev/null || echo "")

  # Also check the plan workflow run status as an activity signal.
  # Filter by display_title (== issue title for issue_comment events)
  # so we pick OUR issue's Plan run, not any concurrent Plan run from
  # a parallel test job. Bumped per_page from 50 to 100 (the API max)
  # so the query still surfaces our run when the release window has
  # high workflow-run volume; v1.13.5's smoke test broke when our
  # Plan run fell to position 51 in the unfiltered list and was
  # truncated off the per_page=50 response.
  # One page only: this runs every POLL_INTERVAL and is an activity
  # signal, so it keeps its single-call cost; the run-ID capture
  # at the success exits pages further (issue #4723).
  PLAN_RUN_STATUS=$(latest_scoped_run_field "Plan" "status" 1 || echo "")
  if [ -z "$PLAN_RUN_STATUS" ] || [ "$PLAN_RUN_STATUS" = "null" ]; then
    PLAN_RUN_STATUS="unknown"
  fi

  CUR_STATE="labels=${LABELS};comments=${COMMENT_COUNT};comment_updated=${LATEST_COMMENT_UPDATED};plan_run=${PLAN_RUN_STATUS}"

  # Reset inactivity timer on any state change
  if [ "$CUR_STATE" != "$PREV_STATE" ]; then
    if [ -n "$PREV_STATE" ]; then
      echo "  ↻ Activity detected (state changed) — resetting inactivity timer"
    fi
    LAST_ACTIVITY=$NOW
    IDLE=0
    PREV_STATE="$CUR_STATE"
  fi

  if echo "$LABELS" | grep -q 'ai:awaiting-approval'; then
    echo "✓ Plan phase completed — awaiting approval"
    PLAN_RUN_ID=$(capture_run_id "Plan")
    require_plan_run_id "$PLAN_RUN_ID" "ai:awaiting-approval"
    echo "run_id=${PLAN_RUN_ID}" >> "$GITHUB_OUTPUT"
    echo "status=success" >> "$GITHUB_OUTPUT"
    exit 0
  fi

  # Check for auto-approved (if AUTO_IMPLEMENT_ON_CLEAR_PLAN is on)
  if echo "$LABELS" | grep -q 'ai:implementing'; then
    echo "✓ Plan phase completed with auto-approval — already implementing"
    PLAN_RUN_ID=$(capture_run_id "Plan")
    require_plan_run_id "$PLAN_RUN_ID" "ai:implementing"
    echo "run_id=${PLAN_RUN_ID}" >> "$GITHUB_OUTPUT"
    echo "status=auto_approved" >> "$GITHUB_OUTPUT"
    exit 0
  fi

  # Claude pool at capacity: the phase paused (#7370); fail fast with its own status (#8217).
  if [[ ",${LABELS}," == *",ai:claude-capacity-wait,"* ]]; then
    echo "::error::Plan phase paused — Claude pool at capacity (ai:claude-capacity-wait); release gate cannot complete until capacity returns"
    echo "status=claude_capacity" >> "$GITHUB_OUTPUT"
    exit 1
  fi

  # Detect plan workflow failure (completed but no expected label)
  if [ "$PLAN_RUN_STATUS" = "completed" ]; then
    # Plan finished but didn't produce awaiting-approval or implementing
    # Give one more cycle in case labels are delayed
    sleep "$POLL_INTERVAL"
    LABELS_RECHECK=$(gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}" \
      --jq '[.labels[].name] | join(",")' 2>/dev/null) || LABELS_RECHECK=""
    if echo "$LABELS_RECHECK" | grep -qE 'ai:awaiting-approval|ai:implementing'; then
      continue
    fi
    # Check if other Plan runs for OUR issue are still queued or
    # in-progress. The "completed" run may be a spurious trigger
    # (e.g. a non-/answer comment that caused the job to be
    # skipped while the real Plan run is still working). Filter by
    # display_title (== issue title for issue_comment events) so we
    # only count Plan runs for OUR issue, and use per_page=100
    # (the API max) so high concurrent workflow-run volume in the
    # release window doesn't truncate our run off the response.
    _plan_runs_json_valid() {
      printf '%s' "$1" \
        | jq -se 'length == 1 and (.[0] | type == "object" and (.workflow_runs | type == "array"))' \
        >/dev/null 2>&1
    }
    PLAN_RUNS_JSON=$(fetch_plan_runs_json || echo "")
    if ! _plan_runs_json_valid "$PLAN_RUNS_JSON"; then
      echo "  ⏳ Unable to confirm concurrent Plan runs yet — retrying"
      fail_plan_confirm_retry_if_idle
      sleep "$POLL_INTERVAL"
      continue
    fi
    # Issue #9473: a queued Plan run older than ZOMBIE_RUN_MAX_QUEUED_HOURS
    # (gh_helpers.sh, default 24, 0 disables) is wedged (zero jobs,
    # cannot be cancelled) and is not active. The helpers run in a
    # subshell so this step's shell is not changed; when they are
    # unavailable every queued run stays active.
    if [ -z "${PLAN_ZQ_CUTOFF:-}" ]; then
      # stderr is kept so an invalid ZOMBIE_RUN_MAX_QUEUED_HOURS warning shows.
      PLAN_ZQ_CUTOFF="$(bash -c '. scripts/gh_helpers.sh >/dev/null 2>&1; declare -F zombie_queued_cutoff_epoch >/dev/null || exit 1; zombie_queued_cutoff_epoch' || echo 0)"
      [[ "$PLAN_ZQ_CUTOFF" =~ ^[0-9]+$ ]] || PLAN_ZQ_CUTOFF=0
      PLAN_ZQ_DEF="$(bash -c '. scripts/gh_helpers.sh >/dev/null 2>&1; zombie_queued_jq_def' 2>/dev/null || true)"
      [ -n "$PLAN_ZQ_DEF" ] || PLAN_ZQ_DEF='def is_wedged_queued($cutoff): false;'
    fi
    # Log each wedged run once, and only runs the guard below would count
    # (Plan name, our title, active status). The ids already logged are
    # kept in this shell, so the helper subshell starts only for new ones.
    if [ "$PLAN_ZQ_CUTOFF" -gt 0 ]; then
      PLAN_ZQ_NEW="$(printf '%s' "$PLAN_RUNS_JSON" \
        | jq -c --arg title "${ISSUE_TITLE}" --argjson zq_cutoff "$PLAN_ZQ_CUTOFF" "$PLAN_ZQ_DEF"'[.workflow_runs[]? | select((.name // "") | test("Plan"; "i")) | select(.display_title == $title) | select(.status == "in_progress" or .status == "queued") | select(is_wedged_queued($zq_cutoff))]' 2>/dev/null \
        | jq -c --arg seen ",${PLAN_ZQ_SEEN:-}," '[.[] | ((.id | tostring) as $zq_id | select(($seen | contains("," + $zq_id + ",")) | not))]' 2>/dev/null || echo '[]')"
      if [ -n "$PLAN_ZQ_NEW" ] && [ "$PLAN_ZQ_NEW" != "[]" ]; then
        printf '%s' "$PLAN_ZQ_NEW" \
          | bash -c '. scripts/gh_helpers.sh >/dev/null 2>&1 && zombie_queued_log_ignored stable_gate_plan_runs "$1"' _ "$PLAN_ZQ_CUTOFF" >/dev/null || true
        PLAN_ZQ_SEEN="${PLAN_ZQ_SEEN:-}$(printf '%s' "$PLAN_ZQ_NEW" | jq -r '[.[].id | tostring] | join(",")' 2>/dev/null || true),"
      fi
    fi
    OTHER_ACTIVE_PLAN_RUNS=0
    if _OTHER_ACTIVE_PLAN_RUNS=$(printf '%s' "$PLAN_RUNS_JSON" \
      | jq -r --arg title "${ISSUE_TITLE}" --argjson zq_cutoff "$PLAN_ZQ_CUTOFF" "$PLAN_ZQ_DEF"'[.workflow_runs[] | select(.name | test("Plan"; "i")) | select(.display_title == $title) | select(.status == "in_progress" or .status == "queued") | select(is_wedged_queued($zq_cutoff) | not)] | length' 2>/dev/null); then
      OTHER_ACTIVE_PLAN_RUNS="$_OTHER_ACTIVE_PLAN_RUNS"
    fi
    unset _OTHER_ACTIVE_PLAN_RUNS
    if ! [[ "$OTHER_ACTIVE_PLAN_RUNS" =~ ^[0-9]+$ ]]; then
      OTHER_ACTIVE_PLAN_RUNS=0
    fi
    # Page 1 holds only the newest 100 runs of the window. An older
    # real Plan run that is still active can sit past it while
    # skipped issue_comment runs pile up (the issue #4723 shape,
    # PR #4730 review), so walk later pages of the same query with
    # the same shape guard and page cap as the run-ID capture,
    # stopping at the first page with an active run or a short page.
    if [ "$OTHER_ACTIVE_PLAN_RUNS" -eq 0 ]; then
      ACTIVE_WALK_PAGE=1
      ACTIVE_WALK_PAGE_LEN=$(printf '%s' "$PLAN_RUNS_JSON" | jq -r '.workflow_runs | length' 2>/dev/null || echo "0")
      ACTIVE_WALK_UNCONFIRMED=0
      while [[ "$ACTIVE_WALK_PAGE_LEN" =~ ^[0-9]+$ ]] && [ "$ACTIVE_WALK_PAGE_LEN" -ge 100 ] \
        && [ "$ACTIVE_WALK_PAGE" -lt "$PLAN_RUN_LOOKUP_MAX_PAGES" ]; do
        ACTIVE_WALK_PAGE=$((ACTIVE_WALK_PAGE + 1))
        ACTIVE_WALK_PAGE_JSON=$(fetch_plan_runs_page_json "$ACTIVE_WALK_PAGE" || echo "")
        if ! _plan_runs_json_valid "$ACTIVE_WALK_PAGE_JSON"; then
          ACTIVE_WALK_UNCONFIRMED=1
          break
        fi
        ACTIVE_WALK_PAGE_COUNT=$(printf '%s' "$ACTIVE_WALK_PAGE_JSON" \
          | jq -r --arg title "${ISSUE_TITLE}" --argjson zq_cutoff "$PLAN_ZQ_CUTOFF" "$PLAN_ZQ_DEF"'[.workflow_runs[] | select(.name | test("Plan"; "i")) | select(.display_title == $title) | select(.status == "in_progress" or .status == "queued") | select(is_wedged_queued($zq_cutoff) | not)] | length' 2>/dev/null || echo "0")
        if [[ "$ACTIVE_WALK_PAGE_COUNT" =~ ^[0-9]+$ ]] && [ "$ACTIVE_WALK_PAGE_COUNT" -gt 0 ]; then
          OTHER_ACTIVE_PLAN_RUNS="$ACTIVE_WALK_PAGE_COUNT"
          break
        fi
        ACTIVE_WALK_PAGE_LEN=$(printf '%s' "$ACTIVE_WALK_PAGE_JSON" | jq -r '.workflow_runs | length' 2>/dev/null || echo "0")
      done
      if [ "$ACTIVE_WALK_UNCONFIRMED" -eq 1 ]; then
        echo "  ⏳ Unable to confirm concurrent Plan runs on page ${ACTIVE_WALK_PAGE} yet — retrying"
        fail_plan_confirm_retry_if_idle
        sleep "$POLL_INTERVAL"
        continue
      fi
    fi
    if [ "$OTHER_ACTIVE_PLAN_RUNS" -gt 0 ]; then
      echo "  ⏳ Found ${OTHER_ACTIVE_PLAN_RUNS} other active Plan run(s) — waiting for the real run to finish"
      continue
    fi
    echo "::error::Plan workflow completed but issue lacks expected labels (current: ${LABELS_RECHECK:-none})"
    echo "status=plan_failed" >> "$GITHUB_OUTPUT"
    exit 1
  fi

  # Inactivity timeout
  if [ "$IDLE" -ge "$INACTIVITY_LIMIT" ]; then
    echo "::error::Plan phase stalled — no activity for ${PLAN_PHASE_TIMEOUT} minutes"
    echo "status=timeout" >> "$GITHUB_OUTPUT"
    exit 1
  fi

  REMAINING=$(( (INACTIVITY_LIMIT - IDLE + 59) / 60 ))
  echo "  ... idle ${IDLE}s (timeout in ~${REMAINING}m) — plan_run: ${PLAN_RUN_STATUS}, labels: ${LABELS:-none}"
  sleep "$POLL_INTERVAL"
done
