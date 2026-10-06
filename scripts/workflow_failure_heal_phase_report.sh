#!/usr/bin/env bash
#
# workflow_failure_heal_phase_report.sh
#
# Runs in the `heal-report` job of clarify.yml, plan.yml and implement.yml
# (consumer repositories and coding-workflows alike) after the phase job of the
# same run failed. The job runs separately so the phase job has finished and its
# log is complete when coding-workflows' heal intake reads it. It:
#
#   1. Reads the issue and its comments, and counts how many runs of this phase
#      failed in a row on the issue from the failure comments the phase posts
#      (scripts/workflow_failure_heal.py phase_failure_streak). A clarify /
#      plan run that finished, or another phase's failure, ends the streak.
#   2. At WORKFLOW_HEAL_PHASE_FAILURE_STREAK (default 1, so every failed run)
#      builds the `phase_failure` payload (build-phase-payload): the failed run
#      first in run_refs, then the earlier runs of the streak, and the issue's
#      heal markers so a heal issue's own failed run continues its lineage.
#   3. Sends one `repository_dispatch` (event type `workflow-failure-heal`) to
#      coding-workflows, whose intake reads the failed job's log, diagnoses
#      the failure, de-duplicates (one open heal issue per source issue) and
#      opens the heal issue.
#
# API calls (CLAUDE.md §15): one issue read, one paginated comment read, one
# dispatch. The phase job's own issue snapshot and comment reads live in
# another job and predate its failure comment, so neither can be reused; the
# event payload's issue copy is not used because a long body can push a step's
# environment past the kernel's per-variable limit.
#
# The reporter never fails the job: every exit is 0 and every outcome is a
# stable log line prefixed WORKFLOW_HEAL_PHASE_REPORT.
#
# Required env (set by the workflow):
#   GITHUB_REPOSITORY              owner/repo of the issue
#   GH_TOKEN                       GitHub token (GH_PAT) able to read the issue and
#                                  dispatch to coding-workflows
#   WORKFLOW_HEAL_PHASE            clarify | plan | implement
#   WORKFLOW_HEAL_ISSUE_NUMBER     the issue the phase ran on
#   GITHUB_RUN_ID                  the failed run (the phase job's run)
#
# Optional env (have defaults):
#   WORKFLOW_HEAL_ENABLED                "false" to disable; default on
#   WORKFLOW_HEAL_PHASE_FAILURE_STREAK   consecutive failed runs before reporting (default 1)
#   WORKFLOW_HEAL_UPSTREAM_REPO          dispatch target (default shubhodeep1/coding-workflows)
#   WORKFLOW_HEAL_PY                     path of workflow_failure_heal.py (default: beside this script)
#   REPORT_WORKFLOW_NAME                 caller workflow display name (github.workflow)
#   REPORT_WRAPPER_SHA                   coding-workflows commit the run's support scripts came from
#   RUNTIME_DIR                          scratch dir

set -uo pipefail

log()
{
	echo "WORKFLOW_HEAL_PHASE_REPORT $*"
}

ENABLED="${WORKFLOW_HEAL_ENABLED:-true}"
UPSTREAM_REPO="${WORKFLOW_HEAL_UPSTREAM_REPO:-shubhodeep1/coding-workflows}"
REPO="${GITHUB_REPOSITORY:-}"
PHASE="${WORKFLOW_HEAL_PHASE:-}"
ISSUE_NUMBER="${WORKFLOW_HEAL_ISSUE_NUMBER:-}"
RUN_ID="${GITHUB_RUN_ID:-}"
REPORTER_RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${REPO}/actions/runs/${RUN_ID}"
WORKFLOW_NAME="${REPORT_WORKFLOW_NAME:-${GITHUB_WORKFLOW:-${PHASE}}}"
WRAPPER_SHA="$(printf '%s' "${REPORT_WRAPPER_SHA:-}" | tr '[:upper:]' '[:lower:]')"
STREAK_THRESHOLD="${WORKFLOW_HEAL_PHASE_FAILURE_STREAK:-1}"
case "${STREAK_THRESHOLD}" in
	''|*[!0-9]*|0) STREAK_THRESHOLD=1 ;;
esac
export PYTHONDONTWRITEBYTECODE=1

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEAL_PY="${WORKFLOW_HEAL_PY:-${script_dir}/workflow_failure_heal.py}"

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
case "${PHASE}" in
	clarify|plan|implement) ;;
	*)
		log "skip reason=unknown_phase phase=${PHASE:-none}"
		exit 0
		;;
esac
if [ -z "${REPO}" ] || ! [[ "${ISSUE_NUMBER}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${RUN_ID}" =~ ^[0-9]+$ ]]; then
	log "skip reason=missing_context repo=${REPO:-none} issue=${ISSUE_NUMBER:-none} run=${RUN_ID:-none}"
	exit 0
fi
if [ ! -f "${HEAL_PY}" ]; then
	log "skip reason=heal_helper_missing path=${HEAL_PY}"
	exit 0
fi

WORK_DIR="${RUNTIME_DIR:-${TMPDIR:-/tmp}}"
mkdir -p "${WORK_DIR}" 2>/dev/null || WORK_DIR="${TMPDIR:-/tmp}"
REPORT_DIR="${WORK_DIR}/workflow_heal_phase_report"
mkdir -p "${REPORT_DIR}"

# --- Issue + comments ----------------------------------------------------------

ISSUE_JSON_FILE="${REPORT_DIR}/issue.json"
ISSUE_FETCH_ERROR_FILE="${REPORT_DIR}/issue_fetch_error.txt"
if ! gh_retry gh api --method GET "repos/${REPO}/issues/${ISSUE_NUMBER}" > "${ISSUE_JSON_FILE}" 2> "${ISSUE_FETCH_ERROR_FILE}" \
	|| ! jq -e 'type == "object" and (.number | type == "number")' "${ISSUE_JSON_FILE}" >/dev/null 2>&1; then
	log "skip reason=issue_fetch_failed issue=${ISSUE_NUMBER} detail=$(head -c 300 "${ISSUE_FETCH_ERROR_FILE}" 2>/dev/null | tr '\n' ' ')"
	exit 0
fi
ISSUE_STATE="$(jq -r '.state // ""' "${ISSUE_JSON_FILE}")"
if [ "${ISSUE_STATE}" != "open" ]; then
	log "skip reason=issue_not_open issue=${ISSUE_NUMBER} state=${ISSUE_STATE}"
	exit 0
fi

COMMENTS_JSON_FILE="${REPORT_DIR}/comments.json"
if ! gh_retry gh api --method GET --paginate "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" -F per_page=100 \
	--jq '.[] | {body: (.body // ""), created_at: (.created_at // "")}' 2>/dev/null \
	| jq -s '.' > "${COMMENTS_JSON_FILE}" 2>/dev/null \
	|| ! jq -e 'type == "array"' "${COMMENTS_JSON_FILE}" >/dev/null 2>&1; then
	log "warn comments_fetch_failed issue=${ISSUE_NUMBER}; counting this run only"
	printf '[]' > "${COMMENTS_JSON_FILE}"
fi

# --- Build, gate, validate, dispatch --------------------------------------------

PAYLOAD_FILE="${REPORT_DIR}/payload.json"
BUILD_ERROR_FILE="${REPORT_DIR}/build_error.txt"
if ! python3 "${HEAL_PY}" build-phase-payload --repo "${REPO}" --phase "${PHASE}" --issue-json "${ISSUE_JSON_FILE}" \
	--comments-json "${COMMENTS_JSON_FILE}" --workflow-name "${WORKFLOW_NAME}" --run-id "${RUN_ID}" \
	--wrapper-sha "${WRAPPER_SHA}" --reporter-run-url "${REPORTER_RUN_URL}" > "${PAYLOAD_FILE}" 2> "${BUILD_ERROR_FILE}"; then
	log "skip reason=payload_build_failed issue=${ISSUE_NUMBER} phase=${PHASE} detail=$(head -c 200 "${BUILD_ERROR_FILE}" | tr '\n' ' ')"
	exit 0
fi
STREAK="$(jq -r '.failure_streak // 1' "${PAYLOAD_FILE}")"
[[ "${STREAK}" =~ ^[0-9]+$ ]] || STREAK=1
if [ "${STREAK}" -lt "${STREAK_THRESHOLD}" ]; then
	log "skip reason=below_streak issue=${ISSUE_NUMBER} phase=${PHASE} streak=${STREAK} threshold=${STREAK_THRESHOLD}"
	exit 0
fi

SKIP_REASON="$(python3 "${HEAL_PY}" skip-reason --payload-json "${PAYLOAD_FILE}" --self-repo "${REPO}" 2>/dev/null || echo "")"
if [ -n "${SKIP_REASON}" ]; then
	log "skip reason=${SKIP_REASON} issue=${ISSUE_NUMBER} phase=${PHASE}"
	exit 0
fi

# The report is enveloped under client_payload.report: GitHub rejects a
# client_payload with more than 10 top-level properties (HTTP 422).
DISPATCH_FILE="${REPORT_DIR}/dispatch.json"
if ! python3 "${HEAL_PY}" wrap-dispatch --payload-json "${PAYLOAD_FILE}" > "${DISPATCH_FILE}" 2> "${BUILD_ERROR_FILE}"; then
	log "skip reason=dispatch_envelope_failed issue=${ISSUE_NUMBER} phase=${PHASE} detail=$(head -c 200 "${BUILD_ERROR_FILE}" | tr '\n' ' ')"
	exit 0
fi
DISPATCH_ERROR_FILE="${REPORT_DIR}/dispatch_error.txt"
if ! gh_retry gh api -X POST "repos/${UPSTREAM_REPO}/dispatches" --input "${DISPATCH_FILE}" >/dev/null 2> "${DISPATCH_ERROR_FILE}"; then
	log "skip reason=dispatch_denied issue=${ISSUE_NUMBER} phase=${PHASE} streak=${STREAK} upstream=${UPSTREAM_REPO} detail=$(head -c 300 "${DISPATCH_ERROR_FILE}" | tr '\n' ' ')"
	echo "::warning::Workflow failure heal report for ${REPO}#${ISSUE_NUMBER} (${PHASE}) could not be dispatched to ${UPSTREAM_REPO}."
	exit 0
fi
log "dispatched issue=${ISSUE_NUMBER} phase=${PHASE} streak=${STREAK} run=${RUN_ID} workflow=${WORKFLOW_NAME} wrapper_sha=${WRAPPER_SHA:-none} upstream=${UPSTREAM_REPO}"
exit 0
