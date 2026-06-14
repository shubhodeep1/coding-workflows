#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./comprehensive_test_and_release_gh_api.sh
. "${SCRIPT_DIR}/comprehensive_test_and_release_gh_api.sh"
GITHUB_OUTPUT="${GITHUB_OUTPUT:-/dev/null}"

usage()
{
	cat <<'EOF' >&2
Usage: dispatch_and_watch_workflow_run.sh \
	--repo <owner/repo> \
	--workflow <workflow.yml> \
	[--display-name <name>] \
	--registration-timeout-secs <secs> \
	[--registration-poll-interval-secs <secs>] \
	[--completion-timeout-secs <secs>] \
	[--completion-poll-interval-secs <secs>] \
	[--dispatch-max-attempts <n>] \
	[--dispatch-backoff-base-secs <n>] \
	[--allowed-conclusions <csv>] \
	[--status-log-prefix <prefix>] \
	[--snapshot-only] \
	[--dispatch-fail-open] \
	[--registration-fail-open] \
	[--field key=value]...
EOF
}

require_numeric()
{
	local name="$1"
	local value="$2"
	if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
		echo "::error::${name} must be an integer, got '${value}'" >&2
		exit 2
	fi
}

require_option_argument()
{
	local option="${1:-}"
	if [ "$#" -lt 2 ]; then
		echo "::error::${option} requires an argument" >&2
		usage
		exit 2
	fi
}

conclusion_allowed()
{
	local conclusion="$1"
	local allowed
	local allowed_value
	IFS=',' read -r -a allowed <<< "${ALLOWED_CONCLUSIONS_CSV}"
	for allowed_value in "${allowed[@]}"; do
		if [ "${conclusion}" = "${allowed_value}" ]; then
			return 0
		fi
	done
	return 1
}

dispatch_workflow()
{
	local attempt=1
	local backoff=0
	local cmd=(gh workflow run "${WORKFLOW_FILE}" --repo "${TARGET_REPO}")
	local field
	for field in "${DISPATCH_FIELDS[@]}"; do
		cmd+=(--field "${field}")
	done

	while true; do
		if "${cmd[@]}"; then
			return 0
		fi

		if [ "${attempt}" -ge "${DISPATCH_MAX_ATTEMPTS}" ]; then
			echo "::error::gh workflow run ${WORKFLOW_FILE} failed after ${DISPATCH_MAX_ATTEMPTS} attempts"
			return 1
		fi

		backoff=$(( DISPATCH_BACKOFF_BASE_SECS ** attempt ))
		echo "::warning::gh workflow run ${WORKFLOW_FILE} failed (attempt ${attempt}/${DISPATCH_MAX_ATTEMPTS}), retrying in ${backoff}s..."
		sleep "${backoff}"
		attempt=$((attempt + 1))
	done
}

TARGET_REPO=""
WORKFLOW_FILE=""
DISPLAY_NAME=""
REGISTRATION_TIMEOUT_SECS=""
REGISTRATION_POLL_INTERVAL_SECS=5
COMPLETION_TIMEOUT_SECS=""
COMPLETION_POLL_INTERVAL_SECS=15
DISPATCH_MAX_ATTEMPTS=1
DISPATCH_BACKOFF_BASE_SECS=2
ALLOWED_CONCLUSIONS_CSV="success"
STATUS_LOG_PREFIX=""
SNAPSHOT_ONLY=0
DISPATCH_FAIL_OPEN=0
REGISTRATION_FAIL_OPEN=0
DISPATCH_FIELDS=()

while [ "$#" -gt 0 ]; do
	case "$1" in
		--repo)
			require_option_argument "$@"
			TARGET_REPO="$2"
			shift 2
			;;
		--workflow)
			require_option_argument "$@"
			WORKFLOW_FILE="$2"
			shift 2
			;;
		--display-name)
			require_option_argument "$@"
			DISPLAY_NAME="$2"
			shift 2
			;;
		--registration-timeout-secs)
			require_option_argument "$@"
			REGISTRATION_TIMEOUT_SECS="$2"
			shift 2
			;;
		--registration-poll-interval-secs)
			require_option_argument "$@"
			REGISTRATION_POLL_INTERVAL_SECS="$2"
			shift 2
			;;
		--completion-timeout-secs)
			require_option_argument "$@"
			COMPLETION_TIMEOUT_SECS="$2"
			shift 2
			;;
		--completion-poll-interval-secs)
			require_option_argument "$@"
			COMPLETION_POLL_INTERVAL_SECS="$2"
			shift 2
			;;
		--dispatch-max-attempts)
			require_option_argument "$@"
			DISPATCH_MAX_ATTEMPTS="$2"
			shift 2
			;;
		--dispatch-backoff-base-secs)
			require_option_argument "$@"
			DISPATCH_BACKOFF_BASE_SECS="$2"
			shift 2
			;;
		--allowed-conclusions)
			require_option_argument "$@"
			ALLOWED_CONCLUSIONS_CSV="$2"
			shift 2
			;;
		--status-log-prefix)
			require_option_argument "$@"
			STATUS_LOG_PREFIX="$2"
			shift 2
			;;
		--snapshot-only)
			SNAPSHOT_ONLY=1
			shift
			;;
		--dispatch-fail-open)
			DISPATCH_FAIL_OPEN=1
			shift
			;;
		--registration-fail-open)
			REGISTRATION_FAIL_OPEN=1
			shift
			;;
		--field)
			require_option_argument "$@"
			DISPATCH_FIELDS+=("$2")
			shift 2
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			echo "::error::Unknown argument: $1" >&2
			usage
			exit 2
			;;
	esac
done

if [ -z "${TARGET_REPO}" ] || [ -z "${WORKFLOW_FILE}" ] || [ -z "${REGISTRATION_TIMEOUT_SECS}" ]; then
	echo "::error::--repo, --workflow, and --registration-timeout-secs are required" >&2
	usage
	exit 2
fi

if [ -z "${DISPLAY_NAME}" ]; then
	DISPLAY_NAME="${WORKFLOW_FILE%.yml}"
fi

require_numeric "registration timeout" "${REGISTRATION_TIMEOUT_SECS}"
require_numeric "registration poll interval" "${REGISTRATION_POLL_INTERVAL_SECS}"
require_numeric "dispatch max attempts" "${DISPATCH_MAX_ATTEMPTS}"
require_numeric "dispatch backoff base" "${DISPATCH_BACKOFF_BASE_SECS}"

if [ "${SNAPSHOT_ONLY}" != "1" ]; then
	if [ -z "${COMPLETION_TIMEOUT_SECS}" ]; then
		echo "::error::--completion-timeout-secs is required unless --snapshot-only is set" >&2
		exit 2
	fi
	require_numeric "completion timeout" "${COMPLETION_TIMEOUT_SECS}"
	require_numeric "completion poll interval" "${COMPLETION_POLL_INTERVAL_SECS}"
fi

PRE_RUN_ID="$(gh_api_safe_quiet_print "repos/${TARGET_REPO}/actions/workflows/${WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=1" --jq '.workflow_runs[0].id // 0' || echo "0")"
if [[ ! "${PRE_RUN_ID}" =~ ^[0-9]+$ ]]; then
	PRE_RUN_ID=0
fi

REGISTRATION_WINDOW_START_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if ! dispatch_workflow; then
	if [ "${DISPATCH_FAIL_OPEN}" = "1" ]; then
		echo "run_id=" >> "${GITHUB_OUTPUT}"
		echo "workflow_conclusion=dispatch_failed" >> "${GITHUB_OUTPUT}"
		echo "::warning::${DISPLAY_NAME} dispatch failed; continuing because this check is non-blocking"
		exit 0
	fi
	exit 1
fi

DISPATCH_STARTED_AT="$(date +%s)"
REGISTRATION_DEADLINE=$((DISPATCH_STARTED_AT + REGISTRATION_TIMEOUT_SECS))
NEW_ID=""
while [ "$(date +%s)" -lt "${REGISTRATION_DEADLINE}" ]; do
	NEW_ID="$(gh_api_safe_quiet_print "repos/${TARGET_REPO}/actions/workflows/${WORKFLOW_FILE}/runs?event=workflow_dispatch&created=>${REGISTRATION_WINDOW_START_UTC}&per_page=10" --jq "[.workflow_runs[] | select(.id > ${PRE_RUN_ID:-0})] | sort_by(.created_at) | last | .id // empty" || echo "")"
	[ -n "${NEW_ID}" ] && break
	sleep "${REGISTRATION_POLL_INTERVAL_SECS}"
done

if [ -z "${NEW_ID}" ]; then
	if [ "${REGISTRATION_FAIL_OPEN}" = "1" ]; then
		echo "run_id=" >> "${GITHUB_OUTPUT}"
		echo "workflow_conclusion=dispatch_not_registered" >> "${GITHUB_OUTPUT}"
		echo "::warning::${DISPLAY_NAME} dispatch did not register; continuing because this check is non-blocking"
		exit 0
	fi
	echo "::error::${DISPLAY_NAME} dispatch did not register"
	exit 1
fi

if [ "${SNAPSHOT_ONLY}" = "1" ]; then
	echo "Registered run #${NEW_ID}"
	RUN_JSON="$(gh_api_safe_quiet_print "repos/${TARGET_REPO}/actions/runs/${NEW_ID}" --jq '{status, conclusion}' || echo "")"
	if [ -z "${RUN_JSON}" ]; then
		RUN_JSON='{}'
		echo "::warning::${DISPLAY_NAME} snapshot query failed for run #${NEW_ID}; child run context unavailable"
	fi
	STATUS="$(printf '%s' "${RUN_JSON}" | jq -r '.status // "unknown"' 2>/dev/null || echo "unknown")"
	CONCLUSION="$(printf '%s' "${RUN_JSON}" | jq -r '.conclusion // ""' 2>/dev/null || echo "")"
	if [ -n "${CONCLUSION}" ]; then
		echo "  snapshot: status=${STATUS} conclusion=${CONCLUSION}"
	else
		echo "  snapshot: status=${STATUS}"
	fi
	echo "run_id=${NEW_ID}" >> "${GITHUB_OUTPUT}"
	if [ "${STATUS}" = "completed" ] && [ -n "${CONCLUSION}" ]; then
		echo "workflow_conclusion=${CONCLUSION}" >> "${GITHUB_OUTPUT}"
	else
		echo "workflow_conclusion=success" >> "${GITHUB_OUTPUT}"
	fi
	echo "workflow_snapshot_status=${STATUS}" >> "${GITHUB_OUTPUT}"
	echo "workflow_snapshot_conclusion=${CONCLUSION}" >> "${GITHUB_OUTPUT}"
	if [ "${STATUS}" = "completed" ] && [ -n "${CONCLUSION}" ] && [ "${CONCLUSION}" != "success" ]; then
		echo "::warning::${DISPLAY_NAME} run #${NEW_ID} is already completed with conclusion=${CONCLUSION}; not waiting because this check is non-blocking"
	fi
	exit 0
fi

echo "Watching run #${NEW_ID}"
COMPLETION_DEADLINE=$((DISPATCH_STARTED_AT + COMPLETION_TIMEOUT_SECS))
STATUS=""
CONCLUSION=""
while [ "$(date +%s)" -lt "${COMPLETION_DEADLINE}" ]; do
	RUN_JSON="$(gh_api_safe_quiet_print "repos/${TARGET_REPO}/actions/runs/${NEW_ID}" --jq '{status, conclusion}' || echo "")"
	if [ -n "${RUN_JSON}" ]; then
		STATUS="$(printf '%s' "${RUN_JSON}" | jq -r '.status // ""' 2>/dev/null || echo "")"
		CONCLUSION="$(printf '%s' "${RUN_JSON}" | jq -r '.conclusion // ""' 2>/dev/null || echo "")"
	else
		STATUS=""
		CONCLUSION=""
	fi
	if [ -n "${STATUS_LOG_PREFIX}" ]; then
		echo "  ${STATUS_LOG_PREFIX} status=${STATUS} conclusion=${CONCLUSION}"
	else
		echo "  status=${STATUS} conclusion=${CONCLUSION}"
	fi
	if [ "${STATUS}" = "completed" ]; then
		echo "run_id=${NEW_ID}" >> "${GITHUB_OUTPUT}"
		echo "workflow_conclusion=${CONCLUSION}" >> "${GITHUB_OUTPUT}"
		if conclusion_allowed "${CONCLUSION}"; then
			exit 0
		fi
		echo "::error::${DISPLAY_NAME} run #${NEW_ID} concluded ${CONCLUSION}"
		exit 1
	fi
	sleep "${COMPLETION_POLL_INTERVAL_SECS}"
done

echo "::error::${DISPLAY_NAME} run #${NEW_ID} timed out"
exit 1
