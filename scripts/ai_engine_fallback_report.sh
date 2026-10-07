#!/usr/bin/env bash
#
# ai_engine_fallback_report.sh
#
# Runs in the `engine-fallback-report` job of clarify.yml, plan.yml,
# implement.yml and orchestrate_clarify_respond.yml (consumer repositories and
# coding-workflows alike) when the phase job of the same run ran a Claude role
# on codex instead (AI_ENGINE_FALLBACK). The phase job's "Collect AI engine
# fallbacks" step classified them (scripts/claude_engine.py fallbacks) into
# the job output this script reads. It:
#
#   1. Sends one Telegram WARNING naming every fallback of the run, capacity
#      ones included. The phase job's model steps hold no Telegram secret, so
#      ai_engine_fallback's own note never reached anyone; with
#      AI_ENGINE_FALLBACK_REPORTER=true that note is skipped and this one is
#      the run's single alert.
#   2. For each `defect` fallback (a setup or code fault, not "every account
#      is over its usage limit"), sends one `repository_dispatch` (event type
#      `workflow-failure-heal`, source_kind `engine_fallback`) to
#      coding-workflows. Its intake verifies the fallback line in the run's job
#      log, de-duplicates on role + reason (one open heal issue across
#      repositories; repeats become occurrence comments) and opens the heal
#      issue that gets the root cause fixed.
#
# A separate job, so the phase job has finished and its log is complete when
# the intake reads it.
#
# API calls (CLAUDE.md §15): one dispatch per distinct defect fallback (one in
# practice), nothing else. The issue title, URL and labels come from the event
# through the environment, so no issue read is needed.
#
# The reporter never fails the job: every exit is 0 and every outcome is a
# stable log line prefixed AI_ENGINE_FALLBACK_REPORT.
#
# Required env (set by the workflow):
#   GITHUB_REPOSITORY                owner/repo of the run
#   GITHUB_RUN_ID                    the run whose phase job fell back
#   ENGINE_FALLBACKS_JSON            the phase job's engine_fallbacks output
#   ENGINE_FALLBACK_PHASE            clarify | plan | implement | clarify_respond
#   GH_TOKEN                         GH_PAT, able to dispatch to coding-workflows
#
# Optional env (have defaults):
#   ENGINE_FALLBACK_ISSUE_NUMBER / _ISSUE_TITLE / _ISSUE_URL / _ISSUE_LABELS
#                                    the issue the phase ran on (context only)
#   ENGINE_FALLBACK_JOB_RESULT       the phase job's result (needs.<job>.result)
#   WORKFLOW_HEAL_ENABLED            "false" sends the alert but no heal report; default on
#   WORKFLOW_HEAL_UPSTREAM_REPO      dispatch target (default shubhodeep1/coding-workflows)
#   TG_BOT_SECRET, TG_CHAT_ID, ALERT_MSG_LEVEL   Telegram (tg_helpers.sh)
#   REPORT_WORKFLOW_NAME             caller workflow display name (github.workflow)
#   REPORT_WRAPPER_SHA               coding-workflows commit the support scripts came from
#   RUNTIME_DIR                      scratch dir

set -uo pipefail

log()
{
	echo "AI_ENGINE_FALLBACK_REPORT $*"
}

HEAL_ENABLED="${WORKFLOW_HEAL_ENABLED:-true}"
UPSTREAM_REPO="${WORKFLOW_HEAL_UPSTREAM_REPO:-shubhodeep1/coding-workflows}"
REPO="${GITHUB_REPOSITORY:-}"
PHASE="${ENGINE_FALLBACK_PHASE:-}"
RUN_ID="${GITHUB_RUN_ID:-}"
REPORTER_RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${REPO}/actions/runs/${RUN_ID}"
WORKFLOW_NAME="${REPORT_WORKFLOW_NAME:-${GITHUB_WORKFLOW:-${PHASE}}}"
WRAPPER_SHA="$(printf '%s' "${REPORT_WRAPPER_SHA:-}" | tr '[:upper:]' '[:lower:]')"
export PYTHONDONTWRITEBYTECODE=1

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEAL_PY="${WORKFLOW_HEAL_PY:-${script_dir}/workflow_failure_heal.py}"

if [ -f "${script_dir}/gh_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source "${script_dir}/gh_helpers.sh" 2>/dev/null || true
fi
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
if [ -f "${script_dir}/tg_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source "${script_dir}/tg_helpers.sh" 2>/dev/null || true
fi
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { return 0; }

# --- Gates -----------------------------------------------------------------

case "${PHASE}" in
	clarify|plan|implement|clarify_respond) ;;
	*)
		log "skip reason=unknown_phase phase=${PHASE:-none}"
		exit 0
		;;
esac
if [ -z "${REPO}" ] || ! [[ "${RUN_ID}" =~ ^[0-9]+$ ]]; then
	log "skip reason=missing_context repo=${REPO:-none} run=${RUN_ID:-none}"
	exit 0
fi
if [ ! -f "${HEAL_PY}" ]; then
	log "skip reason=heal_helper_missing path=${HEAL_PY}"
	exit 0
fi

WORK_DIR="${RUNTIME_DIR:-${TMPDIR:-/tmp}}"
mkdir -p "${WORK_DIR}" 2>/dev/null || WORK_DIR="${TMPDIR:-/tmp}"
REPORT_DIR="${WORK_DIR}/ai_engine_fallback_report"
mkdir -p "${REPORT_DIR}"

# --- Classify + build ----------------------------------------------------------

RESULT_FILE="${REPORT_DIR}/result.json"
BUILD_ERROR_FILE="${REPORT_DIR}/build_error.txt"
if ! python3 "${HEAL_PY}" build-engine-fallback-payloads --repo "${REPO}" --phase "${PHASE}" \
	--fallbacks-json "${ENGINE_FALLBACKS_JSON:-[]}" --workflow-name "${WORKFLOW_NAME}" --run-id "${RUN_ID}" \
	--conclusion "${ENGINE_FALLBACK_JOB_RESULT:-}" --issue-number "${ENGINE_FALLBACK_ISSUE_NUMBER:-}" \
	--issue-title "${ENGINE_FALLBACK_ISSUE_TITLE:-}" --issue-url "${ENGINE_FALLBACK_ISSUE_URL:-}" \
	--labels-json "${ENGINE_FALLBACK_ISSUE_LABELS:-[]}" --wrapper-sha "${WRAPPER_SHA}" \
	--reporter-run-url "${REPORTER_RUN_URL}" > "${RESULT_FILE}" 2> "${BUILD_ERROR_FILE}" \
	|| ! jq -e '(.entries | type == "array") and (.payloads | type == "array")' "${RESULT_FILE}" >/dev/null 2>&1; then
	log "skip reason=payload_build_failed phase=${PHASE} detail=$(head -c 200 "${BUILD_ERROR_FILE}" 2>/dev/null | tr '\n' ' ')"
	exit 0
fi
ENTRY_COUNT="$(jq '.entries | length' "${RESULT_FILE}")"
if [ "${ENTRY_COUNT}" -eq 0 ]; then
	log "skip reason=no_valid_fallbacks phase=${PHASE} run=${RUN_ID}"
	exit 0
fi
# Tab is whitespace to `read`, so an empty field would collapse: fill it.
while IFS=$'\t' read -r role reason detail fallback_class; do
	log "fallback role=${role} reason=${reason} detail=${detail} class=${fallback_class} phase=${PHASE} run=${RUN_ID}"
done < <(jq -r '.entries[] | [.role, .reason, (if .detail == "" then "none" else .detail end), .class] | @tsv' "${RESULT_FILE}")

# --- Alert -------------------------------------------------------------------

ALERT_TEXT="$(jq -r '.alert' "${RESULT_FILE}")"
tg_send_msg "${ALERT_TEXT}" "WARNING" >/dev/null 2>&1 || true
log "alerted phase=${PHASE} run=${RUN_ID} fallbacks=${ENTRY_COUNT}"

# --- Heal reports (defect fallbacks only) --------------------------------------

PAYLOAD_COUNT="$(jq '.payloads | length' "${RESULT_FILE}")"
if [ "${PAYLOAD_COUNT}" -eq 0 ]; then
	log "skip reason=capacity_only phase=${PHASE} run=${RUN_ID}"
	exit 0
fi
if [ "$(printf '%s' "${HEAL_ENABLED}" | tr '[:upper:]' '[:lower:]')" = "false" ]; then
	log "skip reason=heal_disabled (WORKFLOW_HEAL_ENABLED=false) phase=${PHASE} run=${RUN_ID}"
	exit 0
fi

for ((index = 0; index < PAYLOAD_COUNT; index++)); do
	PAYLOAD_FILE="${REPORT_DIR}/payload-${index}.json"
	jq ".payloads[${index}]" "${RESULT_FILE}" > "${PAYLOAD_FILE}"
	ROLE="$(jq -r '.engine_role' "${PAYLOAD_FILE}")"
	REASON="$(jq -r '.failure_reason' "${PAYLOAD_FILE}")"
	SKIP_REASON="$(python3 "${HEAL_PY}" skip-reason --payload-json "${PAYLOAD_FILE}" --self-repo "${REPO}" 2>/dev/null || echo "")"
	if [ -n "${SKIP_REASON}" ]; then
		log "skip reason=${SKIP_REASON} role=${ROLE} fallback_reason=${REASON} phase=${PHASE}"
		continue
	fi
	DISPATCH_FILE="${REPORT_DIR}/dispatch-${index}.json"
	if ! python3 "${HEAL_PY}" wrap-dispatch --payload-json "${PAYLOAD_FILE}" > "${DISPATCH_FILE}" 2> "${BUILD_ERROR_FILE}"; then
		log "skip reason=dispatch_envelope_failed role=${ROLE} fallback_reason=${REASON} detail=$(head -c 200 "${BUILD_ERROR_FILE}" | tr '\n' ' ')"
		continue
	fi
	DISPATCH_ERROR_FILE="${REPORT_DIR}/dispatch_error-${index}.txt"
	if ! gh_retry gh api -X POST "repos/${UPSTREAM_REPO}/dispatches" --input "${DISPATCH_FILE}" >/dev/null 2> "${DISPATCH_ERROR_FILE}"; then
		log "skip reason=dispatch_denied role=${ROLE} fallback_reason=${REASON} upstream=${UPSTREAM_REPO} detail=$(head -c 300 "${DISPATCH_ERROR_FILE}" | tr '\n' ' ')"
		echo "::warning::Claude engine fallback report for ${REPO} run ${RUN_ID} (${ROLE} ${REASON}) could not be dispatched to ${UPSTREAM_REPO}."
		continue
	fi
	log "dispatched role=${ROLE} fallback_reason=${REASON} phase=${PHASE} run=${RUN_ID} workflow=${WORKFLOW_NAME} wrapper_sha=${WRAPPER_SHA:-none} upstream=${UPSTREAM_REPO}"
done
exit 0
