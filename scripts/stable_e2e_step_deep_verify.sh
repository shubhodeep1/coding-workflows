#!/usr/bin/env bash
# shellcheck shell=bash
# Body of the "Deep verify: inspect all workflow run steps" step of the e2e-smoke-test job in
# .github/workflows/test-and-mark-stable.yml, moved here verbatim to keep
# that workflow at or under the 430,000-byte headroom ceiling
# (tests/test_test_and_mark_stable_step_scripts_contract.py). The step keeps
# its name, id and env in the workflow; its run: block checks
# STABLE_E2E_STEP_SCRIPTS_VERIFIED and sources a github.sha-pinned copy
# of this file from RUNNER_TEMP, so exit/GITHUB_OUTPUT act on the step.
set -euo pipefail

# Issue #10225: the jobs reads below go through gh_api_retry
# (scripts/gh_helpers.sh), which waits for a rate-limit reset instead of
# failing. A missing helper fails the step (fail closed).
if ! declare -F gh_api_retry >/dev/null 2>&1; then
  source scripts/gh_helpers.sh
fi

# deep_verify_rate_limit_max_wait: the longest single rate-limit wait a
# deep-verify read may take (review of #10225). Same formula as
# phase7_rate_limit_max_wait in stable_e2e_step_verify_cancel_on_close.sh:
# the smaller of 600 s and half the time left before the finalization
# reserve (E2E_JOB_STARTED_EPOCH + (E2E_JOB_TIMEOUT_MINUTES -
# E2E_FINALIZATION_RESERVE_MINUTES) * 60), floored at 0. It is computed per
# call, so repeated reads can never wait past that cap together. Without a
# recorded job start the cap is 600.
deep_verify_rate_limit_max_wait() {
  local now job_cap left
  if [[ "${E2E_JOB_STARTED_EPOCH:-}" =~ ^[0-9]+$ ]] && [[ "${E2E_JOB_TIMEOUT_MINUTES:-}" =~ ^[0-9]+$ ]]; then
    now=$(date +%s)
    job_cap=$(( E2E_JOB_STARTED_EPOCH + (E2E_JOB_TIMEOUT_MINUTES - ${E2E_FINALIZATION_RESERVE_MINUTES:-20}) * 60 ))
    left=$(( (job_cap - now) / 2 ))
    if [ "${left}" -lt 0 ]; then
      left=0
    fi
    if [ "${left}" -lt 600 ]; then
      echo "${left}"
      return 0
    fi
  fi
  echo 600
}

# deep_verify_gh_api_read ARGS...: gh_api_retry with the bounded wait above.
deep_verify_gh_api_read() {
  local dv_max_wait
  dv_max_wait=$(deep_verify_rate_limit_max_wait)
  GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS="${dv_max_wait}" gh_api_retry "$@"
}

DEEP_PASS=true
FAILED_STEPS=""
SKIPPED_STEPS=""
TOTAL_CHECKED=0
TOTAL_OK=0
TOTAL_FAILED=0
TOTAL_SKIPPED=0

# ── Helper: check ALL steps in a workflow run ──
check_all_steps() {
  local run_id="$1"
  local phase_name="$2"

  if [ -z "${run_id}" ] || [ "${run_id}" = "null" ]; then
    echo ""
    echo "  ⚠ ${phase_name}: workflow run ID not captured — cannot verify steps"
    DEEP_PASS=false
    FAILED_STEPS="${FAILED_STEPS}\n  - ${phase_name}: run ID not found (phase may not have triggered)"
    return 0
  fi

  echo ""
  echo "  ┌─ ${phase_name} (run #${run_id})"

  # Fetch all jobs and their steps (retry up to 3 times on transient failures).
  #
  # IMPORTANT: Do NOT use `JOBS_JSON=$(gh api ... || echo fallback)`.
  # On non-zero exit, `gh api` still writes the API error body (e.g.
  # `{"message":"Not Found",...}`) to stdout; inside `$(...)`, the
  # `|| echo` branch runs anyway and its output is *concatenated*
  # with gh's stdout, producing a multi-document JSON blob. Downstream
  # `jq '.jobs | length'` then emits multiple lines (one per document),
  # `[ "$TOTAL_JOBS" -eq 0 ]` trips with "integer expression expected",
  # and `jq '[.jobs[] | select(...)] | length'` errors with "Cannot
  # iterate over null (null)" on any error-body document whose .jobs
  # key is missing. Always assign the fallback outside the command
  # substitution so the two outputs can't concatenate.
  #
  # Single source of truth for the schema predicate. Uses `jq -se`
  # (slurp + exit-status) so multi-document input fails the
  # predicate — without `-s`, `jq -e` evaluates each document
  # independently and exits with the truthiness of the *last*
  # document, so a multi-doc blob whose tail happens to satisfy
  # the schema would pass through and break downstream
  # `jq '.jobs | length'` with mixed-line output. Extracted into a
  # helper so the outer retry and the indexing-delay recheck stay
  # in lock-step.
  _jobs_json_valid() {
    printf '%s' "$1" \
      | jq -se 'length == 1 and (.[0] | type == "object" and (.jobs | type == "array"))' \
      >/dev/null 2>&1
  }

  JOBS_JSON='{"jobs":[]}'
  # rc of the latest jobs listing read: a failed read is "listing
  # unavailable", never "no jobs" (review of #10225).
  local dv_last_rc=0
  # 1 once any listing read of this run succeeded (even with zero jobs): a
  # later failed recheck then leaves the "no jobs" verdict, not "unavailable".
  local dv_listing_read_ok=0
  for _attempt in 1 2 3; do
    local dv_read_rc=0
    _JOBS_OUT=$(deep_verify_gh_api_read "repos/${TEST_REPO}/actions/runs/${run_id}/jobs?per_page=100" 2>/dev/null) || dv_read_rc=$?
    dv_last_rc="${dv_read_rc}"
    if [ "${dv_read_rc}" -eq 0 ]; then
      JOBS_JSON="$_JOBS_OUT"
      # Only a well-formed jobs listing counts as read (review of #10225).
      if _jobs_json_valid "$JOBS_JSON"; then
        dv_listing_read_ok=1
      fi
      break
    fi
    # rc 75: the helper already waited what the job's budget allows and
    # gave up on a rate limit; another round would only repeat that.
    if [ "${dv_read_rc}" -eq 75 ]; then
      echo "  │  ⚠ API rate-limited (gh_api_retry rc=75) — not retrying"
      break
    fi
    # rc 2: a permanent answer (e.g. 404); a retry cannot change it.
    if [ "${dv_read_rc}" -eq 2 ]; then
      echo "  │  ⚠ API read failed permanently (gh_api_retry rc=2) — not retrying"
      break
    fi
    echo "  │  ⚠ API attempt ${_attempt}/3 failed — retrying in 2s..."
    sleep 2
  done
  unset _JOBS_OUT
  # Shape check: reject anything that isn't a single JSON object
  # with a `.jobs` array. Catches (a) all-retries-failed with an
  # empty JOBS_JSON, (b) error-body responses that happen to parse,
  # and (c) any future accidental multi-doc concatenation.
  if ! _jobs_json_valid "$JOBS_JSON"; then
    JOBS_JSON='{"jobs":[]}'
  fi

  # Check if all jobs in this run were skipped (spurious re-trigger where
  # the reusable workflow's job-level if: condition was false). This commonly
  # happens when a comment event re-triggers internal-clarify.yml or
  # internal-implement.yml but the reusable workflow skips the job.
  TOTAL_JOBS=$(echo "$JOBS_JSON" | jq '.jobs | length')
  SKIPPED_JOBS=$(echo "$JOBS_JSON" | jq '[.jobs[] | select(.conclusion == "skipped")] | length')
  if [ "$TOTAL_JOBS" -eq 0 ] && [ "${dv_last_rc}" -ne 75 ] && [ "${dv_last_rc}" -ne 2 ]; then
    # GitHub can briefly report zero jobs before the run is fully indexed.
    # Re-fetch a few times so we don't false-pass real runs as spurious.
    echo "  │  ⚠ Run has no jobs yet — rechecking for indexing delay..."
    for _jobs_retry in 1 2 3; do
      sleep $((_jobs_retry * 2))
      # Same hazard as the outer retry — assign fallback outside the
      # command substitution so gh's error-body stdout cannot
      # concatenate with the literal fallback JSON.
      local dv_recheck_rc=0
      _JOBS_OUT=$(deep_verify_gh_api_read "repos/${TEST_REPO}/actions/runs/${run_id}/jobs?per_page=100" 2>/dev/null) || dv_recheck_rc=$?
      dv_last_rc="${dv_recheck_rc}"
      if [ "${dv_recheck_rc}" -eq 0 ]; then
        JOBS_JSON="$_JOBS_OUT"
        if _jobs_json_valid "$JOBS_JSON"; then
          dv_listing_read_ok=1
        fi
      else
        JOBS_JSON='{"jobs":[]}'
      fi
      unset _JOBS_OUT
      if [ "${dv_recheck_rc}" -eq 75 ]; then
        echo "  │  ⚠ API rate-limited during recheck (gh_api_retry rc=75) — not retrying"
        break
      fi
      if [ "${dv_recheck_rc}" -eq 2 ]; then
        echo "  │  ⚠ API read failed permanently during recheck (gh_api_retry rc=2) — not retrying"
        break
      fi
      if ! _jobs_json_valid "$JOBS_JSON"; then
        JOBS_JSON='{"jobs":[]}'
      fi
      TOTAL_JOBS=$(echo "$JOBS_JSON" | jq '.jobs | length')
      SKIPPED_JOBS=$(echo "$JOBS_JSON" | jq '[.jobs[] | select(.conclusion == "skipped")] | length')
      [ "$TOTAL_JOBS" -gt 0 ] && break
    done
  fi
  # Fail closed: the steps of a run whose listing could not be read were
  # not verified, so it must not pass as a spurious re-trigger.
  if [ "$TOTAL_JOBS" -eq 0 ] && [ "${dv_last_rc}" -ne 0 ] && [ "${dv_listing_read_ok}" -eq 0 ]; then
    echo "  │  ✗ Jobs listing unavailable (gh_api_retry rc=${dv_last_rc}) — steps not verified"
    DEEP_PASS=false
    FAILED_STEPS="${FAILED_STEPS}\n  - ${phase_name}: jobs listing unavailable (gh_api_retry rc=${dv_last_rc})"
    echo "  └─"
    return 0
  fi
  if [ "$TOTAL_JOBS" -eq 0 ]; then
    echo "  │  ⊘ Run has no jobs after retries (spurious re-trigger) — OK"
    echo "  └─"
    return 0
  fi
  if [ "$SKIPPED_JOBS" -eq "$TOTAL_JOBS" ]; then
    echo "  │  ⊘ All ${TOTAL_JOBS} job(s) were skipped (spurious re-trigger) — OK"
    echo "  └─"
    return 0
  fi

  # Iterate every step in every job
  STEP_COUNT=$(echo "$JOBS_JSON" | jq '[.jobs[].steps[]] | length')
  if [ "$STEP_COUNT" -eq 0 ]; then
    echo "  │  ⚠ No steps found — run may still be in progress or API returned empty"
    DEEP_PASS=false
    FAILED_STEPS="${FAILED_STEPS}\n  - ${phase_name}: no steps found in run"
    echo "  └─"
    return 0
  fi

  local phase_ok=0
  local phase_fail=0
  local phase_skip=0

  # Process each step
  while IFS=$'\t' read -r step_name step_conclusion step_status; do
    TOTAL_CHECKED=$((TOTAL_CHECKED + 1))

    # Skip GitHub's internal "Set up job" / "Complete job" steps —
    # these are infra steps, not workflow logic
    if [ "$step_name" = "Set up job" ] || [ "$step_name" = "Complete job" ] || [ "$step_name" = "Post actions/checkout@v5" ]; then
      continue
    fi

    case "${step_conclusion}" in
      success)
        echo "  │  ✓ ${step_name}"
        phase_ok=$((phase_ok + 1))
        TOTAL_OK=$((TOTAL_OK + 1))
        ;;
      skipped)
        echo "  │  ⊘ ${step_name} — SKIPPED"
        phase_skip=$((phase_skip + 1))
        TOTAL_SKIPPED=$((TOTAL_SKIPPED + 1))
        SKIPPED_STEPS="${SKIPPED_STEPS}\n  - ${phase_name} → ${step_name}"
        ;;
      failure)
        echo "  │  ✗ ${step_name} — FAILED"
        phase_fail=$((phase_fail + 1))
        TOTAL_FAILED=$((TOTAL_FAILED + 1))
        DEEP_PASS=false
        FAILED_STEPS="${FAILED_STEPS}\n  - ${phase_name} → ${step_name}"
        ;;
      cancelled)
        echo "  │  ⊘ ${step_name} — CANCELLED"
        phase_skip=$((phase_skip + 1))
        TOTAL_SKIPPED=$((TOTAL_SKIPPED + 1))
        SKIPPED_STEPS="${SKIPPED_STEPS}\n  - ${phase_name} → ${step_name} (cancelled)"
        ;;
      *)
        echo "  │  ? ${step_name} — ${step_conclusion:-unknown} (status: ${step_status})"
        # In-progress or unknown — flag but don't fail
        SKIPPED_STEPS="${SKIPPED_STEPS}\n  - ${phase_name} → ${step_name} (${step_conclusion:-unknown})"
        ;;
    esac
  done < <(echo "$JOBS_JSON" | jq -r '.jobs[].steps[] | [.name, .conclusion, .status] | @tsv')

  echo "  └─ ${phase_ok} ok, ${phase_fail} failed, ${phase_skip} skipped"
}

echo "═══════════════════════════════════════════"
echo "  Deep Verification: All Workflow Steps"
echo "═══════════════════════════════════════════"

check_all_steps "${CLARIFY_RUN_ID}" "Clarify"
check_all_steps "${PLAN_RUN_ID}" "Plan"
check_all_steps "${IMPL_RUN_ID}" "Implement"
check_all_steps "${REVIEW_RUN_ID}" "Review/Edit"

echo ""
echo "───────────────────────────────────────────"
echo "  Totals: ${TOTAL_OK} ok, ${TOTAL_FAILED} failed, ${TOTAL_SKIPPED} skipped"
echo "───────────────────────────────────────────"

# Print the failed-steps list whenever deep verify did not fully pass,
# so "no steps found in run" / "run ID not found" entries (which do not
# increment TOTAL_FAILED) are still surfaced in the summary.
if [ "$DEEP_PASS" != "true" ] || [ "$TOTAL_FAILED" -gt 0 ]; then
  echo ""
  echo "  Failed steps:"
  echo -e "$FAILED_STEPS"
fi

if [ "$TOTAL_SKIPPED" -gt 0 ]; then
  echo ""
  echo "  Skipped steps:"
  echo -e "$SKIPPED_STEPS"
fi

echo ""
if [ "$DEEP_PASS" = "true" ]; then
  echo "  ✓ All steps across all phases succeeded."
  echo "deep_passed=true" >> "$GITHUB_OUTPUT"
else
  echo "  ✗ Some steps failed — see details above."
  echo "deep_passed=false" >> "$GITHUB_OUTPUT"
fi
echo "═══════════════════════════════════════════"
