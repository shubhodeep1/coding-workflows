#!/usr/bin/env bash
#
# workflow_failure_heal_implement_report.sh
#
# Runs as a late failure-path step of the implement workflow
# (.github/workflows/implement.yml, "Report implementation failure to workflow
# failure heal"), in consumer repositories and in coding-workflows alike. When
# the implementation run for an issue has failed, it:
#
#   1. Names the failure (`failure_reason`): `cancelled_or_timed_out` when the
#      job was cancelled, otherwise `workflow_failure`. The distinctive error
#      line lives in the job log, which the heal intake fetches from the API
#      for every run this report links.
#   2. Counts how many implementation runs in a row failed on this issue from
#      the issue comments the run fetched at its start ("AI implementation
#      workflow failed for ..." / "... was cancelled/timed out for ..."). Plan,
#      approval and stall-recovery comments between two failures are neutral;
#      a BLOCKED verdict, a pathspec halt or a no-op comment ends the streak
#      because those paths route the issue elsewhere. The reporter adds one
#      for this run and makes zero API reads (CLAUDE.md §15). A single failure
#      stays with the stall poller's retry; only a streak of at least
#      WORKFLOW_HEAL_IMPLEMENT_FAILURE_STREAK (default 2) is reported.
#   3. Builds the `implement_failure` payload (scripts/workflow_failure_heal.py
#      build-implement-payload) with this run and the runs linked from the
#      issue's failure comments as run references and the tail of the run's own
#      log files as evidence, and sends one `repository_dispatch` (event type
#      `workflow-failure-heal`) to coding-workflows, whose intake diagnoses the
#      failure and opens the heal issue.
#
# Why: implement runs 35614385686, 35628923735, 35642366131 and 35656715219
# (issues #4227 / #4242) failed four times on one deterministic defect while
# the stall poller retried twice, the stall judge re-issued, and the
# replacement failed the same way. No escalation label was ever applied, so
# the label-triggered heal path never saw it.
#
# The reporter never fails the implement job: every exit is 0 and every skip
# is a stable log line prefixed WORKFLOW_HEAL_IMPLEMENT_REPORT.
#
# Required env (set by the workflow):
#   GITHUB_REPOSITORY              owner/repo of the issue
#   GH_TOKEN                       GitHub token (GH_PAT) able to dispatch to coding-workflows
#   ISSUE_NUMBER                   issue number
#   GITHUB_RUN_ID                  this run
#
# Optional env (have defaults):
#   WORKFLOW_HEAL_ENABLED                    "false" to disable; default on
#   WORKFLOW_HEAL_IMPLEMENT_FAILURE_STREAK   consecutive failed runs before reporting (default 2)
#   WORKFLOW_HEAL_UPSTREAM_REPO              dispatch target (default shubhodeep1/coding-workflows)
#   WORKFLOW_HEAL_PY                         path of workflow_failure_heal.py
#   RUNTIME_DIR                              the run's scratch dir (evidence + log tails)
#   ISSUE_META_FILE                          issue JSON fetched at run start
#   ISSUE_COMMENTS_FILE                      issue comments JSON fetched at run start
#   REPORT_WORKFLOW_NAME                     caller workflow display name (github.workflow)
#   REPORT_RUN_URL                           this run's URL
#   REPORT_WRAPPER_SHA                       coding-workflows ref the run used (SCRIPT_REF)
#   REPORT_JOB_STATUS                        job.status (failure | cancelled)
#   REPORT_HEAD_BRANCH                       branch the run implemented on (TARGET_BRANCH)

set -uo pipefail

log()
{
	echo "WORKFLOW_HEAL_IMPLEMENT_REPORT $*"
}

ENABLED="${WORKFLOW_HEAL_ENABLED:-true}"
UPSTREAM_REPO="${WORKFLOW_HEAL_UPSTREAM_REPO:-shubhodeep1/coding-workflows}"
REPO="${GITHUB_REPOSITORY:-}"
ISSUE="${ISSUE_NUMBER:-}"
RUN_ID="${GITHUB_RUN_ID:-}"
RUN_URL="${REPORT_RUN_URL:-${GITHUB_SERVER_URL:-https://github.com}/${REPO}/actions/runs/${RUN_ID}}"
WORKFLOW_NAME="${REPORT_WORKFLOW_NAME:-${GITHUB_WORKFLOW:-implement}}"
WRAPPER_SHA="$(printf '%s' "${REPORT_WRAPPER_SHA:-}" | tr '[:upper:]' '[:lower:]')"
JOB_STATUS="${REPORT_JOB_STATUS:-failure}"
HEAD_BRANCH="${REPORT_HEAD_BRANCH:-}"
STREAK_THRESHOLD="${WORKFLOW_HEAL_IMPLEMENT_FAILURE_STREAK:-2}"
case "${STREAK_THRESHOLD}" in
	''|*[!0-9]*|0) STREAK_THRESHOLD=2 ;;
esac
export PYTHONDONTWRITEBYTECODE=1

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEAL_PY="${WORKFLOW_HEAL_PY:-${script_dir}/workflow_failure_heal.py}"
if [ ! -f "${HEAL_PY}" ] && [ -f "scripts/workflow_failure_heal.py" ]; then
	HEAL_PY="scripts/workflow_failure_heal.py"
fi

# Optional helpers (staged by the implement workflow; absent in tests).
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
if [ -z "${REPO}" ] || ! [[ "${ISSUE}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${RUN_ID}" =~ ^[0-9]+$ ]]; then
	log "skip reason=missing_context repo=${REPO:-none} issue=${ISSUE:-none} run=${RUN_ID:-none}"
	exit 0
fi
if [ ! -f "${HEAL_PY}" ]; then
	log "skip reason=heal_helper_missing path=${HEAL_PY}"
	exit 0
fi
if [ -n "${RUNTIME_DIR:-}" ] && [ -f "${RUNTIME_DIR}/codex_blocked.flag" ]; then
	# A deliberate BLOCKED verdict is the model's answer, not a workflow
	# failure; "Comment on issue failure" parks the issue in ai:blocked.
	log "skip reason=blocked_verdict issue=${ISSUE}"
	exit 0
fi

WORK_DIR="${RUNTIME_DIR:-${TMPDIR:-/tmp}}"
[ -d "${WORK_DIR}" ] || WORK_DIR="${TMPDIR:-/tmp}"
REPORT_DIR="${WORK_DIR}/workflow_heal_implement_report"
mkdir -p "${REPORT_DIR}"

# --- Failure reason ----------------------------------------------------------

if [ "${JOB_STATUS}" = "cancelled" ]; then
	FAILURE_REASON="cancelled_or_timed_out"
else
	FAILURE_REASON="workflow_failure"
fi

# --- Streak ------------------------------------------------------------------

COMMENTS_FILE="${ISSUE_COMMENTS_FILE:-}"
PRIOR_FAILURES=0
if [ -n "${COMMENTS_FILE}" ] && [ -s "${COMMENTS_FILE}" ]; then
	PRIOR_FAILURES="$(python3 "${HEAL_PY}" implement-failure-streak --comments-json "${COMMENTS_FILE}" 2>/dev/null || echo 0)"
	[[ "${PRIOR_FAILURES}" =~ ^[0-9]+$ ]] || PRIOR_FAILURES=0
else
	log "warn issue_comments_unavailable issue=${ISSUE}; counting this run only"
fi
STREAK=$((PRIOR_FAILURES + 1))
if [ "${STREAK}" -lt "${STREAK_THRESHOLD}" ]; then
	log "skip reason=below_streak issue=${ISSUE} reason=${FAILURE_REASON} streak=${STREAK} threshold=${STREAK_THRESHOLD}"
	exit 0
fi

# --- Evidence ----------------------------------------------------------------

EVIDENCE_FILE="${REPORT_DIR}/evidence.txt"
{
	echo "failure_reason=${FAILURE_REASON}"
	echo "job_status=${JOB_STATUS}"
	echo "consecutive_failed_runs=${STREAK}"
	echo "head_branch=${HEAD_BRANCH:-unknown}"
	# Tail of the run's own log files (bounded): the codex attempt logs,
	# the diagnose log and the syntax-repair logs the workflow keeps in
	# RUNTIME_DIR until its cleanup step.
	if [ -n "${RUNTIME_DIR:-}" ] && [ -d "${RUNTIME_DIR}" ]; then
		while IFS= read -r log_file; do
			[ -n "${log_file}" ] || continue
			echo "--- $(basename "${log_file}") (tail) ---"
			tail -n 40 "${log_file}" 2>/dev/null | cut -c1-400
		done < <(find "${RUNTIME_DIR}" -maxdepth 1 -type f \( -name 'codex_log_attempt_*.txt' -o -name 'post_codex_repair_log_attempt_*.txt' -o -name '*log*.txt' -o -name '*.log' \) -size +0 2>/dev/null | sort | head -3)
	fi
} > "${EVIDENCE_FILE}" 2>/dev/null

# --- Issue payload -----------------------------------------------------------

ISSUE_JSON_FILE="${ISSUE_META_FILE:-}"
if [ -z "${ISSUE_JSON_FILE}" ] || ! jq -e 'type == "object" and (.number != null)' "${ISSUE_JSON_FILE}" >/dev/null 2>&1; then
	ISSUE_JSON_FILE="${REPORT_DIR}/issue.json"
	ISSUE_FETCH_ERROR_FILE="${REPORT_DIR}/issue_fetch_error.txt"
	if ! gh_retry gh api --method GET "repos/${REPO}/issues/${ISSUE}" > "${ISSUE_JSON_FILE}" 2> "${ISSUE_FETCH_ERROR_FILE}"; then
		log "skip reason=issue_fetch_failed issue=${ISSUE} detail=$(head -c 300 "${ISSUE_FETCH_ERROR_FILE}" | tr '\n' ' ')"
		exit 0
	fi
fi

# --- Build, validate, dispatch ----------------------------------------------------

PAYLOAD_FILE="${REPORT_DIR}/payload.json"
BUILD_ARGS=(--repo "${REPO}" --issue-json "${ISSUE_JSON_FILE}" --workflow-name "${WORKFLOW_NAME}" --failure-reason "${FAILURE_REASON}" \
	--failure-evidence-file "${EVIDENCE_FILE}" --failure-streak "${STREAK}" --run-id "${RUN_ID}" --run-url "${RUN_URL}" \
	--wrapper-sha "${WRAPPER_SHA}" --reporter-run-url "${RUN_URL}" --head-branch "${HEAD_BRANCH}")
if [ -n "${COMMENTS_FILE}" ] && [ -s "${COMMENTS_FILE}" ]; then
	BUILD_ARGS+=(--comments-json "${COMMENTS_FILE}")
fi
if ! python3 "${HEAL_PY}" build-implement-payload "${BUILD_ARGS[@]}" > "${PAYLOAD_FILE}" 2> "${REPORT_DIR}/build_error.txt"; then
	log "skip reason=payload_build_failed issue=${ISSUE} detail=$(head -c 200 "${REPORT_DIR}/build_error.txt" | tr '\n' ' ')"
	exit 0
fi

SKIP_REASON="$(python3 "${HEAL_PY}" skip-reason --payload-json "${PAYLOAD_FILE}" --self-repo "${REPO}" 2>/dev/null || echo "")"
if [ -n "${SKIP_REASON}" ]; then
	log "skip reason=${SKIP_REASON} issue=${ISSUE} failure=${FAILURE_REASON}"
	exit 0
fi

DISPATCH_FILE="${REPORT_DIR}/dispatch.json"
jq -n --arg event_type "workflow-failure-heal" --slurpfile payload "${PAYLOAD_FILE}" \
	'{event_type: $event_type, client_payload: $payload[0]}' > "${DISPATCH_FILE}"
if ! gh_retry gh api -X POST "repos/${UPSTREAM_REPO}/dispatches" --input "${DISPATCH_FILE}" >/dev/null 2>&1; then
	log "skip reason=dispatch_denied issue=${ISSUE} failure=${FAILURE_REASON} streak=${STREAK} upstream=${UPSTREAM_REPO}"
	exit 0
fi
log "dispatched issue=${ISSUE} failure=${FAILURE_REASON} streak=${STREAK} workflow=${WORKFLOW_NAME} wrapper_sha=${WRAPPER_SHA:-none} upstream=${UPSTREAM_REPO}"
exit 0
