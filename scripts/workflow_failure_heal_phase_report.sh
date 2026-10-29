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
# API calls (CLAUDE.md §15): one issue read, one identity read, one paginated
# comment read, one dispatch. The phase job's own issue snapshot and comment
# reads live in another job and predate its failure comment, so neither can be
# reused; the event payload's issue copy is not used because a long body can
# push a step's environment past the kernel's per-variable limit.
#
# The reporter never fails the job: every exit is 0 and every outcome is a
# stable log line prefixed WORKFLOW_HEAL_PHASE_REPORT.
#
# Required env (set by the workflow):
#   GITHUB_REPOSITORY              owner/repo of the issue
#   GH_TOKEN                       GitHub token (GH_PAT) able to read the issue and
#                                  dispatch to coding-workflows
#   WORKFLOW_HEAL_PHASE            clarify | plan | implement | validate
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
#
# Validate phase (WORKFLOW_HEAL_PHASE=validate; plan
# docs/plans/validation-failure-harness-routing-plan.md, Phase 1): the validate
# run's heal-report job sends one `validation_failure` report for a runtime
# validation failure owned by the harness this repository ships. The issue is
# the project's tracking issue. Optional env, each with a default; a missing or
# malformed value skips with a log line and exits 0:
#   WORKFLOW_HEAL_VALIDATION_STAGE           harness stage (default empty)
#   WORKFLOW_HEAL_VALIDATION_REASON          harness reason (default empty)
#   WORKFLOW_HEAL_VALIDATION_SIGNATURE_FILE  regular file holding the error signature (default empty)
#   WORKFLOW_HEAL_VALIDATION_FP              fingerprint the validate run computed (default empty)
#   VALIDATION_HARNESS_HEAL_ENABLED          "false" (any case) skips this phase (default true)
# API calls for this phase: the OIDC report-identity request and one dispatch;
# no issue, /user or comment read (the intake verifies the run itself).

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
VALIDATION_STAGE="${WORKFLOW_HEAL_VALIDATION_STAGE:-}"
VALIDATION_REASON="${WORKFLOW_HEAL_VALIDATION_REASON:-}"
VALIDATION_SIGNATURE_FILE="${WORKFLOW_HEAL_VALIDATION_SIGNATURE_FILE:-}"
VALIDATION_FP="$(printf '%s' "${WORKFLOW_HEAL_VALIDATION_FP:-}" | tr '[:upper:]' '[:lower:]')"
VALIDATION_HEAL_SWITCH="${VALIDATION_HARNESS_HEAL_ENABLED:-true}"

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
	clarify|plan|implement|validate) ;;
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

# --- Validate phase: one validation_failure dispatch ---------------------------

if [ "${PHASE}" = "validate" ]; then
	if [ "$(printf '%s' "${VALIDATION_HEAL_SWITCH}" | tr '[:upper:]' '[:lower:]')" = "false" ]; then
		log "skip reason=disabled (VALIDATION_HARNESS_HEAL_ENABLED=false) issue=${ISSUE_NUMBER} run=${RUN_ID}"
		exit 0
	fi
	if ! [[ "${VALIDATION_STAGE}" =~ ^[a-z_]{1,40}$ ]]; then
		log "skip reason=missing_validation_context field=stage issue=${ISSUE_NUMBER} run=${RUN_ID}"
		exit 0
	fi
	if ! [[ "${VALIDATION_REASON}" =~ ^[a-z_]{1,40}$ ]]; then
		log "skip reason=missing_validation_context field=reason issue=${ISSUE_NUMBER} run=${RUN_ID}"
		exit 0
	fi
	if [ -z "${VALIDATION_SIGNATURE_FILE}" ] || [ -L "${VALIDATION_SIGNATURE_FILE}" ] || [ ! -f "${VALIDATION_SIGNATURE_FILE}" ] || [ ! -s "${VALIDATION_SIGNATURE_FILE}" ]; then
		log "skip reason=missing_validation_context field=signature_file issue=${ISSUE_NUMBER} run=${RUN_ID}"
		exit 0
	fi
	if ! [[ "${VALIDATION_FP}" =~ ^[0-9a-f]{64}$ ]]; then
		log "skip reason=missing_validation_context field=fp issue=${ISSUE_NUMBER} run=${RUN_ID}"
		exit 0
	fi
	V_PAYLOAD_FILE="${REPORT_DIR}/validation_payload.json"
	V_BUILD_ERROR_FILE="${REPORT_DIR}/validation_build_error.txt"
	if ! python3 "${HEAL_PY}" build-validation-payload --repo "${REPO}" --issue-number "${ISSUE_NUMBER}" --run-id "${RUN_ID}" \
		--wrapper-sha "${WRAPPER_SHA}" --stage "${VALIDATION_STAGE}" --reason "${VALIDATION_REASON}" \
		--signature-file "${VALIDATION_SIGNATURE_FILE}" --workflow-name "${WORKFLOW_NAME}" \
		--reporter-run-url "${REPORTER_RUN_URL}" > "${V_PAYLOAD_FILE}" 2> "${V_BUILD_ERROR_FILE}"; then
		log "skip reason=payload_build_failed issue=${ISSUE_NUMBER} phase=validate detail=$(head -c 200 "${V_BUILD_ERROR_FILE}" | tr '\n' ' ')"
		exit 0
	fi
	# The intake deduplicates on this fingerprint: recompute it from the built
	# payload so a report can only carry the key its own fields produce.
	V_SIGNATURE_FILE="${REPORT_DIR}/validation_signature.txt"
	V_COMPUTED_FP=""
	if jq -r '.validation_signature // ""' "${V_PAYLOAD_FILE}" > "${V_SIGNATURE_FILE}" 2>/dev/null; then
		V_COMPUTED_FP="$(python3 "${HEAL_PY}" validation-failure-fingerprint --stage "$(jq -r '.validation_stage // ""' "${V_PAYLOAD_FILE}")" \
			--reason "$(jq -r '.validation_reason // ""' "${V_PAYLOAD_FILE}")" --signature-file "${V_SIGNATURE_FILE}" --source-repo "${REPO}" 2>/dev/null || true)"
	fi
	if ! [[ "${V_COMPUTED_FP}" =~ ^[0-9a-f]{64}$ ]]; then
		log "skip reason=fingerprint_unavailable issue=${ISSUE_NUMBER} phase=validate run=${RUN_ID}"
		exit 0
	fi
	if [ "${V_COMPUTED_FP}" != "${VALIDATION_FP}" ]; then
		log "skip reason=fingerprint_mismatch issue=${ISSUE_NUMBER} phase=validate run=${RUN_ID} expected=${VALIDATION_FP} computed=${V_COMPUTED_FP}"
		exit 0
	fi
	V_SKIP_REASON="$(python3 "${HEAL_PY}" skip-reason --payload-json "${V_PAYLOAD_FILE}" --self-repo "${REPO}" 2>/dev/null || echo "")"
	if [ -n "${V_SKIP_REASON}" ]; then
		log "skip reason=${V_SKIP_REASON} issue=${ISSUE_NUMBER} phase=validate"
		exit 0
	fi
	# Report identity: as for the other phases (OIDC token, 0600 file, never
	# printed, removed on exit).
	V_IDENTITY_FILE="${REPORT_DIR}/report_identity.jwt"
	V_DISPATCH_FILE="${REPORT_DIR}/dispatch.json"
	trap 'rm -f "${V_IDENTITY_FILE}" "${V_DISPATCH_FILE}"' EXIT
	V_WRAP_ARGS=(--payload-json "${V_PAYLOAD_FILE}")
	V_IDENTITY_STATUS="$(python3 "${HEAL_PY}" request-report-identity --out "${V_IDENTITY_FILE}" 2>/dev/null || echo "identity=absent reason=helper_failed")"
	V_IDENTITY_STATUS="$(printf '%s' "${V_IDENTITY_STATUS}" | head -1 | tr -cd 'A-Za-z0-9_=. -')"
	log "${V_IDENTITY_STATUS:-identity=absent reason=unknown} issue=${ISSUE_NUMBER} phase=validate"
	if [ -s "${V_IDENTITY_FILE}" ]; then
		V_WRAP_ARGS+=(--report-identity-file "${V_IDENTITY_FILE}")
	fi
	if ! python3 "${HEAL_PY}" wrap-dispatch "${V_WRAP_ARGS[@]}" > "${V_DISPATCH_FILE}" 2> "${V_BUILD_ERROR_FILE}"; then
		log "skip reason=dispatch_envelope_failed issue=${ISSUE_NUMBER} phase=validate detail=$(head -c 200 "${V_BUILD_ERROR_FILE}" | tr '\n' ' ')"
		exit 0
	fi
	V_DISPATCH_ERROR_FILE="${REPORT_DIR}/dispatch_error.txt"
	if ! gh_retry gh api -X POST "repos/${UPSTREAM_REPO}/dispatches" --input "${V_DISPATCH_FILE}" >/dev/null 2> "${V_DISPATCH_ERROR_FILE}"; then
		log "skip reason=dispatch_denied issue=${ISSUE_NUMBER} phase=validate upstream=${UPSTREAM_REPO} detail=$(head -c 300 "${V_DISPATCH_ERROR_FILE}" | tr '\n' ' ')"
		echo "::warning::Workflow failure heal report for ${REPO}#${ISSUE_NUMBER} (validate) could not be dispatched to ${UPSTREAM_REPO}."
		exit 0
	fi
	log "dispatched issue=${ISSUE_NUMBER} phase=validate stage=${VALIDATION_STAGE} reason=${VALIDATION_REASON} fp=${VALIDATION_FP} run=${RUN_ID} workflow=${WORKFLOW_NAME} wrapper_sha=${WRAPPER_SHA:-none} upstream=${UPSTREAM_REPO}"
	exit 0
fi

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
# The issue GET above does not identify the GH_PAT account that posted phase
# comments; resolve it once so user-authored comment bodies cannot alter the streak.
COMMENT_AUTHOR="$(gh_retry gh api --method GET user --jq .login 2>/dev/null || true)"
if [ -z "${COMMENT_AUTHOR}" ]; then
	log "warn comment_author_unavailable issue=${ISSUE_NUMBER}; reporting this run without a streak"
	printf '[]' > "${COMMENTS_JSON_FILE}"
	STREAK_THRESHOLD=1
elif ! gh_retry gh api --method GET --paginate "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" -F per_page=100 \
	--jq '.[] | {body: (.body // ""), user: (.user // {})}' 2>/dev/null \
	| jq -s '.' > "${COMMENTS_JSON_FILE}" 2>/dev/null \
	|| ! jq -e 'type == "array"' "${COMMENTS_JSON_FILE}" >/dev/null 2>&1; then
	log "warn comments_fetch_failed issue=${ISSUE_NUMBER}; reporting this run without a streak"
	printf '[]' > "${COMMENTS_JSON_FILE}"
	STREAK_THRESHOLD=1
fi

# --- Build, gate, validate, dispatch --------------------------------------------

PAYLOAD_FILE="${REPORT_DIR}/payload.json"
BUILD_ERROR_FILE="${REPORT_DIR}/build_error.txt"
if ! python3 "${HEAL_PY}" build-phase-payload --repo "${REPO}" --phase "${PHASE}" --issue-json "${ISSUE_JSON_FILE}" \
	--comments-json "${COMMENTS_JSON_FILE}" --workflow-name "${WORKFLOW_NAME}" --run-id "${RUN_ID}" \
	--wrapper-sha "${WRAPPER_SHA}" --reporter-run-url "${REPORTER_RUN_URL}" --comment-author "${COMMENT_AUTHOR}" > "${PAYLOAD_FILE}" 2> "${BUILD_ERROR_FILE}"; then
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
# Report identity (issues #6559, #6834): see workflow_failure_heal_report.sh.
# The heal-report job is granted id-token: write, so a token is normally
# attached; identity=absent means the OIDC request failed or a caller withheld
# the grant, and the intake then rejects the report when
# WORKFLOW_HEAL_REQUIRE_REPORT_AUTH=true. Only when the staged helper knows the
# subcommand; the token file is 0600, never printed, and removed on exit.
IDENTITY_FILE="${REPORT_DIR}/report_identity.jwt"
DISPATCH_FILE="${REPORT_DIR}/dispatch.json"
trap 'rm -f "${IDENTITY_FILE}" "${DISPATCH_FILE}"' EXIT
WRAP_ARGS=(--payload-json "${PAYLOAD_FILE}")
if grep -q 'request-report-identity' "${HEAL_PY}" 2>/dev/null; then
	IDENTITY_STATUS="$(python3 "${HEAL_PY}" request-report-identity --out "${IDENTITY_FILE}" 2>/dev/null || echo "identity=absent reason=helper_failed")"
	IDENTITY_STATUS="$(printf '%s' "${IDENTITY_STATUS}" | head -1 | tr -cd 'A-Za-z0-9_=. -')"
	log "${IDENTITY_STATUS:-identity=absent reason=unknown} issue=${ISSUE_NUMBER} phase=${PHASE}"
	if [ -s "${IDENTITY_FILE}" ]; then
		WRAP_ARGS+=(--report-identity-file "${IDENTITY_FILE}")
	fi
else
	log "identity=absent reason=helper_outdated issue=${ISSUE_NUMBER} phase=${PHASE}"
fi
if ! python3 "${HEAL_PY}" wrap-dispatch "${WRAP_ARGS[@]}" > "${DISPATCH_FILE}" 2> "${BUILD_ERROR_FILE}"; then
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
