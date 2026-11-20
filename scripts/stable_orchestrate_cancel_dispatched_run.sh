#!/usr/bin/env bash
# shellcheck shell=bash
# Called by the "Cleanup orchestrate test issues" step of the
# orchestrate-decompose-test job in .github/workflows/test-and-mark-stable.yml.
# Kept here so the workflow stays well under the 480,000-byte guard
# (CLAUDE.md §27, tests/test_workflow_file_size_limit.py).
#
# Why: when the Actions queue is long, the orchestrate bootstrap the job
# dispatched can still be queued when scripts/dispatch_and_watch_workflow_run.sh
# stops watching it. The cleanup then finds no tracking issue, and the run
# starts later and creates a smoke project no gate ever cleans up (gates
# 43445515854, 43590943288, 43708007145 left projects #9490, #9523, #9550).
#
# What it does:
#   1. Cancels DISPATCHED_RUN_ID when it is not completed yet, waits for it to
#      finish, and force-cancels it once if a plain cancel does not land.
#   2. When TRACKING_NUMBER is empty, looks for an open
#      `ai:orchestrator-tracking` issue whose body carries RUN_ID (the gate's
#      run id, embedded in the project description), in case the run created
#      the project before the cancel landed. Prints `tracking_issue=<N>` on
#      stdout when it finds one, then `child_issues=<a,b>` for the open
#      `ai:orchestrator-managed` issues whose body names it; everything else
#      goes to stderr.
#
# Env: TEST_REPO (owner/repo), RUN_ID (gate run id), DISPATCHED_RUN_ID
# (optional), TRACKING_NUMBER (optional). Never fails the step: every API
# error is logged and the script exits 0, so the caller's cleanup carries on.
#
# Log line (stable prefix, agents.md "Stable log prefixes (contractual)"):
#   ORCHESTRATE_SMOKE_DISPATCH_CANCEL run=<id> outcome=<...>[ tracking_issue=<N>]
set -uo pipefail

SMOKE_CANCEL_WAIT_SECS="${SMOKE_CANCEL_WAIT_SECS:-120}"
SMOKE_CANCEL_POLL_SECS="${SMOKE_CANCEL_POLL_SECS:-10}"
SMOKE_CANCEL_FORCE_WAIT_SECS="${SMOKE_CANCEL_FORCE_WAIT_SECS:-60}"

_smoke_cancel_log()
{
	echo "ORCHESTRATE_SMOKE_DISPATCH_CANCEL $*" >&2
}

# Print the status of run $1, or nothing after 3 failed reads.
_smoke_cancel_status()
{
	local attempt status
	for attempt in 1 2 3; do
		if status="$(gh api "repos/${TEST_REPO}/actions/runs/${1}" --jq '.status // ""' 2>/dev/null)" && [ -n "${status}" ]; then
			printf '%s' "${status}"
			return 0
		fi
		[ "${attempt}" -lt 3 ] && sleep "${SMOKE_CANCEL_POLL_SECS}"
	done
	return 0
}

# Wait until run $1 is completed or $2 seconds pass. Prints the last status.
_smoke_cancel_wait_completed()
{
	local run_id="$1"
	local wait_secs="$2"
	local deadline=$(( $(date +%s) + wait_secs ))
	local status=""
	while :; do
		status="$(_smoke_cancel_status "${run_id}")"
		if [ "${status}" = "completed" ] || [ "$(date +%s)" -ge "${deadline}" ]; then
			break
		fi
		sleep "${SMOKE_CANCEL_POLL_SECS}"
	done
	printf '%s' "${status}"
}

smoke_cancel_dispatched_run()
{
	local run_id="${DISPATCHED_RUN_ID:-}"
	if ! [[ "${run_id}" =~ ^[1-9][0-9]*$ ]]; then
		_smoke_cancel_log "run=none outcome=skip reason=no_dispatched_run"
		return 0
	fi
	local status
	status="$(_smoke_cancel_status "${run_id}")"
	if [ -z "${status}" ]; then
		_smoke_cancel_log "run=${run_id} outcome=lookup_failed"
		echo "::warning::Could not read orchestrate run ${run_id}; it was not cancelled. The poller's smoke-fixture backstop closes any project it creates once this gate completes." >&2
		return 0
	fi
	if [ "${status}" = "completed" ]; then
		_smoke_cancel_log "run=${run_id} outcome=already_completed"
		return 0
	fi
	if ! gh api -X POST "repos/${TEST_REPO}/actions/runs/${run_id}/cancel" >/dev/null 2>&1; then
		# A run that completed between the read and the cancel answers 409.
		status="$(_smoke_cancel_status "${run_id}")"
		if [ "${status}" = "completed" ]; then
			_smoke_cancel_log "run=${run_id} outcome=already_completed"
			return 0
		fi
		_smoke_cancel_log "run=${run_id} outcome=cancel_failed status=${status:-unknown}"
		echo "::warning::Could not cancel orchestrate run ${run_id} (status=${status:-unknown}). The poller's smoke-fixture backstop closes any project it creates once this gate completes." >&2
		return 0
	fi
	status="$(_smoke_cancel_wait_completed "${run_id}" "${SMOKE_CANCEL_WAIT_SECS}")"
	if [ "${status}" = "completed" ]; then
		_smoke_cancel_log "run=${run_id} outcome=cancelled previous_status=queued_or_running"
		return 0
	fi
	gh api -X POST "repos/${TEST_REPO}/actions/runs/${run_id}/force-cancel" >/dev/null 2>&1 || true
	status="$(_smoke_cancel_wait_completed "${run_id}" "${SMOKE_CANCEL_FORCE_WAIT_SECS}")"
	if [ "${status}" = "completed" ]; then
		_smoke_cancel_log "run=${run_id} outcome=force_cancelled"
		return 0
	fi
	_smoke_cancel_log "run=${run_id} outcome=cancel_requested_not_completed status=${status:-unknown}"
	echo "::warning::Orchestrate run ${run_id} had not stopped ${SMOKE_CANCEL_WAIT_SECS}s+${SMOKE_CANCEL_FORCE_WAIT_SECS}s after cancel. The poller's smoke-fixture backstop closes any project it creates once this gate completes." >&2
	return 0
}

smoke_cancel_discover_tracking()
{
	[ -z "${TRACKING_NUMBER:-}" ] || return 0
	[[ "${RUN_ID:-}" =~ ^[1-9][0-9]*$ ]] || return 0
	local found
	found="$(gh api "repos/${TEST_REPO}/issues?state=open&labels=ai:orchestrator-tracking&per_page=100" \
		--jq "[.[] | select(.pull_request | not) | select((.body // \"\") | contains(\"${RUN_ID}\"))] | sort_by(.created_at) | last | .number // empty" \
		2>/dev/null || echo "")"
	if [[ "${found}" =~ ^[1-9][0-9]*$ ]]; then
		_smoke_cancel_log "run=${DISPATCHED_RUN_ID:-none} outcome=tracking_found tracking_issue=${found}"
		echo "tracking_issue=${found}"
		# Children carry `Tracking issue: #<N>` in their body; their titles are
		# model-written and may drop the run marker the cleanup's title search uses.
		local children
		children="$(gh api --paginate "repos/${TEST_REPO}/issues?state=open&labels=ai:orchestrator-managed&per_page=100" \
			--jq ".[] | select(.pull_request | not) | select((.body // \"\") | test(\"Tracking issue: #${found}([^0-9]|\$)\")) | .number" \
			2>/dev/null | grep -E '^[1-9][0-9]*$' | sort -un | paste -sd, - || echo "")"
		if [ -n "${children}" ]; then
			echo "child_issues=${children}"
		fi
	fi
	return 0
}

smoke_cancel_dispatched_run
smoke_cancel_discover_tracking
exit 0
