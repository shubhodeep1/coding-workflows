#!/usr/bin/env bash
# Body of the "Claude-fixer merge check" step in
# .github/workflows/review_autofix.yml. The step sources this file in its own
# shell, so the step's if:, env: and continue-on-error: stay in the workflow;
# edit those there.
#
# Claude-fixer checks pending (CLAUDE_FIXER_CHECKS_PENDING_ENABLED, default
# true): when a clean review round's check runs were still running, the
# hand-off step posted a "clean, waiting for checks" comment carrying
#   <!-- ai:claude-fixer-checks-pending:v1 head=<sha> round=<n> run=<run_id> -->
# instead of handing an empty round to the Claude session. The review sweep
# re-dispatches every open PR about every 30 minutes; for such a head the gate
# sets CLAUDE_FIXER_MERGE_CHECK=true, the reviewers are skipped, and this step
# decides:
#
#   * the evidence artifact of the marker's run is verified through the API
#     (scripts/review_claude_fixer_evidence.py verify, outcome checks-pending,
#     same PR and head); the comment marker only names the run;
#   * the check runs on the head are re-collected without waiting
#     (CHECK_RUNS_WAIT_TIMEOUT_SECS=0, this run's own jobs excluded):
#       - fresh, ready and green -> CLAUDE_FIXER_ZERO_FINDINGS=true, so the
#         workflow's auto-merge step enables auto-merge bound to this head
#         (scripts/review_enable_auto_merge.sh, --match-head-commit);
#       - a failed check -> one `kind=findings` hand-off naming the failing
#         checks (the Claude session fixes it as a `ci` round);
#       - still running (or a transient check-run API error) -> log
#         `action=still_waiting` and exit; the next sweep retries;
#   * unverified evidence, or a check-run snapshot that can never become
#     ready (collector disabled or broken), falls back to the pre-checks-
#     pending behaviour: a `kind=findings` hand-off with no reviewer findings.
#
# Inputs (environment): PR_NUMBER, GH_TOKEN, GITHUB_REPOSITORY, HEAD_SHA,
# HEAD_REF, DEFAULT_BRANCH, CLAUDE_FIXER_MERGE_CHECK_RUN_ID,
# CLAUDE_FIXER_ROUND_INDEX, PR_PAYLOAD_FILE, PR_CHECK_RUNS_CONTEXT_FILE,
# SUPPORT_SCRIPTS_DIR, GITHUB_RUN_ID, GITHUB_SERVER_URL, RUNTIME_DIR,
# CLAUDE_FIXER_EVIDENCE_DIR (optional).
# API calls: the evidence verification (3 REST reads, plus one paginated PR
# files read when the evidence run came from the PR head branch), one
# check-runs read, and on a hand-off one comment POST.
set -euo pipefail

# shellcheck source=/dev/null
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

if ! [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]] || ! [[ "${HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::Claude-fixer merge check needs a numeric PR_NUMBER and a 40-hex HEAD_SHA (PR_NUMBER=${PR_NUMBER:-} HEAD_SHA=${HEAD_SHA:-})."
  exit 1
fi
merge_check_run_id="${CLAUDE_FIXER_MERGE_CHECK_RUN_ID:-}"
merge_check_round_index="${CLAUDE_FIXER_ROUND_INDEX:-0}"
[[ "${merge_check_round_index}" =~ ^[0-9]+$ ]] || merge_check_round_index=0
merge_check_round="$((merge_check_round_index + 1))"
merge_check_run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID:-0}"
merge_check_evidence_dir="${CLAUDE_FIXER_EVIDENCE_DIR:-${RUNTIME_DIR:-${TMPDIR:-/tmp}}/claude_fixer_evidence}"
merge_check_body_file="$(mktemp "${RUNTIME_DIR:-${TMPDIR:-/tmp}}/claude_fixer_merge_check.XXXXXX")"
merge_check_failed_checks=""

merge_check_write_evidence()
{
  PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR}/review_claude_fixer_evidence.py" write \
    --out-dir "${merge_check_evidence_dir}" \
    --pr "${PR_NUMBER}" \
    --head "${HEAD_SHA}" \
    --round "${merge_check_round}" \
    --outcome "$1" \
    --failed-checks "${merge_check_failed_checks}" >/dev/null \
    || echo "::warning::Could not write Claude-fixer evidence for outcome $1."
}

# Hand the round to the Claude session with the same header and marker the
# hand-off step uses, so the gate's awaiting-session rule and
# .claude/scripts/check_in_status.py treat it like any findings hand-off.
merge_check_hand_off()
{
  local handoff_reason="$1"
  local payload_file
  {
    echo "## Review round ${merge_check_round}: findings handed to the Claude session"
    echo
    echo "Reviewed head: \`${HEAD_SHA}\` ([merge check run](${merge_check_run_url})). The reviewer panel found nothing on this head; this is the checks-pending merge check."
    echo "${handoff_reason}"
    echo
    echo "Claude-fixer mode: the GPT editor did not run. The Claude session that owns this PR fixes what is named above in **one** commit whose subject starts with \`[claude-autofix]\` and pushes it; the push starts the next review round."
    echo
    echo "<!-- ai:claude-fixer-handoff:v1 kind=findings head=${HEAD_SHA} round=${merge_check_round} -->"
  } > "${merge_check_body_file}"
  merge_check_write_evidence findings
  payload_file="$(mktemp "${RUNTIME_DIR:-${TMPDIR:-/tmp}}/claude_fixer_merge_check_payload.XXXXXX")"
  jq -n --rawfile body "${merge_check_body_file}" '{body: $body}' > "${payload_file}"
  gh_retry gh api -X POST "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" --input "${payload_file}" >/dev/null
  rm -f "${payload_file}"
}

merge_check_verdict='{"verified": false, "reason": "helper_missing", "evidence": null}'
if [ -f "${SUPPORT_SCRIPTS_DIR}/review_claude_fixer_evidence.py" ]; then
  merge_check_verdict="$(PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR}/review_claude_fixer_evidence.py" verify \
    --repo "${GITHUB_REPOSITORY}" \
    --pr "${PR_NUMBER}" \
    --head "${HEAD_SHA}" \
    --run-id "${merge_check_run_id:-0}" \
    --expect-outcome checks-pending \
    --pr-head-ref "${HEAD_REF:-}" \
    --default-branch "${DEFAULT_BRANCH:-}" 2>/dev/null || true)"
fi
merge_check_verified="$(printf '%s' "${merge_check_verdict}" | jq -r 'if .verified == true then "true" else "false" end' 2>/dev/null || echo false)"
merge_check_reason="$(printf '%s' "${merge_check_verdict}" | jq -r '(.reason // "unparseable") | tostring | gsub("[^A-Za-z0-9_.:-]"; "_")' 2>/dev/null || echo unparseable)"
if [ "${merge_check_verified}" != "true" ]; then
  echo "CLAUDE_FIXER_CHECKS_PENDING pr=${PR_NUMBER} head=${HEAD_SHA} run=${merge_check_run_id:-none} action=evidence_unverified reason=${merge_check_reason}"
  merge_check_hand_off "The clean review's evidence (run \`${merge_check_run_id:-none}\`) could not be verified (\`${merge_check_reason}\`), so this head cannot auto-merge from it. No reviewer finding is open: push a new commit, or add the \`force-review\` label to re-run the reviewers on this head."
  echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${merge_check_round} kind=findings findings=0 ledger=none failed_checks=none reason=evidence_unverified"
  exit 0
fi
merge_check_evidence_round="$(printf '%s' "${merge_check_verdict}" | jq -r '.evidence.round // empty' 2>/dev/null || true)"
if [[ "${merge_check_evidence_round}" =~ ^[1-9][0-9]*$ ]]; then
  merge_check_round="${merge_check_evidence_round}"
fi

merge_check_status="unavailable"
if [ -f "${SUPPORT_SCRIPTS_DIR}/collect_pr_check_runs_context.py" ]; then
  CHECK_RUNS_WAIT_TIMEOUT_SECS=0 SELF_RUN_ID="${GITHUB_RUN_ID:-}" CHECK_RUNS_EXCLUDE_SELF_FROM_CONTEXT=true PYTHONDONTWRITEBYTECODE=1 \
    python3 "${SUPPORT_SCRIPTS_DIR}/collect_pr_check_runs_context.py"
  if [ -s "${PR_CHECK_RUNS_CONTEXT_FILE:-}" ] \
    && [ "$(sed -n '1p' "${PR_CHECK_RUNS_CONTEXT_FILE}")" = "PR_CHECK_RUNS_CONTEXT" ] \
    && [ "$(sed -n '2p' "${PR_CHECK_RUNS_CONTEXT_FILE}")" = "head_sha: ${HEAD_SHA}" ]; then
    merge_check_status="$(sed -n 's/^collection_status: //p' "${PR_CHECK_RUNS_CONTEXT_FILE}" | head -n 1)"
    merge_check_failed_checks="$(sed -n 's/^failed\[[0-9]*\]\.name: //p' "${PR_CHECK_RUNS_CONTEXT_FILE}" | paste -sd, - || true)"
  fi
fi

if [ -n "${merge_check_failed_checks}" ]; then
  echo "CLAUDE_FIXER_CHECKS_PENDING pr=${PR_NUMBER} head=${HEAD_SHA} run=${merge_check_run_id} action=handoff_ci failed_checks=${merge_check_failed_checks}"
  merge_check_hand_off "Failing check runs on this head: \`${merge_check_failed_checks//,/\`, \`}\`."
  echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${merge_check_round} kind=findings findings=0 ledger=none failed_checks=${merge_check_failed_checks}"
  exit 0
fi

if [ "${merge_check_status}" = "ready" ] \
  && grep -Eq '^total_check_runs: [1-9][0-9]*$' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
  && grep -Fxq 'failed_count: 0' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
  && grep -Fxq 'incomplete_count: 0' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
  && ! grep -Eq '^(failed|incomplete)\[[0-9]+\]\.' "${PR_CHECK_RUNS_CONTEXT_FILE}"; then
  merge_check_write_evidence clean
  echo "CLAUDE_FIXER_CHECKS_PENDING pr=${PR_NUMBER} head=${HEAD_SHA} run=${merge_check_run_id} action=auto_merge"
  echo "CLAUDE_FIXER_ZERO_FINDINGS=true" >> "$GITHUB_ENV"
  exit 0
fi

case "${merge_check_status}" in
  ready|timeout|api_error)
    echo "CLAUDE_FIXER_CHECKS_PENDING pr=${PR_NUMBER} head=${HEAD_SHA} run=${merge_check_run_id} action=still_waiting collection_status=${merge_check_status}"
    ;;
  *)
    echo "CLAUDE_FIXER_CHECKS_PENDING pr=${PR_NUMBER} head=${HEAD_SHA} run=${merge_check_run_id} action=snapshot_unavailable collection_status=${merge_check_status:-unknown}"
    merge_check_hand_off "The check-run snapshot for this head is unavailable (collection status \`${merge_check_status:-unknown}\`), so the clean review cannot be merged automatically. No reviewer finding is open: push a new commit, or add the \`force-review\` label to re-run the reviewers on this head."
    echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${merge_check_round} kind=findings findings=0 ledger=none failed_checks=none reason=snapshot_unavailable"
    ;;
esac
