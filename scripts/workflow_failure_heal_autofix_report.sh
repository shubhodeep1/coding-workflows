#!/usr/bin/env bash
#
# workflow_failure_heal_autofix_report.sh
#
# Runs as the last failure-path step of the review/autofix workflow
# (.github/workflows/review_autofix.yml, "Report autofix failure to workflow
# failure heal"), in consumer repositories and in coding-workflows alike. When
# the review/autofix run on a pull request has failed, it:
#
#   1. Names the failure (`failure_reason`) from the flags the run already set
#      (editor empty no-op, editor changes lost, editor refusal) or from the
#      structured run summary's `finalize_reason`, falling back to
#      `workflow_failure`.
#   2. Counts how many review runs in a row failed on this pull request from
#      the PR comments the run fetched at its start. Editor summaries paired
#      with a later failure comment from the same run do not end the streak.
#      The reporter adds one for this run and makes zero API reads (CLAUDE.md
#      §15). A single failure stays with the stall poller's retry; only a streak of at least
#      WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK (default 2) is reported.
#   3. Builds the `autofix_failure` payload (scripts/workflow_failure_heal.py
#      build-autofix-payload) with the run summary line and the tail of the
#      run's own log files as evidence, and sends one `repository_dispatch`
#      (event type `workflow-failure-heal`) to coding-workflows, whose intake
#      diagnoses the failure and opens the heal issue.
#
# The reporter never fails the review job: every exit is 0 and every skip is
# a stable log line prefixed WORKFLOW_HEAL_AUTOFIX_REPORT.
#
# Required env (set by the workflow):
#   GITHUB_REPOSITORY              owner/repo of the pull request
#   GH_TOKEN                       GitHub token (GH_PAT) able to dispatch to coding-workflows
#   PR_NUMBER                      pull request number
#   GITHUB_RUN_ID                  this run
#
# Optional env (have defaults):
#   WORKFLOW_HEAL_ENABLED                  "false" to disable; default on
#   WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK   consecutive failed runs before reporting (default 2)
#   WORKFLOW_HEAL_UPSTREAM_REPO            dispatch target (default shubhodeep1/coding-workflows)
#   WORKFLOW_HEAL_PY                       path of workflow_failure_heal.py
#   RUNTIME_DIR                            the run's scratch dir (evidence + log tails)
#   PR_PAYLOAD_FILE                        PR JSON fetched at run start
#   PR_ISSUE_COMMENTS_FILE                 PR issue comments JSON fetched at run start
#   REPORT_WORKFLOW_NAME                   caller workflow display name (github.workflow)
#   REPORT_RUN_URL                         this run's URL
#   REPORT_WRAPPER_SHA                     coding-workflows ref the run used (SCRIPT_REF)
#   REPORT_SUMMARY_LINE_FILE               file holding the REVIEW_AUTOFIX_RUN_SUMMARY_V1 line
#   AUTOFIX_EDITOR_EMPTY_NOOP, EDITOR_CHANGES_LOST, EDITOR_NOOP_REFUSAL,
#   EDITOR_NOOP_SUSPICIOUS, RESOLVER_ESCALATED   run flags (true/false)

set -uo pipefail

log()
{
	echo "WORKFLOW_HEAL_AUTOFIX_REPORT $*"
}

ENABLED="${WORKFLOW_HEAL_ENABLED:-true}"
UPSTREAM_REPO="${WORKFLOW_HEAL_UPSTREAM_REPO:-shubhodeep1/coding-workflows}"
REPO="${GITHUB_REPOSITORY:-}"
PR="${PR_NUMBER:-}"
RUN_ID="${GITHUB_RUN_ID:-}"
RUN_URL="${REPORT_RUN_URL:-${GITHUB_SERVER_URL:-https://github.com}/${REPO}/actions/runs/${RUN_ID}}"
WORKFLOW_NAME="${REPORT_WORKFLOW_NAME:-${GITHUB_WORKFLOW:-review_autofix}}"
WRAPPER_SHA="$(printf '%s' "${REPORT_WRAPPER_SHA:-}" | tr '[:upper:]' '[:lower:]')"
STREAK_THRESHOLD="${WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK:-2}"
case "${STREAK_THRESHOLD}" in
	''|*[!0-9]*|0) STREAK_THRESHOLD=2 ;;
esac
export PYTHONDONTWRITEBYTECODE=1

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEAL_PY="${WORKFLOW_HEAL_PY:-${script_dir}/workflow_failure_heal.py}"
if [ ! -f "${HEAL_PY}" ] && [ -f "scripts/workflow_failure_heal.py" ]; then
	HEAL_PY="scripts/workflow_failure_heal.py"
fi

# Optional helpers (staged by the review workflow; absent in tests).
if [ -f "${script_dir}/gh_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source "${script_dir}/gh_helpers.sh" 2>/dev/null || true
fi
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

# --- Gates -----------------------------------------------------------------

if [ "$(printf '%s' "${ENABLED}" | tr '[:upper:]' '[:lower:]')" = "false" ]; then
	log "skip reason=disabled (WORKFLOW_HEAL_ENABLED=false) repo=${REPO}"
	exit 0
fi
if [ -z "${REPO}" ] || ! [[ "${PR}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${RUN_ID}" =~ ^[0-9]+$ ]]; then
	log "skip reason=missing_context repo=${REPO:-none} pr=${PR:-none} run=${RUN_ID:-none}"
	exit 0
fi
if [ ! -f "${HEAL_PY}" ]; then
	log "skip reason=heal_helper_missing path=${HEAL_PY}"
	exit 0
fi
if [ "${RESOLVER_ESCALATED:-false}" = "true" ]; then
	# ai:resolver-escalated is applied to the PR; the label-triggered heal
	# wrapper reports that escalation with the resolver's own evidence.
	log "skip reason=resolver_escalated pr=${PR}"
	exit 0
fi

WORK_DIR="${RUNTIME_DIR:-${TMPDIR:-/tmp}}"
[ -d "${WORK_DIR}" ] || WORK_DIR="${TMPDIR:-/tmp}"
REPORT_DIR="${WORK_DIR}/workflow_heal_autofix_report"
mkdir -p "${REPORT_DIR}"

# --- Failure reason ----------------------------------------------------------

SUMMARY_LINE_FILE="${REPORT_SUMMARY_LINE_FILE:-${WORK_DIR}/review_autofix_run_summary_line.txt}"
FINALIZE_REASON=""
if [ -s "${SUMMARY_LINE_FILE}" ]; then
	FINALIZE_REASON="$(sed -n 's/^REVIEW_AUTOFIX_RUN_SUMMARY_V1 //p' "${SUMMARY_LINE_FILE}" | head -1 | jq -r '.finalize_reason // ""' 2>/dev/null || echo "")"
fi
if [ "${AUTOFIX_EDITOR_EMPTY_NOOP:-false}" = "true" ]; then
	FAILURE_REASON="editor_empty_noop"
elif [ "${EDITOR_CHANGES_LOST:-false}" = "true" ]; then
	FAILURE_REASON="editor_changes_lost"
elif [ "${EDITOR_NOOP_REFUSAL:-false}" = "true" ]; then
	FAILURE_REASON="editor_refusal"
elif [ -n "${FINALIZE_REASON}" ] && [[ "${FINALIZE_REASON}" =~ ^[a-z][a-z0-9_:-]{0,79}$ ]]; then
	FAILURE_REASON="${FINALIZE_REASON}"
else
	FAILURE_REASON="workflow_failure"
fi

# --- Streak ------------------------------------------------------------------

COMMENTS_FILE="${PR_ISSUE_COMMENTS_FILE:-}"
PRIOR_FAILURES=0
if [ -n "${COMMENTS_FILE}" ] && [ -s "${COMMENTS_FILE}" ]; then
	PRIOR_FAILURES="$(python3 "${HEAL_PY}" autofix-failure-streak --comments-json "${COMMENTS_FILE}" 2>/dev/null || echo 0)"
	[[ "${PRIOR_FAILURES}" =~ ^[0-9]+$ ]] || PRIOR_FAILURES=0
else
	log "warn pr_comments_unavailable pr=${PR}; counting this run only"
fi
STREAK=$((PRIOR_FAILURES + 1))
if [ "${STREAK}" -lt "${STREAK_THRESHOLD}" ]; then
	log "skip reason=below_streak pr=${PR} reason=${FAILURE_REASON} streak=${STREAK} threshold=${STREAK_THRESHOLD}"
	exit 0
fi

# --- Evidence ----------------------------------------------------------------

EVIDENCE_FILE="${REPORT_DIR}/evidence.txt"
{
	echo "failure_reason=${FAILURE_REASON}"
	echo "finalize_reason=${FINALIZE_REASON:-unknown}"
	echo "consecutive_failed_runs=${STREAK}"
	echo "flags: AUTOFIX_EDITOR_EMPTY_NOOP=${AUTOFIX_EDITOR_EMPTY_NOOP:-} EDITOR_NOOP_SUSPICIOUS=${EDITOR_NOOP_SUSPICIOUS:-} EDITOR_NOOP_REFUSAL=${EDITOR_NOOP_REFUSAL:-} EDITOR_CHANGES_LOST=${EDITOR_CHANGES_LOST:-} HAS_PR_DIFF=${HAS_PR_DIFF:-} PR_DIFF_SOURCE=${PR_DIFF_SOURCE:-}"
	if [ -s "${SUMMARY_LINE_FILE}" ]; then
		head -c 6000 "${SUMMARY_LINE_FILE}"
		echo
	fi
	# Tail of the run's own log files (bounded): the codex / editor logs the
	# workflow keeps in RUNTIME_DIR until its cleanup step.
	if [ -n "${RUNTIME_DIR:-}" ] && [ -d "${RUNTIME_DIR}" ]; then
		while IFS= read -r log_file; do
			[ -n "${log_file}" ] || continue
			echo "--- $(basename "${log_file}") (tail) ---"
			tail -n 40 "${log_file}" 2>/dev/null | cut -c1-400
		done < <(find "${RUNTIME_DIR}" -maxdepth 1 -type f \( -name '*log*.txt' -o -name '*.log' \) -size +0 2>/dev/null | sort | head -3)
	fi
} > "${EVIDENCE_FILE}" 2>/dev/null

# --- PR payload ----------------------------------------------------------------

PR_JSON_FILE="${PR_PAYLOAD_FILE:-}"
if [ -z "${PR_JSON_FILE}" ] || ! jq -e 'type == "object" and (.number != null)' "${PR_JSON_FILE}" >/dev/null 2>&1; then
	PR_JSON_FILE="${REPORT_DIR}/pr.json"
	PR_FETCH_ERROR_FILE="${REPORT_DIR}/pr_fetch_error.txt"
	if ! gh_retry gh api --method GET "repos/${REPO}/pulls/${PR}" > "${PR_JSON_FILE}" 2> "${PR_FETCH_ERROR_FILE}"; then
		log "skip reason=pr_fetch_failed pr=${PR} detail=$(head -c 300 "${PR_FETCH_ERROR_FILE}" | tr '\n' ' ')"
		exit 0
	fi
fi

# --- Build, validate, dispatch ----------------------------------------------------

PAYLOAD_FILE="${REPORT_DIR}/payload.json"
BUILD_ARGS=(--repo "${REPO}" --pr-json "${PR_JSON_FILE}" --workflow-name "${WORKFLOW_NAME}" --failure-reason "${FAILURE_REASON}" \
	--failure-evidence-file "${EVIDENCE_FILE}" --failure-streak "${STREAK}" --run-id "${RUN_ID}" --run-url "${RUN_URL}" \
	--wrapper-sha "${WRAPPER_SHA}" --reporter-run-url "${RUN_URL}")
if [ -n "${COMMENTS_FILE}" ] && [ -s "${COMMENTS_FILE}" ]; then
	BUILD_ARGS+=(--comments-json "${COMMENTS_FILE}")
fi
if ! python3 "${HEAL_PY}" build-autofix-payload "${BUILD_ARGS[@]}" > "${PAYLOAD_FILE}" 2> "${REPORT_DIR}/build_error.txt"; then
	log "skip reason=payload_build_failed pr=${PR} detail=$(head -c 200 "${REPORT_DIR}/build_error.txt" | tr '\n' ' ')"
	exit 0
fi

SKIP_REASON="$(python3 "${HEAL_PY}" skip-reason --payload-json "${PAYLOAD_FILE}" --self-repo "${REPO}" 2>/dev/null || echo "")"
if [ -n "${SKIP_REASON}" ]; then
	log "skip reason=${SKIP_REASON} pr=${PR} failure=${FAILURE_REASON}"
	exit 0
fi

DISPATCH_FILE="${REPORT_DIR}/dispatch.json"
jq -n --arg event_type "workflow-failure-heal" --slurpfile payload "${PAYLOAD_FILE}" \
	'{event_type: $event_type, client_payload: $payload[0]}' > "${DISPATCH_FILE}"
if ! gh_retry gh api -X POST "repos/${UPSTREAM_REPO}/dispatches" --input "${DISPATCH_FILE}" >/dev/null 2>&1; then
	log "skip reason=dispatch_denied pr=${PR} failure=${FAILURE_REASON} streak=${STREAK} upstream=${UPSTREAM_REPO}"
	exit 0
fi
log "dispatched pr=${PR} failure=${FAILURE_REASON} streak=${STREAK} workflow=${WORKFLOW_NAME} wrapper_sha=${WRAPPER_SHA:-none} upstream=${UPSTREAM_REPO}"
exit 0
