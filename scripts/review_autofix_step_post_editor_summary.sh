#!/usr/bin/env bash
# Body of the "Post editor summary comment" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
# GitHub does not substitute workflow expressions in a sourced script, so the
# step passes github.server_url, github.repository and github.run_id in as
# AUTOFIX_STEP_SERVER_URL, AUTOFIX_STEP_REPOSITORY and AUTOFIX_STEP_RUN_ID;
# they default to the runner's GITHUB_* values when unset.
set -euo pipefail
AUTOFIX_STEP_SERVER_URL="${AUTOFIX_STEP_SERVER_URL:-${GITHUB_SERVER_URL:-https://github.com}}"
AUTOFIX_STEP_REPOSITORY="${AUTOFIX_STEP_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
AUTOFIX_STEP_RUN_ID="${AUTOFIX_STEP_RUN_ID:-${GITHUB_RUN_ID:-}}"
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
  elif [ "${SANDBOX_PREPARE_STEP_OUTCOME:-}" = "failure" ]; then
    # The review sandbox could not be prepared (PR #6645: Docker
    # Hub answered 500/504 on the base image), so the editor never
    # ran. Name that phase and keep its last error line as evidence.
    autofix_empty_failure_reason="sandbox_prepare_failed"
    echo "AUTOFIX_SANDBOX_PREPARE_FAILED=true" >> "$GITHUB_ENV"
    if [ -n "${RUNTIME_DIR:-}" ] && [ -d "${RUNTIME_DIR}" ]; then
      sandbox_prepare_last_error="$(grep -E '^(#[0-9]+ )?(::error::|ERROR:? )' "${RUNTIME_DIR}/sandbox_prepare_stderr.txt" 2>/dev/null | tail -n 1 | sed -E 's/^#[0-9]+ //; s/^::error:://; s/^ERROR:? //' | tr -d '\r' | cut -c1-300 || true)"
      {
        echo "sandbox_prepare_failed=true"
        grep -E '^REVIEW_SANDBOX_BUILD .*outcome=fail' "${RUNTIME_DIR}/sandbox_prepare_stderr.txt" 2>/dev/null | tail -n 1 || true
        if [ -n "${sandbox_prepare_last_error}" ]; then
          printf '::error::Review sandbox prepare failed: %s\n' "${sandbox_prepare_last_error}"
        else
          echo "::error::Review sandbox prepare failed"
        fi
      } > "${RUNTIME_DIR}/sandbox_prepare_failure_evidence.txt"
    fi
    sandbox_prepare_headline=""
    if [ -n "${AUTOFIX_FAILURE_HEAL_PY:-}" ] && [ -f "${AUTOFIX_FAILURE_HEAL_PY}" ]; then
      sandbox_prepare_headline="$(PYTHONDONTWRITEBYTECODE=1 python3 "${AUTOFIX_FAILURE_HEAL_PY}" failure-headline \
        --evidence-file "${RUNTIME_DIR:-}/sandbox_prepare_failure_evidence.txt" 2>/dev/null | sed -n 's/^first_error=//p' | head -n 1 || true)"
    fi
    noop_explanation="The review sandbox could not be prepared (step \"Install project dependencies\"), so the editor never ran and no file changes were committed. ${sandbox_prepare_headline:-The prepare error is in the workflow run log.}"
    echo "AUTOFIX_SANDBOX_PREPARE_FAILED reason=sandbox_prepare_failed error_present=$([ -n "${sandbox_prepare_headline}" ] && echo 1 || echo 0)"
  fi
  # Model-provider outage (issue #6633): a live probe confirms the
  # log evidence, so the reason is provider_unavailable (no cap
  # count, no label, one repo-wide tracker). Fail-open.
  if [ -n "${RUNTIME_DIR:-}" ] && [ -f "${SUPPORT_SCRIPTS_DIR:-}/provider_outage.py" ]; then
    provider_outage_log_args=()
    for provider_outage_log in "${PREVIOUS_REVIEWS_DIR:-/nonexistent}"/*.log "${RUNTIME_DIR}"/summariser_*.log "${RUNTIME_DIR}/reviewers_failure_evidence.txt" "${RUNTIME_DIR}/editor_stage_stderr.txt" "${RUNTIME_DIR}/collect_metadata_stderr.txt" "${RUNTIME_DIR}/resolver_stage_stderr.txt"; do
      [ -f "${provider_outage_log}" ] && provider_outage_log_args+=(--log-file "${provider_outage_log}")
    done
    if [ "${#provider_outage_log_args[@]}" -gt 0 ]; then
      provider_outage_class="$(PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR}/provider_outage.py" classify --probe-cache "${RUNTIME_DIR}/provider_outage_probe.json" "${provider_outage_log_args[@]}" 2>/dev/null | head -n 1 || true)"
      if [[ "${provider_outage_class}" == reason=provider_unavailable* ]]; then
        provider_outage_status="$(printf '%s\n' "${provider_outage_class}" | sed -n 's/.* status=\([a-z0-9_]*\).*/\1/p' | head -n 1)"
        autofix_empty_failure_reason="provider_unavailable"
        noop_explanation="The model provider failed (status ${provider_outage_status:-unknown}, confirmed by a live probe). This is a repo-wide outage tracked on the ai:provider-outage issue, not a defect in this PR, and it does not count toward the identical-failure cap; review resumes automatically when the provider recovers."
        { echo "AUTOFIX_FAILURE_REASON=provider_unavailable"; echo "AUTOFIX_PROVIDER_STATUS=${provider_outage_status:-unknown}"; } >> "$GITHUB_ENV"
      fi
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
      --evidence-file "${RUNTIME_DIR:-}/sandbox_prepare_failure_evidence.txt" \
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
  if [ "${autofix_empty_failure_reason}" = "provider_unavailable" ]; then
    NOOP_BODY="${NOOP_BODY/produced no output — will retry/paused — model provider unavailable}"
  fi
  NOOP_BODY+="${AUTOFIX_FAILURE_MARKER_SUFFIX}"
  gh_retry gh api "repos/${AUTOFIX_STEP_REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${NOOP_BODY}" >/dev/null 2>&1 || true
  echo "AUTOFIX_EDITOR_EMPTY_NOOP=true" >> "$GITHUB_ENV"
  exit 1
fi

# Records whether the editor wrote a real summary, before the synthetic
# fallback below fills the file in (#8321: the sandbox-excluded escalation
# reads only a real summary).
editor_summary_is_real=0
[ -s "${EDITOR_SUMMARY_FILE}" ] && editor_summary_is_real=1

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

# Sandbox-excluded escalation (#8321). PR #8250 was re-reviewed 29 times on
# unchanged heads because the editor declined its only fix as
# "sandbox-excluded path" and the run still ended as a success, so nothing
# counted it. When the editor's real summary reports "Change status:
# not-edited", nothing was committed, and at least one "Ignored suggestions"
# bullet ends with the fixed reason "sandbox-excluded path" (the wording the
# editor prompt in review_apply_fixes.sh pins), the posted summary ends with a
# review-autofix-failure:v1 marker for the non-retryable reason
# review_editor_sandbox_path_excluded. The gate's identical-failure cap then
# stops the head on the next dispatch; a new push resets it. The marker rides
# on this step's existing comment (no extra API call). Only the integer count
# is logged, never summary text. Fail-open: no helper or no marker leaves the
# run as it was, and the step's exit status is unchanged.
# "Nothing committed" needs positive evidence: the commit step writes "- none"
# or a "- commit skipped (...)" marker line when it committed nothing, and one
# "- <path>" line per committed file otherwise. An unset or missing file is
# unknown, so no marker is written (fail closed toward not capping).
sandbox_excluded_marker_suffix=""
sandbox_excluded_nothing_committed=0
if [ -n "${COMMITTED_FILES_FILE:-}" ] && [ -f "${COMMITTED_FILES_FILE}" ] && [ -r "${COMMITTED_FILES_FILE}" ] \
  && ! grep -q -v -E '^[[:space:]]*$|^- none$|^- none \(edits withheld for safety: .*\)$|^- commit skipped \(.*\)$' "${COMMITTED_FILES_FILE}"; then
  sandbox_excluded_nothing_committed=1
fi
# The commit step's own flag wins over the file text: a committed path that
# prints like a placeholder line never counts as nothing committed.
if [ "${DID_COMMIT:-}" = "true" ]; then
  sandbox_excluded_nothing_committed=0
fi
if [ "${editor_summary_is_real}" = "1" ] && [ "${sandbox_excluded_nothing_committed}" = "1" ]; then
  sandbox_excluded_findings="$(tr -d '\r' < "${EDITOR_SUMMARY_FILE}" | awk '
    BEGIN { section = ""; status = ""; count = 0; trail = "[] \t.\"\047`)*]+$" }
    /^(Changes made|Change status|Already satisfied \(suggested but already present\)|Ignored suggestions \(with short reason\)|Reviewer files processed|Review file issue audit|PR comment audit|Regression fingerprint|Runtime failure path):[ \t]*$/ {
      section = $0
      sub(/:[ \t]*$/, "", section)
      next
    }
    section == "Change status" && status == "" && /^[ \t]*-[ \t]+/ {
      value = $0
      sub(/^[ \t]*-[ \t]+/, "", value)
      sub(/[ \t]+$/, "", value)
      status = tolower(value)
      next
    }
    section == "Ignored suggestions (with short reason)" && /^[ \t]*-[ \t]+/ {
      value = tolower($0)
      sub(trail, "", value)
      if (value ~ /sandbox-excluded path$/) count++
    }
    END { print (status == "not-edited" ? count : 0) }
  ' 2>/dev/null || echo 0)"
  case "${sandbox_excluded_findings}" in
    ''|*[!0-9]*) sandbox_excluded_findings=0 ;;
  esac
  if [ "${sandbox_excluded_findings}" -gt 0 ]; then
    sandbox_excluded_outcome="marker_unavailable"
    sandbox_excluded_reason=" reason=helper_missing"
    if [ -n "${AUTOFIX_FAILURE_HEAL_PY:-}" ] && [ -f "${AUTOFIX_FAILURE_HEAL_PY}" ]; then
      sandbox_excluded_reason=" reason=no_marker"
      sandbox_excluded_marker_line="$(PYTHONDONTWRITEBYTECODE=1 python3 "${AUTOFIX_FAILURE_HEAL_PY}" autofix-failure-fingerprint \
        --failure-reason review_editor_sandbox_path_excluded \
        --head-sha "${AUTOFIX_FAILURE_HEAD_SHA:-}" \
        --support-sha "${REVIEW_SUPPORT_SHA:-}" \
        --run-id "${GITHUB_RUN_ID:-}" 2>/dev/null | sed -n 's/^marker=//p' | head -n 1 || true)"
      if [ -n "${sandbox_excluded_marker_line}" ]; then
        sandbox_excluded_marker_suffix=$'\n\n'"${sandbox_excluded_marker_line}"
        sandbox_excluded_outcome="marker"
        sandbox_excluded_reason=""
      fi
    fi
    sandbox_excluded_head="$(printf '%s' "${AUTOFIX_FAILURE_HEAD_SHA:-}" | tr 'A-F' 'a-f')"
    [[ "${sandbox_excluded_head}" =~ ^[0-9a-f]{40}$ ]] || sandbox_excluded_head="unknown"
    sandbox_excluded_pr="${PR_NUMBER:-}"
    [[ "${sandbox_excluded_pr}" =~ ^[0-9]+$ ]] || sandbox_excluded_pr="unknown"
    echo "AUTOFIX_SANDBOX_EXCLUDED_ESCALATION pr=${sandbox_excluded_pr} head=${sandbox_excluded_head} findings=${sandbox_excluded_findings} outcome=${sandbox_excluded_outcome}${sandbox_excluded_reason}"
  fi
fi

if gh_retry gh api repos/${AUTOFIX_STEP_REPOSITORY}/issues/"${PR_NUMBER}"/comments -f body="$(cat "${PR_EDITOR_COMMENT_FILE}")${sandbox_excluded_marker_suffix}" >/dev/null; then
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
