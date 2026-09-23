#!/usr/bin/env bash
#
# workflow_failure_heal_report.sh
#
# Driven by the reusable AI Workflow Failure Heal workflow
# (.github/workflows/workflow_failure_heal.yml). Runs in the repository where a
# human-needed escalation label (ai:needs-human, ai:check-triage-escalated,
# ai:destructive-blocked, ai:scope-blocked, ai:harness-broken,
# ai:resolver-escalated, ai:security-pass-failed) was just applied to an issue
# or pull request. It:
#
#   1. Reads the escalated issue / PR, its comments, and the repository's recent
#      workflow runs, and picks the failed runs that belong to it (run links in
#      the failure comments the pipeline posts, plus failed runs whose display
#      title equals the issue title).
#   2. Records the coding-workflows release SHA the consumer wrappers are pinned
#      to, so the intake can diagnose against exactly the code that ran.
#   3. Sends one thin `repository_dispatch` (event type `workflow-failure-heal`)
#      to coding-workflows, whose intake workflow fetches the full logs, runs
#      the diagnosis model, de-duplicates, and opens the heal issue.
#
# The reporter never diagnoses and never opens issues itself; it only ships
# evidence pointers. All stable log lines are prefixed WORKFLOW_HEAL_REPORT.
#
# Required env (set by the workflow):
#   GITHUB_REPOSITORY              owner/repo the escalation happened in
#   GH_TOKEN                       GitHub token (GH_PAT) able to read this repo's
#                                  runs and to dispatch to coding-workflows
#   WORKFLOW_HEAL_ISSUE_NUMBER     escalated issue / PR number
#   WORKFLOW_HEAL_LABEL            the escalation label that was applied
#
# Optional env (have defaults):
#   WORKFLOW_HEAL_ENABLED          "false" to disable; default on
#   WORKFLOW_HEAL_IS_PULL_REQUEST  "true" when the labeled object is a PR
#   WORKFLOW_HEAL_UPSTREAM_REPO    dispatch target (default shubhodeep1/coding-workflows)
#   WORKFLOW_HEAL_PY               path of workflow_failure_heal.py
#                                  (default scripts/workflow_failure_heal.py)
#   WORKFLOW_HEAL_WRAPPER_DIR      wrapper dir to read the release pin from
#                                  (default .github/workflows)
#   RUNTIME_DIR                    scratch dir for intermediate files
#
# Exit codes: 0 on dispatch, skip, or a fail-open condition that was alerted;
# 1 only when the dispatch itself failed (so the run is visibly red).

set -euo pipefail

log()
{
	echo "WORKFLOW_HEAL_REPORT $*"
}

# --- Helpers (fail open if unavailable) ------------------------------------

source scripts/gh_helpers.sh 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
type gh_api_json_to_file >/dev/null 2>&1 || gh_api_json_to_file()
{
	local _gh_api_json_outfile="$1"
	shift
	"$@" > "${_gh_api_json_outfile}"
}
source scripts/tg_helpers.sh 2>/dev/null || true
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { :; }

# --- Config ----------------------------------------------------------------

ENABLED="${WORKFLOW_HEAL_ENABLED:-true}"
UPSTREAM_REPO="${WORKFLOW_HEAL_UPSTREAM_REPO:-shubhodeep1/coding-workflows}"
HEAL_PY="${WORKFLOW_HEAL_PY:-scripts/workflow_failure_heal.py}"
WRAPPER_DIR="${WORKFLOW_HEAL_WRAPPER_DIR:-.github/workflows}"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
ISSUE_NUMBER="${WORKFLOW_HEAL_ISSUE_NUMBER:-}"
LABEL="${WORKFLOW_HEAL_LABEL:-}"
IS_PR="${WORKFLOW_HEAL_IS_PULL_REQUEST:-false}"
RUNTIME_DIR="${RUNTIME_DIR:-/tmp/workflow-heal-report-${GITHUB_RUN_ID:-local}}"
mkdir -p "${RUNTIME_DIR}"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${REPO}/actions/runs/${GITHUB_RUN_ID:-0}"
export PYTHONDONTWRITEBYTECODE=1

KIND="issue"
if [ "${IS_PR,,}" = "true" ]; then
	KIND="pull_request"
fi

# --- Gates -----------------------------------------------------------------

if [ "${ENABLED,,}" = "false" ]; then
	log "skip reason=disabled (WORKFLOW_HEAL_ENABLED=false) repo=${REPO}"
	exit 0
fi
if ! [[ "${ISSUE_NUMBER}" =~ ^[1-9][0-9]*$ ]]; then
	log "skip reason=invalid_issue_number value=${ISSUE_NUMBER:-<empty>}"
	exit 0
fi
if [ ! -f "${HEAL_PY}" ]; then
	log "error heal_helper_missing path=${HEAL_PY}"
	exit 1
fi
case "${LABEL}" in
	ai:needs-human|ai:check-triage-escalated|ai:destructive-blocked|ai:scope-blocked|ai:harness-broken|ai:resolver-escalated|ai:security-pass-failed) ;;
	*)
		log "skip reason=label_not_human_needed label=${LABEL}"
		exit 0
		;;
esac

# --- Collect evidence pointers ---------------------------------------------

ISSUE_JSON_FILE="${RUNTIME_DIR}/issue.json"
if ! gh_api_json_to_file "${ISSUE_JSON_FILE}" gh api "repos/${REPO}/issues/${ISSUE_NUMBER}"; then
	log "skip reason=issue_fetch_failed issue=${ISSUE_NUMBER}"
	exit 0
fi
if ! jq -e 'type == "object" and (.number | type == "number")' "${ISSUE_JSON_FILE}" >/dev/null 2>&1; then
	log "skip reason=issue_fetch_invalid issue=${ISSUE_NUMBER}"
	exit 0
fi
ISSUE_STATE="$(jq -r '.state // ""' "${ISSUE_JSON_FILE}")"
if [ "${ISSUE_STATE}" != "open" ]; then
	log "skip reason=issue_not_open issue=${ISSUE_NUMBER} state=${ISSUE_STATE}"
	exit 0
fi

COMMENTS_JSON_FILE="${RUNTIME_DIR}/comments.json"
if ! gh_retry gh api --method GET --paginate "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" -F per_page=100 \
	--jq '.[] | {body: (.body // ""), created_at: (.created_at // "")}' 2>/dev/null \
	| jq -s '.' > "${COMMENTS_JSON_FILE}" 2>/dev/null; then
	log "warn comments_fetch_failed issue=${ISSUE_NUMBER}; continuing without comments"
	printf '[]' > "${COMMENTS_JSON_FILE}"
fi
if ! jq -e 'type == "array"' "${COMMENTS_JSON_FILE}" >/dev/null 2>&1; then
	printf '[]' > "${COMMENTS_JSON_FILE}"
fi

RUNS_JSON_FILE="${RUNTIME_DIR}/runs.json"
if ! gh_api_json_to_file "${RUNS_JSON_FILE}" gh api --method GET "repos/${REPO}/actions/runs" -F per_page=100; then
	log "warn runs_fetch_failed repo=${REPO}; continuing without run listing"
	printf '{"workflow_runs":[]}' > "${RUNS_JSON_FILE}"
fi
if ! jq -e 'type == "object"' "${RUNS_JSON_FILE}" >/dev/null 2>&1; then
	printf '{"workflow_runs":[]}' > "${RUNS_JSON_FILE}"
fi

# Release pin: consumer wrappers carry `uses: shubhodeep1/coding-workflows/...@<sha> # stable`.
# In coding-workflows itself the failing run used this very commit.
WRAPPER_SHA=""
if [ "${REPO}" = "${UPSTREAM_REPO}" ]; then
	WRAPPER_SHA="${GITHUB_SHA:-}"
elif [ -d "${WRAPPER_DIR}" ]; then
	WRAPPER_SHA="$(grep -rhoE "uses:[[:space:]]+${UPSTREAM_REPO}/\.github/workflows/[A-Za-z0-9_.-]+\.yml@[0-9a-fA-F]{40}" "${WRAPPER_DIR}" 2>/dev/null \
		| sed -E 's/.*@//' | head -1 || true)"
fi
WRAPPER_SHA="${WRAPPER_SHA,,}"
if ! [[ "${WRAPPER_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
	WRAPPER_SHA=""
	log "warn wrapper_pin_unresolved repo=${REPO}; intake will diagnose against stable"
fi

# --- Build + validate the payload -------------------------------------------

PAYLOAD_FILE="${RUNTIME_DIR}/payload.json"
if ! python3 "${HEAL_PY}" build-issue-payload \
	--repo "${REPO}" \
	--kind "${KIND}" \
	--label "${LABEL}" \
	--issue-json "${ISSUE_JSON_FILE}" \
	--comments-json "${COMMENTS_JSON_FILE}" \
	--runs-json "${RUNS_JSON_FILE}" \
	--wrapper-sha "${WRAPPER_SHA}" \
	--reporter-run-url "${RUN_URL}" \
	> "${PAYLOAD_FILE}"; then
	log "error payload_build_failed issue=${ISSUE_NUMBER}"
	exit 1
fi

SKIP_REASON="$(python3 "${HEAL_PY}" skip-reason --payload-json "${PAYLOAD_FILE}" --self-repo "${REPO}" 2>/dev/null || echo "")"
if [ -n "${SKIP_REASON}" ]; then
	log "skip reason=${SKIP_REASON} issue=${ISSUE_NUMBER} label=${LABEL}"
	exit 0
fi

RUN_REF_COUNT="$(jq -r '.run_refs | length' "${PAYLOAD_FILE}")"

# --- Dispatch to coding-workflows ------------------------------------------

# The report is enveloped under client_payload.report: GitHub rejects a
# client_payload with more than 10 top-level properties (HTTP 422).
DISPATCH_FILE="${RUNTIME_DIR}/dispatch.json"
if ! python3 "${HEAL_PY}" wrap-dispatch --payload-json "${PAYLOAD_FILE}" > "${DISPATCH_FILE}" 2> "${RUNTIME_DIR}/build_error.txt"; then
	log "error dispatch_build_failed issue=${ISSUE_NUMBER} detail=$(head -c 200 "${RUNTIME_DIR}/build_error.txt" | tr '\n' ' ')"
	exit 1
fi

DISPATCH_ERROR_FILE="${RUNTIME_DIR}/dispatch_error.txt"
if ! gh_retry gh api -X POST "repos/${UPSTREAM_REPO}/dispatches" --input "${DISPATCH_FILE}" >/dev/null 2> "${DISPATCH_ERROR_FILE}"; then
	log "error dispatch_failed issue=${ISSUE_NUMBER} label=${LABEL} upstream=${UPSTREAM_REPO} detail=$(head -c 300 "${DISPATCH_ERROR_FILE}" | tr '\n' ' ')"
	tg_send_msg "Workflow failure heal report FAILED to dispatch for ${REPO}#${ISSUE_NUMBER} (label '${LABEL}')."$'\n'"Run: ${RUN_URL}" "ERROR" >/dev/null 2>&1 || true
	exit 1
fi

log "dispatched issue=${ISSUE_NUMBER} kind=${KIND} label=${LABEL} runs=${RUN_REF_COUNT} wrapper_sha=${WRAPPER_SHA:-none} upstream=${UPSTREAM_REPO}"
tg_send_msg "Workflow failure heal report dispatched for ${REPO}#${ISSUE_NUMBER} (label '${LABEL}', ${RUN_REF_COUNT} failed run(s) linked). coding-workflows will diagnose it."$'\n'"Run: ${RUN_URL}" "DEBUG" >/dev/null 2>&1 || true
