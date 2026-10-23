#!/usr/bin/env bash
# shellcheck shell=bash
# Body of the "Phase 4b: Verify editor restored canary (pytest + retry)" step of the e2e-smoke-test job in
# .github/workflows/test-and-mark-stable.yml, moved here verbatim (#7991)
# to keep that workflow well under the 480,000-byte guard
# (tests/test_workflow_file_size_limit.py). The step keeps its name, id,
# if and env in the workflow; its run: block checks
# STABLE_E2E_STEP_SCRIPTS_VERIFIED and sources a github.sha-pinned copy
# of this file from RUNNER_TEMP, so exit/GITHUB_OUTPUT act on the step.
set -euo pipefail
BRANCH="ai/issue-${ISSUE_NUMBER}"
CANARY_PATH="tests/e2e_smoke_canary.txt"
# The pytest test reconstructs the issue's exact 3-line target
# from E2E_EXPECTED_RUN_ID. Issue body at line ~347 hardcodes
# `run_id: ${RUN_ID}` where RUN_ID = GITHUB_RUN_ID, so the
# same value works on both sides.
export E2E_EXPECTED_RUN_ID="${GITHUB_RUN_ID}"
export E2E_CANARY_FILE="${RUNNER_TEMP:-/tmp}/e2e_canary_fetched.txt"
# Sentinel telling the test fixture this is the production
# invocation: missing canary env vars must fail loudly here
# (a configuration bug), not skip silently.
export E2E_PHASE_4B_INVOKED=1
PYTEST_OUTPUT="${RUNNER_TEMP:-/tmp}/e2e_pytest_output.txt"

# Capture pip + apt diagnostics into a tmp file so a failed
# install isn't silently lost. If the import probe below
# fails, we surface the captured log alongside the error
# so operators triaging pytest_unavailable can see the root
# cause (network error, missing package, permission, etc.)
# without re-running the workflow with verbose flags.
PYTEST_INSTALL_LOG="${RUNNER_TEMP:-/tmp}/e2e_pytest_install.log"
: > "${PYTEST_INSTALL_LOG}"
python3 -m pip install --quiet pytest >>"${PYTEST_INSTALL_LOG}" 2>&1 || {
  echo "::warning::pip install pytest failed; trying apt fallback"
  # Refresh package indexes before the apt install — without
  # this a stale index on a long-running runner image can
  # surface as a spurious pytest_unavailable. Other workflows
  # in this repo (review_autofix.yml) precede their apt-get
  # installs the same way.
  sudo apt-get update -qq >>"${PYTEST_INSTALL_LOG}" 2>&1 || true
  sudo apt-get install -y --no-install-recommends python3-pytest >>"${PYTEST_INSTALL_LOG}" 2>&1 || true
}
# Verify pytest is actually importable. Without this, a
# double-failure in pip+apt would later surface as a
# ModuleNotFoundError that looks like a test/spec mismatch
# rather than the infrastructure error it is.
if ! python3 -c "import pytest" 2>/dev/null; then
  echo "::error::pytest is not importable after pip + apt install attempts; cannot verify canary"
  echo "::group::pytest install diagnostics"
  cat "${PYTEST_INSTALL_LOG}" || true
  echo "::endgroup::"
  echo "status=pytest_unavailable" >> "$GITHUB_OUTPUT"
  exit 1
fi

# gh api wrapper with a small retry budget so a transient
# 429/5xx doesn't bypass the editor-flake retry budget. Used
# by every gh api call in this step (fetch helpers + poll
# loop) so ordinary blips get up to 3 attempts with 2s/4s
# backoff. A known rate limit instead returns 75 immediately;
# the poll loop can then wait within its own deadline. On final
# failure ALL attempts' stderr is surfaced (not just the
# last) so triage can spot patterns like "first 2 were
# rate-limited then 3rd was 5xx".
source scripts/gh_helpers.sh
GH_API_RATE_LIMIT_RESET_FILE=$(mktemp)
GH_API_RATE_LIMIT_PROBED=0
gh_api_with_retry() {
  local out
  local rc=0
  local err_file
  local response_file
  local attempt_err
  err_file="$(mktemp)"
  response_file="$(mktemp)"
  : > "${GH_API_RATE_LIMIT_RESET_FILE}"
  for try in 1 2 3; do
    # Tag each attempt's stderr so they're distinguishable
    # in the final warning and append (>>) — overwriting
    # would lose earlier diagnostics.
    echo "--- attempt ${try} stderr ---" >> "${err_file}"
    : > "${response_file}"
    if gh api -i "$@" > "${response_file}" 2>>"${err_file}"; then
      # -i supplies headers on both success and failure. Never
      # hand headers (or a failed response body) to a caller.
      out=$(sed '1,/^\r\{0,1\}$/d' "${response_file}")
      rm -f "${err_file}" "${response_file}"
      printf '%s' "${out}"
      return 0
    else
      rc=$?
    fi
    attempt_err=$(cat "${err_file}")
    if _is_gh_rate_limit "${attempt_err} $(cat "${response_file}")"; then
      # _parse_reset_header uses arithmetic on Retry-After. Do
      # not feed it an unbounded/untrusted numeric header.
      local retry_after
      retry_after=$(grep -im1 '^retry-after:' "${response_file}" | tr -d '\r' | cut -d: -f2- | tr -d '[:space:]' || true)
      if [[ "${retry_after}" =~ ^[0-9]{1,9}$ ]]; then
        _parse_reset_header "${response_file}" > "${GH_API_RATE_LIMIT_RESET_FILE}"
      else
        grep -im1 '^x-ratelimit-reset:' "${response_file}" | cut -d: -f2- | tr -d '[:space:]\r' > "${GH_API_RATE_LIMIT_RESET_FILE}" || true
      fi
      echo "::warning::gh api rate limited; deferring retry to the bounded wait" >&2
      rm -f "${err_file}" "${response_file}"
      return 75
    fi
    [ "${try}" -lt 3 ] && sleep $((try * 2))
  done
  if [ -s "${err_file}" ]; then
    echo "::warning::gh api failed after 3 attempts (rc=${rc}): $(tr '\n' ' ' < "${err_file}" | head -c 1000)" >&2
  fi
  rm -f "${err_file}" "${response_file}"
  return "${rc}"
}

wait_for_gh_rate_limit() {
  local limit_deadline="$1"
  local reset_epoch
  local now
  local wait_secs
  reset_epoch=$(cat "${GH_API_RATE_LIMIT_RESET_FILE}")
  # Only probe once per step, and only when the failed response
  # provided neither usable Retry-After nor reset epoch.
  if ! [[ "${reset_epoch}" =~ ^[0-9]{1,12}$ ]] && [ "${GH_API_RATE_LIMIT_PROBED}" -eq 0 ]; then
    GH_API_RATE_LIMIT_PROBED=1
    reset_epoch=$(gh api /rate_limit --jq '.resources.core.reset // empty' 2>/dev/null) || reset_epoch=""
  fi
  now=$(date +%s)
  if [ "${now}" -ge "${limit_deadline}" ]; then
    return 1
  fi
  wait_secs=60  # Secondary limit with no usable reset: pause before retrying.
  if [[ "${reset_epoch}" =~ ^[0-9]{1,12}$ ]]; then
    wait_secs=$((10#${reset_epoch} - now + 1))
    [ "${wait_secs}" -lt 1 ] && wait_secs=1
  fi
  if [ "${wait_secs}" -gt $((limit_deadline - now)) ]; then
    wait_secs=$((limit_deadline - now))
  fi
  echo "::warning::GitHub rate limit; waiting ${wait_secs}s within retry deadline" >&2
  sleep "${wait_secs}"
  [ "$(date +%s)" -lt "${limit_deadline}" ]
}

fetch_canary_to_tmp() {
  # NOTE: do NOT use `blob=$(gh api ... || echo "")` — on
  # non-zero exit, `gh api` writes the error JSON body
  # (e.g. `{"message":"Not Found",...}`) to stdout, which
  # would land in $blob as a valid-looking but bogus
  # response. gh_api_with_retry returns rc!=0 on hard
  # failure and we further validate the .content field
  # before trusting the response.
  local blob_path="${RUNNER_TEMP:-/tmp}/e2e_canary_blob.json"
  : > "${blob_path}"
  if ! gh_api_with_retry "repos/${TEST_REPO}/contents/${CANARY_PATH}?ref=${BRANCH}" \
      > "${blob_path}"; then
    return 1
  fi
  if [ ! -s "${blob_path}" ]; then
    return 1
  fi
  local content_b64
  content_b64=$(jq -r '.content // empty' "${blob_path}")
  if [ -z "${content_b64}" ]; then
    return 1
  fi
  printf '%s' "${content_b64}" | tr -d '\n' | base64 -d > "${E2E_CANARY_FILE}" 2>/dev/null || return 1
  return 0
}

# Fetch PR head SHA with the same fail-loud discipline as
# fetch_canary_to_tmp. Echoes the SHA on stdout, returns rc=0
# on success and rc=1 on any API/parse failure — callers MUST
# check rc and treat empty as "infrastructure error", not as
# "head differs from bait" (the original `|| echo ""` form
# silently fail-opened the no_editor_commit guard).
fetch_pr_head_sha() {
  local resp_path="${RUNNER_TEMP:-/tmp}/e2e_pr_head_resp.json"
  : > "${resp_path}"
  if ! gh_api_with_retry "repos/${TEST_REPO}/pulls/${PR_NUMBER}" \
      > "${resp_path}"; then
    return 1
  fi
  if [ ! -s "${resp_path}" ]; then
    return 1
  fi
  local sha
  sha=$(jq -r '.head.sha // empty' "${resp_path}")
  if ! [[ "${sha}" =~ ^[0-9a-fA-F]{40}$ ]]; then
    return 1
  fi
  printf '%s' "${sha}"
  return 0
}

# Returns "open", "closed", "unknown", or "rate_limited".
# Used by the retry poll loop to bail out if
# the PR is closed/merged mid-wait — no point polling for a
# review run that won't fire on a closed PR.
fetch_pr_state() {
  local state
  local rc
  state=$(gh_api_with_retry "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.state // "unknown"') || {
    rc=$?
    if [ "${rc}" -eq 75 ]; then
      echo "rate_limited"
      return 0
    fi
    echo "unknown"
    return 0
  }
  case "${state}" in
    open|closed) echo "${state}" ;;
    *) echo "unknown" ;;
  esac
}

run_pytest() {
  : > "${PYTEST_OUTPUT}"
  # Locally disable -e so a non-zero pytest exit doesn't
  # propagate up before we can capture it. `set -e` semantics
  # for a function called from `if !` should already exempt
  # the body, but pipefail + the `tee` pipeline make the
  # interaction subtle enough to be worth pinning down
  # explicitly.
  set +e
  python3 -m pytest tests/test_e2e_editor_smoke_canary.py -v --no-header 2>&1 | tee "${PYTEST_OUTPUT}"
  local rc="${PIPESTATUS[0]}"
  set -e
  return "${rc}"
}

# When pytest fails, distinguish a true editor regression
# (test assertion failed on canary content) from a harness/
# infra error (fixture exception, pytest internal error,
# missing dependency, runner I/O failure). pytest's own
# FAILED-vs-ERROR distinction is the cleanest signal:
#
#   FAILED  → the test function ran and an assertion failed
#             → real editor/spec bug → spec_mismatch
#   ERROR   → fixture / collection / setup raised before the
#             test body could run → harness issue
#   INTERNALERROR → pytest itself crashed → harness issue
#
# Anything that's neither FAILED nor a clean pass falls into
# the harness bucket — that catches RuntimeError from the
# canary_invocation fixture, ImportError on a broken Python
# env, OSError/PermissionError on runner I/O, pytest
# UsageError, and similar infra failures that should NOT
# blame the editor.
classify_pytest_failure() {
  # Case-insensitive INTERNALERROR (-i) for forward-compat
  # against pytest output-format tweaks. ^FAILED / ^ERROR
  # stay case-sensitive — pytest's short-summary prefixes are
  # documented as uppercase and matching them case-insensitively
  # would risk false positives on user-controlled error
  # message bodies that happen to contain "failed " or "error "
  # at line start.
  #
  # ERROR is matched explicitly (rather than relying on the
  # else fallthrough) so the classification decision is
  # readable without having to know that pytest's setup/
  # collection failures emit "ERROR file::test ..." in the
  # short summary.
  if grep -qiE "INTERNALERROR" "${PYTEST_OUTPUT}" 2>/dev/null; then
    echo "harness_misconfigured"
  elif grep -qE "^ERROR " "${PYTEST_OUTPUT}" 2>/dev/null; then
    echo "harness_misconfigured"
  elif grep -qE "^FAILED " "${PYTEST_OUTPUT}" 2>/dev/null; then
    echo "spec_mismatch"
  else
    # Catch-all for unknown pytest output shapes (e.g. future
    # format changes, pytest exiting with no FAILED/ERROR
    # summary at all). Bias to harness_misconfigured rather
    # than silently misclassifying as spec_mismatch.
    echo "harness_misconfigured"
  fi
}

# ── Attempt 1 ──────────────────────────────────────────────
echo "─── Phase 4b attempt 1: fetch + pytest ───"
if ! fetch_canary_to_tmp; then
  echo "::error::Could not re-fetch ${CANARY_PATH} after review — branch may be gone"
  echo "status=fetch_failed" >> "$GITHUB_OUTPUT"
  exit 1
fi
if run_pytest; then
  echo "✓ Canary verification passed on first attempt"
  # Confirm the editor pushed at least one commit on top of the bait.
  if ! PR_HEAD=$(fetch_pr_head_sha); then
    echo "::error::Could not fetch PR head SHA to verify editor commit; treating as infra failure (refusing to fail-open the no_editor_commit guard)"
    echo "status=pr_head_fetch_failed" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  if [ "${PR_HEAD}" = "${BAIT_SHA}" ]; then
    echo "::error::PR head is still the bait commit — editor did not push a fix commit"
    echo "status=no_editor_commit" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  echo "✓ Editor produced commit(s) past the bait (PR head: ${PR_HEAD:0:7}, bait: ${BAIT_SHA:0:7})"
  echo "status=success" >> "$GITHUB_OUTPUT"
  exit 0
fi

echo "::warning::Phase 4b first attempt failed — adopting or dispatching review_autofix and retrying once"

# ── Retry: adopt active work or redispatch ${REVIEW_WORKFLOW_FILE} ──
# Mirrors the fallback dispatch in Phase 3c's
# `Inject editor bait commit on PR branch` step, which uses
# ${REVIEW_WORKFLOW_FILE} as the public entry-point that gates into
# review_autofix.yml without an `[ai-autofix]` self-trigger guard.
#
# Capture the branch head SHA before discovery. An active run
# for either the bait or current head may already be queued by
# Phase 3c or pull_request:synchronize; adopting the oldest one
# avoids adding another run behind it in pr-autofix concurrency.
#
# `gh workflow run` is dispatched with --ref "${BRANCH}"
# (its documented contract is branch/tag, not raw SHA);
# RETRY_DISPATCH_SHA and BAIT_SHA select already-active work before
# dispatch. After dispatch, the branch-scoped baseline identifies
# the oldest new run even if the branch advanced. Once selected,
# one positive run id is polled directly so a newer peer cannot
# replace it.
if ! [[ "${PRIOR_REVIEW_RUN}" =~ ^[0-9]+$ ]] || [ "${PRIOR_REVIEW_RUN}" -lt 1 ]; then
  echo "::error::wait-review.outputs.review_run_id is not a positive integer ('${PRIOR_REVIEW_RUN}'); refusing retry discovery with a no-op exclusion filter"
  echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"
  exit 1
fi
if ! RETRY_DISPATCH_SHA=$(gh_api_with_retry "repos/${TEST_REPO}/git/refs/heads/${BRANCH}" --jq '.object.sha // empty'); then
  echo "::error::Could not resolve ${BRANCH} head SHA before retry dispatch"
  echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"
  exit 1
fi
if ! [[ "${RETRY_DISPATCH_SHA}" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "::error::Branch head is not a valid 40-hex SHA before retry dispatch"
  echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"
  exit 1
fi
echo "  resolved ${BRANCH} head for retry dispatch: ${RETRY_DISPATCH_SHA:0:7}"

RETRY_RUNS_QUERY="repos/${TEST_REPO}/actions/workflows/${REVIEW_WORKFLOW_FILE}/runs?branch=${BRANCH}&per_page=100"
if ! RETRY_RUNS_JSON=$(gh_api_with_retry "${RETRY_RUNS_QUERY}"); then
  echo "::error::Could not discover existing review runs before retry dispatch"
  echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"
  exit 1
fi
if ! printf '%s\n' "${RETRY_RUNS_JSON}" | jq -e '
  type == "object" and
  (.workflow_runs | type == "array") and
  all(.workflow_runs[];
    type == "object" and
    (.id | type == "number") and .id > 0 and .id == (.id | floor) and
    (.head_sha | type == "string") and
    (.status | type == "string") and
    (.created_at | type == "string") and
    ((.conclusion == null) or (.conclusion | type == "string")))
' >/dev/null 2>&1; then
  echo "::error::Malformed review run-list payload before retry dispatch"
  echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"
  exit 1
fi
RETRY_BASELINE_ID=$(printf '%s\n' "${RETRY_RUNS_JSON}" | jq -r '[.workflow_runs[].id] | max // 0')
if ! [[ "${RETRY_BASELINE_ID}" =~ ^[0-9]+$ ]]; then
  echo "::error::Malformed review run baseline id: '${RETRY_BASELINE_ID}'"
  echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"
  exit 1
fi

RETRY_ACTIVE_RUN=$(printf '%s\n' "${RETRY_RUNS_JSON}" | jq -c \
  --arg bait_sha "${BAIT_SHA}" \
  --arg retry_sha "${RETRY_DISPATCH_SHA}" \
  --argjson prior_run_id "${PRIOR_REVIEW_RUN}" \
  '[.workflow_runs[]
    | select(.id != $prior_run_id)
    | select(.status != "completed")
    | select(.head_sha == $bait_sha or .head_sha == $retry_sha)]
  | sort_by(.created_at, .id)
  | .[0] // empty')

RETRY_RUN_ID=""
RETRY_STATUS=""
RETRY_CONCL=""
RETRY_REGISTRATION_DEADLINE=0
if [ -n "${RETRY_ACTIVE_RUN}" ]; then
  RETRY_RUN_ID=$(printf '%s\n' "${RETRY_ACTIVE_RUN}" | jq -r '.id')
  if ! [[ "${RETRY_RUN_ID}" =~ ^[0-9]+$ ]] || [ "${RETRY_RUN_ID}" -lt 1 ]; then
    echo "::error::Adopted review run id is not a positive integer: '${RETRY_RUN_ID}'"
    echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  echo "✓ Adopting active review run #${RETRY_RUN_ID} instead of dispatching duplicate work"
else
  # Same exposure as the Bug B fallback above: this retry still
  # executes TEST_REPO's bait-branch workflow, not its default copy.
  if ! gh workflow run "${REVIEW_WORKFLOW_FILE}" \
      --repo "${TEST_REPO}" \
      --ref "${BRANCH}" \
      -f pr_number="${PR_NUMBER}" >/tmp/retry_dispatch.txt 2>&1; then
    echo "::error::Retry dispatch failed; cannot recover"
    cat /tmp/retry_dispatch.txt >&2 || true
    echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  RETRY_REGISTRATION_DEADLINE=$(( $(date +%s) + 90 ))
  echo "✓ Retry dispatch sent on ${BRANCH} (head ${RETRY_DISPATCH_SHA:0:7}) — registering the oldest new branch run after baseline ${RETRY_BASELINE_ID}"
fi

# EDITOR_RETRY_BUDGET_MINUTES defaults to 25 (was 10; raised after
# run 25371432937 timed out
# during the retry's reviewer pass — full review_autofix pipeline
# is 6 reviewers + consolidator + editor + conflict resolver and
# observed to take ~20 min wall time, so 600s reliably truncated
# genuine successful retries. 25 min keeps the upper bound below
# the orchestrator phase budget while covering the observed P95).
DEADLINE=$(( $(date +%s) + (EDITOR_RETRY_BUDGET_MINUTES * 60) ))
# Time the pinned retry run spends waiting for a runner is not
# editor time. Gate run 37395952357's retry run reported
# status=queued for about 24 of its 25 minutes and succeeded six
# minutes after this deadline. Queued time seen on two consecutive
# polls extends the deadline, never past the point that still
# leaves Phase 6 (PHASE_TIMEOUT), Phase 7
# (PHASE7_WAIT_BUDGET_MINUTES) and the finalization reserve inside
# E2E_JOB_TIMEOUT_MINUTES, so the serial budget checked in
# "Validate prerequisites" still holds. Without a recorded job
# start there is no extension. The cap bounds only the extension:
# the base deadline above is never shortened (pre-#6606 behaviour),
# so a Phase 4b that starts after the cap gets no extension, not a
# smaller budget.
RETRY_BASE_DEADLINE="${DEADLINE}"
RETRY_DEADLINE_CEILING="${DEADLINE}"
if [[ "${E2E_JOB_STARTED_EPOCH:-}" =~ ^[0-9]+$ ]]; then
  RETRY_DEADLINE_CEILING=$(( E2E_JOB_STARTED_EPOCH + (E2E_JOB_TIMEOUT_MINUTES - PHASE_TIMEOUT - PHASE7_WAIT_BUDGET_MINUTES - E2E_FINALIZATION_RESERVE_MINUTES) * 60 ))
  if [ "${RETRY_DEADLINE_CEILING}" -lt "${DEADLINE}" ]; then
    RETRY_DEADLINE_CEILING="${DEADLINE}"
  fi
fi
RETRY_QUEUED_SECS=0
RETRY_LAST_QUEUED_TS=""
# Circuit breaker: if fetch_pr_state returns "unknown" (API
# failure / parse failure) for too many consecutive iterations
# we're effectively polling blind and the closed-PR guard
# above is inactive. Bail out instead of burning the full
# 25-min retry window during a GitHub outage.
PR_STATE_FAILURES=0
PR_STATE_FAILURE_LIMIT=4  # ~60s of consecutive unknowns
SKIP_POLL_SLEEP=0
while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
  if [ "${SKIP_POLL_SLEEP}" -eq 0 ]; then
    sleep 15
  fi
  SKIP_POLL_SLEEP=0
  if [ "$(date +%s)" -ge "${DEADLINE}" ]; then
    break
  fi
  if [ -z "${RETRY_RUN_ID}" ] && [ "$(date +%s)" -ge "${RETRY_REGISTRATION_DEADLINE}" ]; then
    echo "::error::Retry dispatch did not register a matching review run within 90 seconds"
    echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  # Bail out if the PR closed/merged during the wait — no
  # point polling for a review run that won't fire on a
  # closed PR.
  PR_STATE=$(fetch_pr_state)
  if [ "${PR_STATE}" = "rate_limited" ]; then
    RATE_LIMIT_DEADLINE="${DEADLINE}"
    if [ -z "${RETRY_RUN_ID}" ] && [ "${RETRY_REGISTRATION_DEADLINE}" -lt "${RATE_LIMIT_DEADLINE}" ]; then
      RATE_LIMIT_DEADLINE="${RETRY_REGISTRATION_DEADLINE}"
    fi
    if ! wait_for_gh_rate_limit "${RATE_LIMIT_DEADLINE}"; then
      if [ -z "${RETRY_RUN_ID}" ]; then
        echo "::error::Retry dispatch did not register a matching review run within 90 seconds"
        echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"
      else
        echo "::error::Retry review run did not complete within ${EDITOR_RETRY_BUDGET_MINUTES} minutes"
        echo "status=retry_timeout" >> "$GITHUB_OUTPUT"
      fi
      exit 1
    fi
    # A PR can close while we wait. Read it again immediately,
    # before any run-list/run poll or another fixed sleep.
    SKIP_POLL_SLEEP=1
    continue
  fi
  if [ "${PR_STATE}" = "closed" ]; then
    echo "::error::PR #${PR_NUMBER} closed during retry wait — abandoning retry"
    echo "status=pr_closed_during_retry" >> "$GITHUB_OUTPUT"
    exit 1
  fi
  if [ "${PR_STATE}" = "unknown" ]; then
    PR_STATE_FAILURES=$((PR_STATE_FAILURES + 1))
    if [ "${PR_STATE_FAILURES}" -ge "${PR_STATE_FAILURE_LIMIT}" ]; then
      echo "::error::PR state unresolvable after ${PR_STATE_FAILURES} consecutive attempts (likely GitHub API outage) — bailing out instead of burning the full retry window"
      echo "status=pr_state_check_failed" >> "$GITHUB_OUTPUT"
      exit 1
    fi
  else
    # Reset on any successful resolve so we only bail on
    # *consecutive* failures, not cumulative.
    PR_STATE_FAILURES=0
  fi
  # A completed run never establishes that a PR is still open.
  if [ "${PR_STATE}" != "open" ]; then
    continue
  fi
  if [ -z "${RETRY_RUN_ID}" ]; then
    if [ "$(date +%s)" -ge "${RETRY_REGISTRATION_DEADLINE}" ]; then
      echo "::error::Retry dispatch did not register a matching review run within 90 seconds"
      echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"
      exit 1
    fi
    if RETRY_RUNS_JSON=$(gh_api_with_retry "${RETRY_RUNS_QUERY}"); then
      :
    else
      RATE_LIMIT_RC=$?
      if [ "${RATE_LIMIT_RC}" -eq 75 ]; then
        if ! wait_for_gh_rate_limit "${RETRY_REGISTRATION_DEADLINE}"; then
          echo "::error::Retry dispatch did not register a matching review run within 90 seconds"
          echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"
          exit 1
        fi
        SKIP_POLL_SLEEP=1
        continue
      fi
      echo "::warning::Could not list post-dispatch review runs; retrying within the registration window"
      continue
    fi
    if ! printf '%s\n' "${RETRY_RUNS_JSON}" | jq -e '
      type == "object" and
      (.workflow_runs | type == "array") and
      all(.workflow_runs[];
        type == "object" and
        (.id | type == "number") and .id > 0 and .id == (.id | floor) and
        (.head_sha | type == "string") and
        (.status | type == "string") and
        (.created_at | type == "string") and
        ((.conclusion == null) or (.conclusion | type == "string")))
    ' >/dev/null 2>&1; then
      echo "::warning::Malformed post-dispatch review run-list payload; retrying within the registration window"
      continue
    fi
    RETRY_RUN_ID=$(printf '%s\n' "${RETRY_RUNS_JSON}" | jq -r \
      --argjson prior_run_id "${PRIOR_REVIEW_RUN}" \
      --argjson baseline_id "${RETRY_BASELINE_ID}" \
      '[.workflow_runs[]
        | select(.id > $baseline_id and .id != $prior_run_id)]
      | sort_by(.created_at, .id)
      | .[0].id // empty')
    if [ -z "${RETRY_RUN_ID}" ]; then
      echo "  (retry review run not registered yet, pr_state=${PR_STATE})"
      continue
    fi
    if ! [[ "${RETRY_RUN_ID}" =~ ^[0-9]+$ ]] || [ "${RETRY_RUN_ID}" -lt 1 ]; then
      echo "::error::Registered review run id is not a positive integer: '${RETRY_RUN_ID}'"
      echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"
      exit 1
    fi
    echo "✓ Registered retry review run #${RETRY_RUN_ID}; polling that exact run"
  fi

  if RETRY_RUN_JSON=$(gh_api_with_retry "repos/${TEST_REPO}/actions/runs/${RETRY_RUN_ID}"); then
    :
  else
    RATE_LIMIT_RC=$?
    if [ "${RATE_LIMIT_RC}" -eq 75 ]; then
      if ! wait_for_gh_rate_limit "${DEADLINE}"; then
        break
      fi
      SKIP_POLL_SLEEP=1
      continue
    fi
    echo "::warning::Could not read retry review run #${RETRY_RUN_ID}; retrying within the retry window"
    continue
  fi
  if ! printf '%s\n' "${RETRY_RUN_JSON}" | jq -e \
    --argjson retry_run_id "${RETRY_RUN_ID}" \
    'type == "object" and .id == $retry_run_id and (.status | type == "string") and ((.conclusion == null) or (.conclusion | type == "string"))' \
    >/dev/null 2>&1; then
    echo "::warning::Malformed payload for retry review run #${RETRY_RUN_ID}; retrying within the retry window"
    continue
  fi
  RETRY_STATUS=$(printf '%s\n' "${RETRY_RUN_JSON}" | jq -r '.status')
  RETRY_CONCL=$(printf '%s\n' "${RETRY_RUN_JSON}" | jq -r '.conclusion // "null"')
  echo "  retry run #${RETRY_RUN_ID}: status=${RETRY_STATUS} conclusion=${RETRY_CONCL}"
  if [ "${RETRY_STATUS}" = "completed" ]; then
    break
  fi
  RETRY_POLL_NOW=$(date +%s)
  case "${RETRY_STATUS}" in
    queued|waiting|pending|requested)
      if [ -n "${RETRY_LAST_QUEUED_TS}" ]; then
        RETRY_QUEUED_SECS=$(( RETRY_QUEUED_SECS + RETRY_POLL_NOW - RETRY_LAST_QUEUED_TS ))
        DEADLINE=$(( RETRY_BASE_DEADLINE + RETRY_QUEUED_SECS ))
        if [ "${DEADLINE}" -gt "${RETRY_DEADLINE_CEILING}" ]; then
          DEADLINE="${RETRY_DEADLINE_CEILING}"
        fi
      fi
      RETRY_LAST_QUEUED_TS="${RETRY_POLL_NOW}"
      ;;
    *)
      RETRY_LAST_QUEUED_TS=""
      ;;
  esac
done

if [ "$(date +%s)" -ge "${DEADLINE}" ] || [ -z "${RETRY_RUN_ID}" ] || [ "${RETRY_STATUS:-}" != "completed" ]; then
  echo "Retry review run queued for ${RETRY_QUEUED_SECS}s; deadline extended by $(( DEADLINE - RETRY_BASE_DEADLINE ))s (cap $(( RETRY_DEADLINE_CEILING - RETRY_BASE_DEADLINE ))s)."
  echo "::error::Retry review run did not complete within ${EDITOR_RETRY_BUDGET_MINUTES} minutes"
  echo "status=retry_timeout" >> "$GITHUB_OUTPUT"
  exit 1
fi

# Validate the retry run's conclusion. If review_autofix
# itself errored (failure / cancelled / timed_out), running
# attempt-2 pytest against the resulting unchanged canary
# would surface as spec_mismatch and falsely blame the
# editor model. Report retry_workflow_failed so operators
# look at the retry run's logs, not the editor.
if [ "${RETRY_CONCL}" != "success" ]; then
  echo "::error::Retry review run #${RETRY_RUN_ID} completed with conclusion=${RETRY_CONCL} (not 'success'); refusing to attribute the resulting canary state to the editor"
  echo "status=retry_workflow_failed" >> "$GITHUB_OUTPUT"
  exit 1
fi

# ── Attempt 2 ──────────────────────────────────────────────
echo "─── Phase 4b attempt 2: re-fetch + pytest after retry ───"
if ! fetch_canary_to_tmp; then
  echo "::error::Could not re-fetch ${CANARY_PATH} after retry"
  echo "status=fetch_failed_after_retry" >> "$GITHUB_OUTPUT"
  exit 1
fi
if ! run_pytest; then
  FAILURE_KIND=$(classify_pytest_failure)
  if [ "${FAILURE_KIND}" = "harness_misconfigured" ]; then
    echo "::error::Phase 4b pytest fixture raised RuntimeError or pytest had INTERNALERROR — harness misconfigured (env var / file path / pytest setup), not an editor regression"
    echo "status=harness_misconfigured" >> "$GITHUB_OUTPUT"
  else
    echo "::error::Canary verification still failed after retry — editor is broken or model fully unavailable"
    echo "status=spec_mismatch" >> "$GITHUB_OUTPUT"
  fi
  exit 1
fi
echo "✓ Canary verification passed on retry"

if ! PR_HEAD=$(fetch_pr_head_sha); then
  echo "::error::Could not fetch PR head SHA after retry; treating as infra failure"
  echo "status=pr_head_fetch_failed" >> "$GITHUB_OUTPUT"
  exit 1
fi
if [ "${PR_HEAD}" = "${BAIT_SHA}" ]; then
  echo "::error::PR head is still the bait commit after retry — editor did not push a fix commit"
  echo "status=no_editor_commit" >> "$GITHUB_OUTPUT"
  exit 1
fi
echo "✓ Editor produced commit(s) past the bait on retry (PR head: ${PR_HEAD:0:7}, bait: ${BAIT_SHA:0:7})"
echo "status=success_after_retry" >> "$GITHUB_OUTPUT"
# Explicit exit 0 — symmetric with the first-attempt success
# path above, and a guard against future code being added
# past this echo without realising the step was already done.
exit 0
