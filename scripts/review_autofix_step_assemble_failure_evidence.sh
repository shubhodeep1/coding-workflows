#!/usr/bin/env bash
# Body of the review_autofix.yml "Assemble failure evidence" step (sourced by
# the step; moved out to keep the workflow under GitHub's 512,000-byte limit).
# Issue #6633 adds the model-provider outage classification.
set -uo pipefail
# Name the failure for the PR comment (fail-open): the job's first
# failed step, from one jobs-API call (CLAUDE.md §15), and the first
# specific ::error:: line of the captured stage stderr, redacted.
failed_step="$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/attempts/${GITHUB_RUN_ATTEMPT:-1}/jobs?per_page=100" \
  --jq '[.jobs[] | select(.runner_name == env.RUNNER_NAME) | .steps[] | select(.conclusion == "failure") | .name][0] // empty' 2>/dev/null | head -n 1 | tr -d '\r' | cut -c1-200 || true)"
echo "AUTOFIX_FAILED_STEP=${failed_step}" >> "$GITHUB_ENV"
if [ -n "${AUTOFIX_FAILURE_HEAL_PY:-}" ] && [ -f "${AUTOFIX_FAILURE_HEAL_PY}" ]; then
  first_error="$(PYTHONDONTWRITEBYTECODE=1 python3 "${AUTOFIX_FAILURE_HEAL_PY}" failure-headline \
    --evidence-file "${RUNTIME_DIR:-}/collect_metadata_stderr.txt" \
    --evidence-file "${RUNTIME_DIR:-}/resolver_stage_stderr.txt" \
    --evidence-file "${RUNTIME_DIR:-}/reviewers_failure_evidence.txt" \
    --evidence-file "${RUNTIME_DIR:-}/sandbox_prepare_failure_evidence.txt" \
    --evidence-file "${RUNTIME_DIR:-}/editor_stage_stderr.txt" 2>/dev/null | sed -n 's/^first_error=//p' | head -n 1 || true)"
  echo "AUTOFIX_FAILURE_FIRST_ERROR=${first_error}" >> "$GITHUB_ENV"
fi
echo "AUTOFIX_FAILURE_HEADLINE step=${failed_step:-unknown} first_error_present=$([ -n "${first_error:-}" ] && echo 1 || echo 0)"
if [ -z "${AUTOFIX_FAILURE_HEAL_PY:-}" ] || [ ! -f "${AUTOFIX_FAILURE_HEAL_PY}" ]; then
  echo "AUTOFIX_FAILURE_MARKER=" >> "$GITHUB_ENV"
  echo "AUTOFIX_FINGERPRINT pr=${PR_NUMBER:-} head=${AUTOFIX_FAILURE_HEAD_SHA:-unknown} degraded=1 reason=helper_missing"
  exit 0
fi
# Model-provider outage (issue #6633): evidence in the pipeline logs that a
# live probe confirms names the failure provider_unavailable, so the gate's
# identical-failure cap never counts it and no ai:review-blocked label,
# per-PR alert or heal report follows. Fail-open: no helper, no key or an
# inconclusive probe keeps the reason derived below.
provider_outage_reason_args=()
provider_outage_helper="${SUPPORT_SCRIPTS_DIR:-}/provider_outage.py"
if [ "${AUTOFIX_FAILURE_REASON:-}" = "provider_unavailable" ]; then
  provider_outage_reason_args=(--failure-reason provider_unavailable)
elif [ -f "${provider_outage_helper}" ]; then
  provider_outage_log_args=()
  for provider_outage_log in "${PREVIOUS_REVIEWS_DIR:-/nonexistent}"/*.log "${RUNTIME_DIR:-/nonexistent}"/summariser_*.log \
    "${RUNTIME_DIR:-/nonexistent}/reviewers_failure_evidence.txt" "${RUNTIME_DIR:-/nonexistent}/editor_stage_stderr.txt" \
    "${RUNTIME_DIR:-/nonexistent}/collect_metadata_stderr.txt" "${RUNTIME_DIR:-/nonexistent}/resolver_stage_stderr.txt"; do
    [ -f "${provider_outage_log}" ] && provider_outage_log_args+=(--log-file "${provider_outage_log}")
  done
  if [ "${#provider_outage_log_args[@]}" -gt 0 ]; then
    provider_outage_class="$(PYTHONDONTWRITEBYTECODE=1 python3 "${provider_outage_helper}" classify --probe-cache "${RUNTIME_DIR:-/tmp}/provider_outage_probe.json" "${provider_outage_log_args[@]}" 2>/dev/null | head -n 1 || true)"
    if [[ "${provider_outage_class}" == reason=provider_unavailable* ]]; then
      provider_outage_status="$(printf '%s\n' "${provider_outage_class}" | sed -n 's/.* status=\([a-z0-9_]*\).*/\1/p' | head -n 1)"
      export AUTOFIX_FAILURE_REASON=provider_unavailable
      # Persist now: the fingerprint helper's failure path exits before the
      # final GITHUB_ENV append, and later steps key on this reason.
      { echo "AUTOFIX_FAILURE_REASON=provider_unavailable"; echo "AUTOFIX_PROVIDER_STATUS=${provider_outage_status:-unknown}"; } >> "$GITHUB_ENV"
      provider_outage_reason_args=(--failure-reason provider_unavailable)
    fi
  fi
fi
evidence_out=""
if [ -n "${RUNTIME_DIR:-}" ] && [ -d "${RUNTIME_DIR}" ]; then
  evidence_out="${RUNTIME_DIR}/failure_evidence_tail.txt"
fi
fingerprint_out="$(PYTHONDONTWRITEBYTECODE=1 python3 "${AUTOFIX_FAILURE_HEAL_PY}" autofix-failure-fingerprint \
  --summary-line-file "${RUNTIME_DIR:-}/review_autofix_run_summary_line.txt" \
  --evidence-file "${RUNTIME_DIR:-}/reviewers_failure_evidence.txt" \
  --evidence-file "${RUNTIME_DIR:-}/sandbox_prepare_failure_evidence.txt" \
  --evidence-file "${RUNTIME_DIR:-}/editor_stage_stderr.txt" \
  --evidence-file "${RUNTIME_DIR:-}/collect_metadata_stderr.txt" \
  --evidence-file "${RUNTIME_DIR:-}/resolver_stage_stderr.txt" \
  --evidence-file "${RUNTIME_DIR:-}/review_autofix_run_summary_line.txt" \
  --evidence-out "${evidence_out}" \
  --head-sha "${AUTOFIX_FAILURE_HEAD_SHA:-}" \
  --support-sha "${REVIEW_SUPPORT_SHA:-}" \
  "${provider_outage_reason_args[@]}" \
  --run-id "${GITHUB_RUN_ID:-}" 2>/dev/null || true)"
failure_fp="$(printf '%s\n' "${fingerprint_out}" | sed -n 's/^fp=//p' | head -n 1)"
failure_degraded="$(printf '%s\n' "${fingerprint_out}" | sed -n 's/^degraded=//p' | head -n 1)"
failure_reason="$(printf '%s\n' "${fingerprint_out}" | sed -n 's/^reason=//p' | head -n 1)"
failure_marker="$(printf '%s\n' "${fingerprint_out}" | sed -n 's/^marker=//p' | head -n 1)"
if ! [[ "${failure_fp}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "AUTOFIX_FAILURE_MARKER=" >> "$GITHUB_ENV"
  echo "AUTOFIX_FINGERPRINT pr=${PR_NUMBER:-} head=${AUTOFIX_FAILURE_HEAD_SHA:-unknown} degraded=1 reason=helper_failed"
  exit 0
fi
{
  echo "AUTOFIX_FAILURE_FP=${failure_fp}"
  echo "AUTOFIX_FAILURE_REASON=${failure_reason}"
  echo "AUTOFIX_FAILURE_MARKER=${failure_marker}"
} >> "$GITHUB_ENV"
echo "AUTOFIX_FINGERPRINT pr=${PR_NUMBER:-} head=${AUTOFIX_FAILURE_HEAD_SHA:-unknown} reason=${failure_reason} fp=${failure_fp} degraded=${failure_degraded:-1}"
