#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./comprehensive_test_and_release_gh_api.sh
. "${SCRIPT_DIR}/comprehensive_test_and_release_gh_api.sh"
GITHUB_OUTPUT="${GITHUB_OUTPUT:-/dev/null}"
# The registration and completion polls read through
# gh_api_safe_quiet_print_read (issue #9888): the job token when the step
# exports GH_READ_TOKEN, the PAT otherwise. The dispatch, the one-off reads
# and the cancel stay on the PAT.

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

       dispatch_and_watch_workflow_run.sh --stop-release-gate \
	--repo <child owner/repo> \
	--watched-run-id <id> \
	[--display-name <name>] \
	[--runner-wait-reason <never_assigned|late_assigned>]

       dispatch_and_watch_workflow_run.sh --alt-model-runner-wait
	(env: TEST_REPO, ALT_ISSUE_TITLE, ALT_CREATED_AFTER, ALT_WAIT_STARTED_AT)
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

# Release-gate runner wait (issue #10037, G3).
#
# dispatch_watch_stop_release_gate <watched_run_id> <display_name> <reason>
# Stops the release gate run this step belongs to (GITHUB_RUN_ID in
# GITHUB_REPOSITORY) after a watched child ended as runner_wait: the child
# never got a runner, or got one only in the last third of its budget. The
# gate cancels itself so the run ends `cancelled`, which both release scripts
# already read as "no attempt used". It never hides a real failure: when any
# gate job already ended failure or timed_out, or a still-running gate job
# already has a failed step (its always()/failure() steps still run), it does
# not cancel (outcome=real_failure_present). A job of the gate that fails in the few
# seconds between that read and the cancel landing may show as cancelled.
# Log lines (stable prefix, matched by the heal intake):
#   RELEASE_GATE_RUNNER_WAIT outcome=cancel_requested|real_failure_present|stop_failed ...
# API calls: 1 gate-jobs read, 1 cancel. Returns 1 when the gate is not
# stopped (the caller then fails the step as before).
dispatch_watch_stop_release_gate()
{
	local stop_watched="${1:-}" stop_display="${2:-}" stop_reason="${3:-}"
	local stop_jobs="" stop_real="" stop_waited=0 stop_wait_secs="${DISPATCH_WATCH_STOP_WAIT_SECS:-240}"
	stop_display="$(printf '%s' "${stop_display}" | tr '[:space:]' '_' | tr -cd 'A-Za-z0-9._:()/-' | cut -c1-60)"
	stop_reason="$(printf '%s' "${stop_reason}" | tr -cd 'a-z_' | cut -c1-40)"
	[[ "${stop_watched}" =~ ^[0-9]+$ ]] || stop_watched="none"
	[[ "${stop_wait_secs}" =~ ^[0-9]{1,5}$ ]] || stop_wait_secs=240
	if ! [[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]] || ! [[ "${GITHUB_REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
		echo "RELEASE_GATE_RUNNER_WAIT outcome=stop_failed detail=no_gate_run watched_run=${stop_watched} reason=${stop_reason:-unknown} display_name=${stop_display:-unknown}"
		return 1
	fi
	if ! stop_jobs="$(gh_api_safe_quiet_print "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/jobs?per_page=100")" \
		|| ! stop_real="$(printf '%s' "${stop_jobs}" | jq -er '.jobs | if type == "array" then [.[] | select(.conclusion == "failure" or .conclusion == "timed_out" or any(.steps[]?; .conclusion == "failure" or .conclusion == "timed_out"))] | length else error("no jobs") end' 2>/dev/null)"; then
		echo "RELEASE_GATE_RUNNER_WAIT outcome=stop_failed detail=gate_jobs_unreadable gate_run=${GITHUB_RUN_ID} watched_run=${stop_watched} reason=${stop_reason:-unknown} display_name=${stop_display:-unknown}"
		return 1
	fi
	if [ "${stop_real}" != "0" ]; then
		echo "RELEASE_GATE_RUNNER_WAIT outcome=real_failure_present gate_run=${GITHUB_RUN_ID} watched_run=${stop_watched} reason=${stop_reason:-unknown} failed_jobs=${stop_real} display_name=${stop_display:-unknown}"
		return 1
	fi
	echo "RELEASE_GATE_RUNNER_WAIT outcome=cancel_requested gate_run=${GITHUB_RUN_ID} watched_run=${stop_watched} reason=${stop_reason:-unknown} display_name=${stop_display:-unknown}"
	if ! timeout 20 gh api -X POST "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/cancel" >/dev/null 2>&1; then
		echo "RELEASE_GATE_RUNNER_WAIT outcome=stop_failed detail=cancel_failed gate_run=${GITHUB_RUN_ID} watched_run=${stop_watched} reason=${stop_reason:-unknown} display_name=${stop_display:-unknown}"
		return 1
	fi
	# The runner kills this step once the cancel lands, so the job ends
	# cancelled, not failed.
	while [ "${stop_waited}" -lt "${stop_wait_secs}" ]; do
		sleep 10
		stop_waited=$((stop_waited + 10))
	done
	echo "RELEASE_GATE_RUNNER_WAIT outcome=stop_failed detail=still_running gate_run=${GITHUB_RUN_ID} watched_run=${stop_watched} reason=${stop_reason:-unknown} waited_secs=${stop_waited} display_name=${stop_display:-unknown}"
	return 1
}

# --alt-model-runner-wait (issue #10037, G3). Run by the "Wait for
# clarify→plan→implement (alt-model)" step of the e2e-alt-model-test job in
# .github/workflows/test-and-mark-stable.yml when its wait budget runs out
# (logic here keeps the workflow well under the 480,000-byte guard, §24).
#
# Decides whether the alt-model pipeline timed out only because this issue's
# runs waited for runners (release run 45296809251: the issue's runs sat 17 to
# 56 minutes in the runner queue). The step polls labels and has no single
# child run id, so the candidates are this issue's runs that are not
# completed: display_title equal to ALT_ISSUE_TITLE, created after
# ALT_CREATED_AFTER, status in_progress or queued.
#
# runner_wait only when ALL of these hold, otherwise timeout (fail closed):
#   - the in_progress listing is complete (at most 100 runs) and readable;
#   - no run of this issue is pending or waiting (held by a concurrency group
#     or an environment), and no job of a queued candidate is waiting/pending
#     (reason=held otherwise);
#   - at least one candidate exists and at most ALT_RUNNER_WAIT_MAX_RUNS
#     candidates (queued and in_progress together) are found;
#   - at least one candidate was created at or before the two-thirds point of
#     the budget, so it waited at least a third of the budget;
#   - every job of every in_progress candidate is status=queued with no
#     runner_name, or started its first step in the last third of the budget
#     and has no failed conclusion or step. An in_progress candidate whose
#     jobs cannot be read, or that has no jobs, gives timeout.
# A queued candidate has no job with a runner; its jobs are read only to rule
# out a held job (an unreadable list gives timeout). A truncated queued
# listing only leaves out more queued runs, which cannot turn a timeout into
# runner_wait.
#
# On runner_wait it writes runner_wait=true to GITHUB_OUTPUT. With
# DISPATCH_WATCH_STOP_RELEASE_GATE=true it cancels the candidates and stops
# the gate (dispatch_watch_stop_release_gate), which prints the
# RELEASE_GATE_RUNNER_WAIT lines the heal intake matches.
#
# Env: TEST_REPO, ALT_ISSUE_TITLE, ALT_CREATED_AFTER, ALT_WAIT_STARTED_AT
# (epoch the wait began), ALT_WAIT_BUDGET_SECS (default 4500),
# ALT_RUNNER_WAIT_MAX_RUNS (default 5), DISPATCH_WATCH_STOP_RELEASE_GATE
# (default false).
# API calls: 4 listings (in_progress, queued, pending, waiting), 1 jobs read
# per queued or in_progress candidate (at most ALT_RUNNER_WAIT_MAX_RUNS), plus
# the cancels on a stop.
# Log line (stable prefix):
#   ALT_MODEL_RUNNER_WAIT outcome=runner_wait|timeout reason=<r> runs=<ids|->
# Always returns 0: the caller fails the step as before.
ALT_RW_BUDGET="${ALT_WAIT_BUDGET_SECS:-4500}"
ALT_RW_MAX_RUNS="${ALT_RUNNER_WAIT_MAX_RUNS:-5}"

alt_rw_result()
{
	echo "ALT_MODEL_RUNNER_WAIT outcome=$1 reason=$2 runs=${3:--}"
}

# Prints the runs listing for one status, or fails.
alt_rw_list()
{
	timeout 30 gh api -X GET "repos/${TEST_REPO}/actions/runs" -f "status=$1" -f "created=>${ALT_CREATED_AFTER}" -f per_page=100 2>/dev/null
}

dispatch_watch_alt_model_runner_wait()
{
	[[ "${ALT_RW_BUDGET}" =~ ^[1-9][0-9]{0,5}$ ]] || ALT_RW_BUDGET=4500
	[[ "${ALT_RW_MAX_RUNS}" =~ ^[1-9][0-9]?$ ]] || ALT_RW_MAX_RUNS=5
	if ! [[ "${TEST_REPO:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [ -z "${ALT_ISSUE_TITLE:-}" ] \
		|| ! [[ "${ALT_CREATED_AFTER:-}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] \
		|| ! [[ "${ALT_WAIT_STARTED_AT:-}" =~ ^[0-9]+$ ]]; then
		alt_rw_result timeout missing_input
		return 0
	fi
	ALT_RW_TWO_THIRDS=$(( ALT_WAIT_STARTED_AT + ALT_RW_BUDGET * 2 / 3 ))

	# Candidate rows "<id> <created_epoch>" of one listing; fails on a bad page.
	alt_rw_candidates()
	{
		printf '%s' "$1" | jq -er --arg title "${ALT_ISSUE_TITLE}" '
			if (.workflow_runs | type) != "array" then error("no runs") else
			[.workflow_runs[] | select(type == "object")
				| select(((.display_title // "") | tostring) == $title)
				| select(((.status // "") | tostring) != "completed")
				| "\(.id) \((.created_at // "") | tostring | sub("\\.[0-9]+Z$"; "Z") | (try fromdateiso8601 catch 0))"]
			| join("\n") end' 2>/dev/null
	}

	if ! alt_rw_in_progress_json="$(alt_rw_list in_progress)" \
		|| ! alt_rw_in_progress_total="$(printf '%s' "${alt_rw_in_progress_json}" | jq -er '.total_count | numbers' 2>/dev/null)" \
		|| ! alt_rw_in_progress="$(alt_rw_candidates "${alt_rw_in_progress_json}")"; then
		alt_rw_result timeout listing_unreadable
		return 0
	fi
	if [ "${alt_rw_in_progress_total}" -gt 100 ]; then
		alt_rw_result timeout listing_truncated
		return 0
	fi
	if ! alt_rw_queued_json="$(alt_rw_list queued)" || ! alt_rw_queued="$(alt_rw_candidates "${alt_rw_queued_json}")"; then
		alt_rw_result timeout listing_unreadable
		return 0
	fi
	# A run of this issue held in `pending`/`waiting` (a concurrency group or
	# an environment) is not a runner wait: fail closed with reason=held, as
	# the watcher does.
	for alt_rw_held_status in pending waiting; do
		if ! alt_rw_held_json="$(alt_rw_list "${alt_rw_held_status}")" || ! alt_rw_held="$(alt_rw_candidates "${alt_rw_held_json}")"; then
			alt_rw_result timeout listing_unreadable
			return 0
		fi
		if [ -n "${alt_rw_held//[[:space:]]/}" ]; then
			alt_rw_result timeout held "$(printf '%s\n' "${alt_rw_held}" | awk '{print $1}' | paste -sd, -)"
			return 0
		fi
	done

	alt_rw_ids=""
	alt_rw_early=0
	alt_rw_in_progress_count=0
	alt_rw_read_count=0
	while read -r alt_rw_id alt_rw_created; do
		[[ "${alt_rw_id:-}" =~ ^[0-9]+$ ]] || continue
		alt_rw_read_count=$((alt_rw_read_count + 1))
		alt_rw_ids="${alt_rw_ids:+${alt_rw_ids},}${alt_rw_id}"
		if [ "${alt_rw_read_count}" -gt "${ALT_RW_MAX_RUNS}" ]; then
			alt_rw_result timeout too_many_runs "${alt_rw_ids}"
			return 0
		fi
		if [[ "${alt_rw_created:-}" =~ ^[1-9][0-9]*$ ]] && [ "${alt_rw_created}" -le "${ALT_RW_TWO_THIRDS}" ]; then
			alt_rw_early=1
		fi
		# A queued run has no job with a runner, but one of its jobs can be
		# held in waiting/pending (job-level concurrency or an environment):
		# that is not a runner wait. An unreadable jobs list fails closed.
		alt_rw_jobs="$(timeout 30 gh api "repos/${TEST_REPO}/actions/runs/${alt_rw_id}/jobs?per_page=100" 2>/dev/null || true)"
		alt_rw_verdict="$(printf '%s' "${alt_rw_jobs}" | jq -r '
			if (.jobs | type) != "array" then "unreadable"
			elif any(.jobs[] | select(type == "object"); ((.status // "") | tostring | IN("waiting", "pending")) and (((.runner_name // "") | tostring) == "")) then "held"
			else "wait" end' 2>/dev/null || echo unreadable)"
		if [ "${alt_rw_verdict}" != "wait" ]; then
			case "${alt_rw_verdict}" in held) ;; *) alt_rw_verdict="jobs_unreadable" ;; esac
			alt_rw_result timeout "${alt_rw_verdict}" "${alt_rw_ids}"
			return 0
		fi
	done <<< "${alt_rw_queued}"
	while read -r alt_rw_id alt_rw_created; do
		[[ "${alt_rw_id:-}" =~ ^[0-9]+$ ]] || continue
		alt_rw_in_progress_count=$((alt_rw_in_progress_count + 1))
		alt_rw_read_count=$((alt_rw_read_count + 1))
		alt_rw_ids="${alt_rw_ids:+${alt_rw_ids},}${alt_rw_id}"
		if [ "${alt_rw_read_count}" -gt "${ALT_RW_MAX_RUNS}" ]; then
			alt_rw_result timeout too_many_runs "${alt_rw_ids}"
			return 0
		fi
		if [[ "${alt_rw_created:-}" =~ ^[1-9][0-9]*$ ]] && [ "${alt_rw_created}" -le "${ALT_RW_TWO_THIRDS}" ]; then
			alt_rw_early=1
		fi
		alt_rw_jobs="$(timeout 30 gh api "repos/${TEST_REPO}/actions/runs/${alt_rw_id}/jobs?per_page=100" 2>/dev/null || true)"
		alt_rw_verdict="$(printf '%s' "${alt_rw_jobs}" | jq -r --argjson two_thirds "${ALT_RW_TWO_THIRDS}" '
			if (.jobs | type) != "array" or (.jobs | length) == 0 then "unreadable" else
			[.jobs[] | select(type == "object") | {
				queued_no_runner: (((.status // "") | tostring) == "queued" and (((.runner_name // "") | tostring) == "")),
				failed: ((((.conclusion // "") | tostring) | IN("failure", "timed_out")) or any(.steps[]?; ((.conclusion // "") | tostring) | IN("failure", "timed_out"))),
				start: ([.steps[]? | (.started_at // empty) | tostring | sub("\\.[0-9]+Z$"; "Z") | (try fromdateiso8601 catch empty)] | min)
			}]
			| if all(.[]; .queued_no_runner or (.start != null and .start >= $two_thirds and (.failed | not))) then "wait" else "ran_early" end
			end' 2>/dev/null || echo unreadable)"
		if [ "${alt_rw_verdict}" != "wait" ]; then
			case "${alt_rw_verdict}" in ran_early) ;; *) alt_rw_verdict="jobs_unreadable" ;; esac
			alt_rw_result timeout "${alt_rw_verdict}" "${alt_rw_ids}"
			return 0
		fi
	done <<< "${alt_rw_in_progress}"

	if [ -z "${alt_rw_ids}" ]; then
		alt_rw_result timeout no_runs_in_flight
		return 0
	fi
	if [ "${alt_rw_early}" != "1" ]; then
		alt_rw_result timeout queued_late "${alt_rw_ids}"
		return 0
	fi

	# never_assigned when no candidate got a runner, late_assigned otherwise.
	alt_rw_reason="never_assigned"
	[ "${alt_rw_in_progress_count}" -eq 0 ] || alt_rw_reason="late_assigned"
	alt_rw_result runner_wait "${alt_rw_reason}" "${alt_rw_ids}"
	echo "runner_wait=true" >> "${GITHUB_OUTPUT}"
	if [ "$(printf '%s' "${DISPATCH_WATCH_STOP_RELEASE_GATE:-false}" | tr '[:upper:]' '[:lower:]')" = "true" ]; then
		IFS=',' read -r -a alt_rw_all <<< "${alt_rw_ids}"
		for alt_rw_id in "${alt_rw_all[@]}"; do
			timeout 20 gh api -X POST "repos/${TEST_REPO}/actions/runs/${alt_rw_id}/cancel" >/dev/null 2>&1 \
				|| echo "::warning::Could not cancel alt-model run #${alt_rw_id} after the runner wait"
		done
		dispatch_watch_stop_release_gate "${alt_rw_ids%%,*}" alt-model "${alt_rw_reason}" || true
	fi
	return 0
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
STOP_RELEASE_GATE_MODE=0
ALT_MODEL_RUNNER_WAIT_MODE=0
WATCHED_RUN_ID=""
RUNNER_WAIT_REASON=""

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
		--stop-release-gate)
			STOP_RELEASE_GATE_MODE=1
			shift
			;;
		--alt-model-runner-wait)
			ALT_MODEL_RUNNER_WAIT_MODE=1
			shift
			;;
		--watched-run-id)
			require_option_argument "$@"
			WATCHED_RUN_ID="$2"
			shift 2
			;;
		--runner-wait-reason)
			require_option_argument "$@"
			RUNNER_WAIT_REASON="$2"
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

# --alt-model-runner-wait (issue #10037): inputs come from the environment
# (see dispatch_watch_alt_model_runner_wait). Always exits 0.
if [ "${ALT_MODEL_RUNNER_WAIT_MODE}" = "1" ]; then
	dispatch_watch_alt_model_runner_wait || true
	exit 0
fi

# --stop-release-gate (issue #10037): used by the gate's Phase 4 wait when its
# watched review run ended as runner_wait. Cancels the watched child run, then
# stops the gate run (see dispatch_watch_stop_release_gate). Exits 1 when the
# gate is still alive afterwards.
if [ "${STOP_RELEASE_GATE_MODE}" = "1" ]; then
	if ! [[ "${TARGET_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || ! [[ "${WATCHED_RUN_ID}" =~ ^[0-9]+$ ]]; then
		echo "::error::--stop-release-gate requires --repo <owner/repo> and a numeric --watched-run-id" >&2
		exit 2
	fi
	if timeout 20 gh api -X POST "repos/${TARGET_REPO}/actions/runs/${WATCHED_RUN_ID}/cancel" >/dev/null 2>&1; then
		echo "Cancel requested for watched run #${WATCHED_RUN_ID} (runner wait)"
	else
		echo "::warning::Could not cancel watched run #${WATCHED_RUN_ID} after the runner wait"
	fi
	dispatch_watch_stop_release_gate "${WATCHED_RUN_ID}" "${DISPLAY_NAME:-review}" "${RUNNER_WAIT_REASON:-never_assigned}" || true
	exit 1
fi

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

PRE_RUN_ID="0"
PRE_RUN_ID_LOOKUP_OK="false"
if PRE_RUN_ID_RAW="$(gh_api_safe_quiet_print "repos/${TARGET_REPO}/actions/workflows/${WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=1" --jq '.workflow_runs[0].id // 0')"; then
	PRE_RUN_ID="${PRE_RUN_ID_RAW}"
	PRE_RUN_ID_LOOKUP_OK="true"
fi
if [[ ! "${PRE_RUN_ID}" =~ ^[0-9]+$ ]]; then
	PRE_RUN_ID=0
	PRE_RUN_ID_LOOKUP_OK="false"
fi

REGISTRATION_WINDOW_START_EPOCH="$(date +%s)"
REGISTRATION_WINDOW_FALLBACK_EPOCH="$(( REGISTRATION_WINDOW_START_EPOCH - 1 ))"
REGISTRATION_WINDOW_FALLBACK_UTC="$(date -u -d "@${REGISTRATION_WINDOW_FALLBACK_EPOCH}" +%Y-%m-%dT%H:%M:%SZ)"

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
if [ "${PRE_RUN_ID_LOOKUP_OK}" = "true" ]; then
	REGISTRATION_QUERY="repos/${TARGET_REPO}/actions/workflows/${WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=10"
	REGISTRATION_JQ="[.workflow_runs[] | select(.id > ${PRE_RUN_ID})] | sort_by(.created_at) | last | .id // empty"
else
	REGISTRATION_QUERY="repos/${TARGET_REPO}/actions/workflows/${WORKFLOW_FILE}/runs?event=workflow_dispatch&created=>${REGISTRATION_WINDOW_FALLBACK_UTC}&per_page=10"
	REGISTRATION_JQ='[.workflow_runs[]] | sort_by(.created_at) | last | .id // empty'
fi
while [ "$(date +%s)" -lt "${REGISTRATION_DEADLINE}" ]; do
	NEW_ID="$(gh_api_safe_quiet_print_read "${REGISTRATION_QUERY}" --jq "${REGISTRATION_JQ}" || echo "")"
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

# Written as soon as the run registers, so a caller's `if: always()` cleanup
# can cancel a run this script stopped watching (completion timeout). `run_id`
# keeps its meaning (written only once the run completes, or in snapshot mode),
# so steps gated on it are unchanged. The release gate's orchestrate-decompose
# cleanup reads this output: a bootstrap left queued past the timeout created
# smoke projects after the gate finished (gates 43445515854, 43590943288,
# 43708007145).
echo "dispatched_run_id=${NEW_ID}" >> "${GITHUB_OUTPUT}"

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
# Status-read counters for the completion-timeout diagnosis below (#9706).
WATCH_STATUS_READS=0
WATCH_QUEUED_READS=0
WATCH_SAW_IN_PROGRESS=0
WATCH_DIAG_QUEUED_WHOLE_TIME=0
while [ "$(date +%s)" -lt "${COMPLETION_DEADLINE}" ]; do
	RUN_JSON="$(gh_api_safe_quiet_print_read "repos/${TARGET_REPO}/actions/runs/${NEW_ID}" --jq '{status, conclusion}' || echo "")"
	if [ -n "${RUN_JSON}" ]; then
		STATUS="$(printf '%s' "${RUN_JSON}" | jq -r '.status // ""' 2>/dev/null || echo "")"
		CONCLUSION="$(printf '%s' "${RUN_JSON}" | jq -r '.conclusion // ""' 2>/dev/null || echo "")"
	else
		STATUS=""
		CONCLUSION=""
	fi
	if [ -n "${STATUS}" ]; then
		WATCH_STATUS_READS=$((WATCH_STATUS_READS + 1))
		if [ "${STATUS}" = "queued" ]; then
			WATCH_QUEUED_READS=$((WATCH_QUEUED_READS + 1))
		elif [ "${STATUS}" = "in_progress" ]; then
			WATCH_SAW_IN_PROGRESS=1
		fi
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

# Completion watch timed out (#9706, follow-up to #9532). In release run
# 43590943288 dispatched children stayed queued for hours with no runner; the
# watcher gave up but nothing cancelled them, so an orphaned child later
# created issues the gate's cleanup could not see. Say whether the run never
# got a runner or was just slow, then cancel it (unless
# DISPATCH_WATCH_CANCEL_ON_TIMEOUT=false). Do not lengthen the timeouts as a
# fix. The `::error::... timed out` line below must stay the last line printed:
# the workflow steps' timeout block extracts the run id from it.
if [ "${WATCH_SAW_IN_PROGRESS}" = "1" ]; then
	echo "::warning::${DISPLAY_NAME} run #${NEW_ID} watch diagnosis: status=in_progress (a runner was assigned; the run was slow); status_reads=${WATCH_STATUS_READS}"
# A trailing failed read leaves STATUS empty: the watcher then cannot claim
# "the whole time" and falls through to the could-not-tell line.
elif [ "${WATCH_STATUS_READS}" -gt 0 ] && [ "${WATCH_QUEUED_READS}" -eq "${WATCH_STATUS_READS}" ] && [ "${STATUS}" = "queued" ]; then
	WATCH_DIAG_QUEUED_WHOLE_TIME=1
	echo "::warning::${DISPLAY_NAME} run #${NEW_ID} watch diagnosis: status=queued the whole time (no runner was assigned); status_reads=${WATCH_STATUS_READS}"
else
	echo "::warning::${DISPLAY_NAME} run #${NEW_ID} watch diagnosis: last status=${STATUS:-unknown} (could not tell queued from in_progress); status_reads=${WATCH_STATUS_READS}"
fi

# Runner-wait classification (issue #10037, G3). One jobs read of the watched
# run. The result is runner_wait only when every job either still waits for a
# runner (status=queued, no runner_name; a job waiting on an environment or
# concurrency group is not a runner wait) or started its first step
# in the last third of the completion budget; an empty jobs list counts only
# when the run was queued the whole time. A job with a runner but no readable
# step start counts as early, a job that already failed never counts as late,
# and a failed read gives timeout: all fail
# closed, so a child that ran early and then failed or overran still fails
# the gate. Printed as DISPATCH_WATCH_RESULT and written as watch_result=.
WATCH_RESULT="timeout"
WATCH_RESULT_REASON="jobs_unreadable"
WATCH_JOBS_COUNT="-"
WATCH_JOBS_QUEUED_NO_RUNNER="-"
WATCH_TWO_THIRDS_EPOCH=$(( DISPATCH_STARTED_AT + COMPLETION_TIMEOUT_SECS * 2 / 3 ))
WATCH_JOBS_SUMMARY=""
if WATCH_JOBS_JSON="$(gh_api_safe_quiet_print "repos/${TARGET_REPO}/actions/runs/${NEW_ID}/jobs?per_page=100")"; then
	WATCH_JOBS_SUMMARY="$(printf '%s' "${WATCH_JOBS_JSON}" | jq -r --argjson two_thirds "${WATCH_TWO_THIRDS_EPOCH}" '
		if (.jobs | type) != "array" then empty else
		[.jobs[] | select(type == "object") | {
			queued_no_runner: (((.status // "") | tostring) == "queued" and (((.runner_name // "") | tostring) == "")),
			has_runner: (((.runner_name // "") | tostring) != ""),
			held_no_runner: (((.status // "") | tostring | IN("waiting", "pending")) and (((.runner_name // "") | tostring) == "")),
			failed: ((((.conclusion // "") | tostring) | IN("failure", "timed_out")) or any(.steps[]?; ((.conclusion // "") | tostring) | IN("failure", "timed_out"))),
			start: ([.steps[]? | (.started_at // empty) | tostring | sub("\\.[0-9]+Z$"; "Z") | (try fromdateiso8601 catch empty)] | min)
		}] as $js
		| [($js | length),
		   ([$js[] | select(.queued_no_runner)] | length),
		   (if ($js | length) > 0 and all($js[]; .queued_no_runner or (.start != null and .start >= $two_thirds and (.failed | not))) then 1 else 0 end),
		   ([$js[] | select(.has_runner or .start != null)] | length),
		   ([$js[] | select(.held_no_runner)] | length)]
		| map(tostring) | join(" ")
		end' 2>/dev/null || true)"
fi
if [[ "${WATCH_JOBS_SUMMARY}" =~ ^([0-9]+)\ ([0-9]+)\ ([01])\ ([0-9]+)\ ([0-9]+)$ ]]; then
	WATCH_JOBS_COUNT="${BASH_REMATCH[1]}"
	WATCH_JOBS_QUEUED_NO_RUNNER="${BASH_REMATCH[2]}"
	if [ "${WATCH_JOBS_COUNT}" -eq 0 ]; then
		if [ "${WATCH_DIAG_QUEUED_WHOLE_TIME}" = "1" ]; then
			WATCH_RESULT="runner_wait"
			WATCH_RESULT_REASON="never_assigned"
		else
			WATCH_RESULT_REASON="no_jobs"
		fi
	elif [ "${BASH_REMATCH[3]}" = "1" ]; then
		WATCH_RESULT="runner_wait"
		if [ "${BASH_REMATCH[4]}" -eq 0 ]; then
			WATCH_RESULT_REASON="never_assigned"
		else
			WATCH_RESULT_REASON="late_assigned"
		fi
	elif [ "${BASH_REMATCH[4]}" -eq 0 ] && [ "${BASH_REMATCH[5]}" -gt 0 ]; then
		# No job got a runner, but not every job is status=queued: held by an
		# environment or a concurrency group, not a runner wait.
		WATCH_RESULT_REASON="held"
	else
		WATCH_RESULT_REASON="ran_early"
	fi
fi
echo "DISPATCH_WATCH_RESULT outcome=${WATCH_RESULT} run=${NEW_ID} reason=${WATCH_RESULT_REASON} jobs=${WATCH_JOBS_COUNT} queued_without_runner=${WATCH_JOBS_QUEUED_NO_RUNNER}"
echo "watch_result=${WATCH_RESULT}" >> "${GITHUB_OUTPUT}"

WATCH_CANCEL_SWITCH="$(printf '%s' "${DISPATCH_WATCH_CANCEL_ON_TIMEOUT:-true}" | tr '[:upper:]' '[:lower:]')"
case "${WATCH_CANCEL_SWITCH}" in
	true)
		if [[ "${NEW_ID}" =~ ^[0-9]+$ ]]; then
			# Bounded like the workflow steps' twin cancel (`timeout 20 gh api`):
			# gh_api_safe retries rate-limit errors on writes for up to ~210 s,
			# which could outlast the step's remaining time and lose the
			# `::error::... timed out` line the workflow cleanup parses.
			if timeout 20 gh api -X POST "repos/${TARGET_REPO}/actions/runs/${NEW_ID}/cancel" >/dev/null 2>&1; then
				echo "Cancel requested for watched run #${NEW_ID} (completion watch timeout)"
			else
				# $? here is the cancel's status: 124 means the 20 s limit hit.
				WATCH_CANCEL_RC=$?
				echo "::warning::Could not cancel watched run #${NEW_ID} after the completion watch timeout (exit ${WATCH_CANCEL_RC})"
			fi
		else
			# The id is not printed here because it is not numeric.
			echo "::warning::Cancel of watched run skipped after the completion watch timeout: run id is not numeric"
		fi
		;;
	false)
		echo "Cancel of watched run #${NEW_ID} skipped (DISPATCH_WATCH_CANCEL_ON_TIMEOUT=false)"
		;;
	*)
		# The value itself is never printed.
		echo "::warning::DISPATCH_WATCH_CANCEL_ON_TIMEOUT is neither true nor false; treating it as false"
		echo "Cancel of watched run #${NEW_ID} skipped (DISPATCH_WATCH_CANCEL_ON_TIMEOUT unrecognised)"
		;;
esac

# Stop the release gate on a runner wait (issue #10037). Off unless the gate
# sets DISPATCH_WATCH_STOP_RELEASE_GATE=true, so other callers are unchanged.
# When the stop lands, the runner kills this step (the job ends cancelled);
# otherwise the step fails below as before.
if [ "${WATCH_RESULT}" = "runner_wait" ] && [ "$(printf '%s' "${DISPATCH_WATCH_STOP_RELEASE_GATE:-false}" | tr '[:upper:]' '[:lower:]')" = "true" ]; then
	dispatch_watch_stop_release_gate "${NEW_ID}" "${DISPLAY_NAME}" "${WATCH_RESULT_REASON}" || true
fi

echo "::error::${DISPLAY_NAME} run #${NEW_ID} timed out"
exit 1
