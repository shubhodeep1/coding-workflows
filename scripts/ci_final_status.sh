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
#   GITHUB_SERVER_URL, GITHUB_RUN_ID     build the status target_url.
#
# State: success only when the aggregate succeeded and, in a docs-only run,
# the docs-only verify step succeeded too; a cancelled aggregate posts
# nothing; everything else (failure, skipped because an earlier step failed,
# empty) posts failure. A sync-quick run uses the same mapping: the
# aggregate already decides whether its skipped jobs count.
#
# API calls (§15): exactly one POST repos/<repo>/statuses/<sha> (through
# gh_retry), none when cancelled or the input is invalid.
#
# Always exits 0 (fail-open for the job, fail-closed for merging): a failed
# post logs `CI_FINAL_STATUS ... outcome=post_failed` and a warning, and the
# missing status keeps the PR blocked; it is never a false green. The token
# and the API response are never printed.
#
# Log lines (stable prefix CI_FINAL_STATUS):
#   CI_FINAL_STATUS sha=<sha> state=<state> outcome=posted run=<run_id>
#   CI_FINAL_STATUS sha=<sha> state=<state> outcome=post_failed
#   CI_FINAL_STATUS sha=<sha> outcome=skipped reason=cancelled
#   CI_FINAL_STATUS sha=<sha|none> outcome=skipped reason=invalid_input

set -uo pipefail

# shellcheck source=gh_helpers.sh
source "$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh" >/dev/null 2>&1 || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

CI_FINAL_STATUS_CONTEXT="ci/final"

ci_final_status_main()
{
	local sha="${CI_FINAL_STATUS_SHA:-${GITHUB_SHA:-}}"
	local repo="${GITHUB_REPOSITORY:-}"
	local aggregate="${CI_FINAL_STATUS_AGGREGATE_OUTCOME:-}"
	local docs_only_requested="${CI_FINAL_STATUS_DOCS_ONLY_REQUESTED:-false}"
	local docs_only_outcome="${CI_FINAL_STATUS_DOCS_ONLY_OUTCOME:-}"
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
	fi

	[[ "${server}" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || server="https://github.com"
	if [[ "${run_id}" =~ ^[0-9]+$ ]]; then
		target_url="${server}/${repo}/actions/runs/${run_id}"
	else
		run_id="unknown"
		target_url="${server}/${repo}/commit/${sha}"
	fi

	if gh_retry gh api --method POST "repos/${repo}/statuses/${sha}" \
		-f "state=${state}" \
		-f "context=${CI_FINAL_STATUS_CONTEXT}" \
		-f "target_url=${target_url}" \
		-f "description=${description}" >/dev/null 2>&1; then
		echo "CI_FINAL_STATUS sha=${sha} state=${state} outcome=posted run=${run_id}"
	else
		echo "CI_FINAL_STATUS sha=${sha} state=${state} outcome=post_failed"
		echo "::warning::Could not post the ${CI_FINAL_STATUS_CONTEXT} commit status on ${sha}; the PR stays blocked until a re-run of the final CI posts it."
	fi
	return 0
}

ci_final_status_main
exit 0
