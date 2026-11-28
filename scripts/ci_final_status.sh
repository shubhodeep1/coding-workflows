#!/usr/bin/env bash
# Final CI result as a `ci/final` commit status (issue #9939).
#
# The ruleset on the default branch requires a check, and the only full CI on
# a pipeline PR head is the dispatched final CI (ci.yml `workflow_dispatch`,
# "CI [pr:N]"). GitHub evaluates check-runs for a PR only when the run's event
# is push, pull_request, pull_request_target, pull_request_review, deployment
# or deployment_status, so that run's `lint` check-run read as "expected"
# forever and every merge landed by an admin bypass. A commit status on the
# tested head counts whatever event produced it, so the `lint` job posts one
# here and the ruleset requires `ci/final` (owner step).
#
# Called only from the last step of ci.yml's `lint` job (`if: always() &&
# !cancelled()`, continue-on-error). Quick mode never runs that job, so it
# never posts. Push runs on main, stable and orchestrator/project-* post too
# (final integration PRs whose head only ever got a push run need it).
#
# Inputs (env, every one has a default):
#   GH_TOKEN                             the job token (statuses: write).
#   GITHUB_REPOSITORY                    owner/repo.
#   CI_FINAL_STATUS_SHA                  the tested head (default GITHUB_SHA).
#   CI_FINAL_STATUS_AGGREGATE_OUTCOME    outcome of "Require every CI job to
#                                        succeed" (success / failure /
#                                        cancelled / skipped / empty).
#   CI_FINAL_STATUS_DOCS_ONLY_REQUESTED  "true" in a docs-only final CI.
#   CI_FINAL_STATUS_DOCS_ONLY_OUTCOME    outcome of the docs-only verify step.
#   CI_FINAL_STATUS_RUN_KIND             "delta" or "full" on main push and
#                                        scheduled runs (issue #10156); adds
#                                        " (delta suite)" / " (full suite)" to
#                                        the description. Any other value,
#                                        including empty (every other run),
#                                        leaves the description unchanged.
#   GITHUB_SERVER_URL, GITHUB_RUN_ID     build the status target_url.
#   CI_FINAL_STATUS_NEEDS_JSON           `toJSON(needs)` of `lint` (default
#                                        empty; only `.result` is read).
#   CI_FINAL_STATUS_RUN_CANCELLED_SKIP_ENABLED  default true; only `true`
#                                        turns the run-cancelled skip on
#                                        (issue #10131).
#   GITHUB_RUN_ATTEMPT                   the run attempt (GitHub sets it);
#                                        the jobs of that attempt are read.
#
# State: success only when the aggregate succeeded and, in a docs-only run,
# the docs-only verify step succeeded too; a cancelled aggregate posts
# nothing; everything else (failure, skipped because an earlier step failed,
# empty) posts failure. A sync-quick run uses the same mapping: the
# aggregate already decides whether its skipped jobs count.
# Run-cancelled skip (issue #10131): when the run is cancelled before `lint`
# starts, `lint` still runs (`always()`), its aggregate fails on the
# cancelled needed jobs and `cancelled()` is false inside the job. So before
# posting failure, when the switch is on, the needs include a `cancelled`
# job and no `failure` job, ask `ci_cancelled_rerun.py
# classify-cancelled-run` about this run attempt. Only `run_cancelled` (a
# cancelled job never started, or every cancelled job carries only
# shutdown/cancel annotations, a hand cancel's "The run was canceled by @..."
# or a concurrency replacement's "Canceling since a higher priority ...")
# skips the post; a job cut off by its own timeout-minutes, any error, a
# classifier timeout and any unclear answer post failure as before.
#
# API calls (§15): one POST repos/<repo>/statuses/<sha> through gh_retry,
# marked idempotent so transient failures are retried (the newest status for
# a context wins); none when cancelled or the input is invalid. On the
# cancelled-and-no-failure path only (state failure, switch on), one jobs
# GET for this run attempt (a second one only when the first read fails)
# plus at most 10 check-run annotation GETs, none of those annotation GETs
# when a cancelled job never started.
#
# Always exits 0 (fail-open for the job, fail-closed for merging): a failed
# post logs `CI_FINAL_STATUS ... outcome=post_failed` and a warning, and the
# missing status keeps the PR blocked. Caveat: on a re-run for a SHA that
# already carries `ci/final=success`, a failed post leaves that older status
# in place, so a failed `failure` post is an ::error:: line; the red `lint`
# still sends the PR back through `final-ci-failed`. The token and the API
# response are never printed.
#
# Log lines (stable prefix CI_FINAL_STATUS):
#   CI_FINAL_STATUS sha=<sha> state=<state> outcome=posted run=<run_id>
#   CI_FINAL_STATUS sha=<sha> state=<state> outcome=post_failed
#   CI_FINAL_STATUS sha=<sha> outcome=skipped reason=cancelled
#   CI_FINAL_STATUS sha=<sha|none> outcome=skipped reason=invalid_input
#   CI_FINAL_STATUS sha=<sha> outcome=skipped reason=run_cancelled
#   CI_FINAL_STATUS sha=<sha> check=run_cancelled verdict=run_cancelled|not_cancelled|unverified|none
#   CI_FINAL_STATUS sha=<sha> check=run_cancelled verdict=none reason=timeout

set -uo pipefail

# shellcheck source=gh_helpers.sh
source "$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh" >/dev/null 2>&1 || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

CI_FINAL_STATUS_CONTEXT="ci/final"

# Return 0 only when this run attempt was cancelled as a whole
# (issue #10131); 1 otherwise, so the caller posts failure as before.
# Args: <repo> <sha> (both already validated by the caller).
ci_final_status_run_was_cancelled()
{
	local repo="$1" sha="$2"
	local enabled="${CI_FINAL_STATUS_RUN_CANCELLED_SKIP_ENABLED:-true}"
	local needs_json="${CI_FINAL_STATUS_NEEDS_JSON:-}"
	local attempt="${GITHUB_RUN_ATTEMPT:-}"
	local cancel_run_id="${GITHUB_RUN_ID:-}"
	local needs_shape="" verdict="" classifier_rc=0

	[ "${enabled}" = "true" ] || return 1
	[ -n "${needs_json}" ] || return 1
	needs_shape="$(printf '%s' "${needs_json}" | jq -r '
		if type == "object" and length > 0 then
			[.[] | (.result? // "")] as $results
			| if any($results[]; . == "failure") then "has_failure"
			elif any($results[]; . == "cancelled") then "cancelled"
			else "no_cancelled" end
		else "invalid" end' 2>/dev/null)" || return 1
	[ "${needs_shape}" = "cancelled" ] || return 1

	if ! [[ "${cancel_run_id}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${attempt}" =~ ^[1-9][0-9]{0,3}$ ]]; then
		echo "CI_FINAL_STATUS sha=${sha} check=run_cancelled verdict=unverified"
		return 1
	fi

	# `lint` is running, `final-ci-failed` waits on it and `budget-watch`
	# runs beside it on push to main; none is in `lint.needs`.
	verdict="$(PYTHONDONTWRITEBYTECODE=1 timeout 90 python3 "$(dirname "${BASH_SOURCE[0]}")/ci_cancelled_rerun.py" classify-cancelled-run \
		--repo "${repo}" --run-id "${cancel_run_id}" --attempt "${attempt}" \
		--ignore-job lint --ignore-job final-ci-failed --ignore-job budget-watch 2>/dev/null)" || classifier_rc=$?
	verdict="$(printf '%s\n' "${verdict}" | tail -n 1)"
	case "${verdict}" in
		run_cancelled|not_cancelled|unverified) ;;
		*) verdict="none" ;;
	esac
	if [ "${classifier_rc}" -eq 124 ]; then
		echo "CI_FINAL_STATUS sha=${sha} check=run_cancelled verdict=none reason=timeout"
		return 1
	fi
	echo "CI_FINAL_STATUS sha=${sha} check=run_cancelled verdict=${verdict}"
	[ "${verdict}" = "run_cancelled" ]
}

ci_final_status_main()
{
	local sha="${CI_FINAL_STATUS_SHA:-${GITHUB_SHA:-}}"
	local repo="${GITHUB_REPOSITORY:-}"
	local aggregate="${CI_FINAL_STATUS_AGGREGATE_OUTCOME:-}"
	local docs_only_requested="${CI_FINAL_STATUS_DOCS_ONLY_REQUESTED:-false}"
	local docs_only_outcome="${CI_FINAL_STATUS_DOCS_ONLY_OUTCOME:-}"
	local run_kind="${CI_FINAL_STATUS_RUN_KIND:-}"
	local server="${GITHUB_SERVER_URL:-https://github.com}"
	local run_id="${GITHUB_RUN_ID:-}"
	local state="" description="" target_url=""

	if ! [[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || ! [[ "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
		[[ "${sha}" =~ ^[0-9a-f]{40}$ ]] || sha="none"
		echo "CI_FINAL_STATUS sha=${sha} outcome=skipped reason=invalid_input"
		echo "::warning::Final CI status not posted: the head SHA or repository is invalid."
		return 0
	fi

	if [ "${aggregate}" = "cancelled" ]; then
		echo "CI_FINAL_STATUS sha=${sha} outcome=skipped reason=cancelled"
		return 0
	fi

	state="failure"
	if [ "${aggregate}" = "success" ]; then
		if [ "${docs_only_requested}" != "true" ] || [ "${docs_only_outcome}" = "success" ]; then
			state="success"
		fi
	fi
	if [ "${state}" = "success" ]; then
		description="Final CI passed"
	else
		description="Final CI failed"
		if ci_final_status_run_was_cancelled "${repo}" "${sha}"; then
			echo "CI_FINAL_STATUS sha=${sha} outcome=skipped reason=run_cancelled"
			return 0
		fi
	fi
	case "${run_kind}" in
		delta) description="${description} (delta suite)" ;;
		full) description="${description} (full suite)" ;;
	esac

	[[ "${server}" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || server="https://github.com"
	if [[ "${run_id}" =~ ^[0-9]+$ ]]; then
		target_url="${server}/${repo}/actions/runs/${run_id}"
	else
		run_id="unknown"
		target_url="${server}/${repo}/commit/${sha}"
	fi

	# A status POST for the same context and SHA replaces the earlier one,
	# so it is safe to retry (GH_RETRY_IDEMPOTENT; the env form keeps the
	# fallback gh_retry stub above working).
	if GH_RETRY_IDEMPOTENT=true gh_retry gh api --method POST "repos/${repo}/statuses/${sha}" \
		-f "state=${state}" \
		-f "context=${CI_FINAL_STATUS_CONTEXT}" \
		-f "target_url=${target_url}" \
		-f "description=${description}" >/dev/null 2>&1; then
		echo "CI_FINAL_STATUS sha=${sha} state=${state} outcome=posted run=${run_id}"
	else
		echo "CI_FINAL_STATUS sha=${sha} state=${state} outcome=post_failed"
		if [ "${state}" = "failure" ]; then
			echo "::error::Could not post ${CI_FINAL_STATUS_CONTEXT}=failure on ${sha}; an earlier ${CI_FINAL_STATUS_CONTEXT}=success on this SHA, if any, still stands."
		fi
		echo "::warning::Could not post the ${CI_FINAL_STATUS_CONTEXT} commit status on ${sha}; the PR stays blocked until a re-run of the final CI posts it."
	fi
	return 0
}

ci_final_status_main
exit 0
