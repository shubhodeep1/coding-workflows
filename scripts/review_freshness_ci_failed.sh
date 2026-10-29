#!/usr/bin/env bash
# Send a freshness-update head whose CI failed back to review/autofix (Q103).
#
# A head that is only GitHub's merge of an approved head with its base skips
# the reviewers (scripts/review_head_gate.sh freshness-sync, #7084). Its
# review run is the deterministic-skip-merge job, which waits for CI and
# merges. When that CI failed, the job refused the merge and nothing else ran
# review/autofix on the head again: PR #7153 sat with red CI from 2026-10-11
# while its orchestrator project waited on it.
#
# This posts one marker comment bound to the head,
#   <!-- ai:freshness-ci-failed:v1 pr=<n> head=<40-hex sha> -->
# which makes review_head_gate.sh freshness-sync review that head normally,
# and then dispatches the PR's review workflow so the normal review and
# autofix run (CI failure context included) happens now instead of never.
#
# Env: REPOSITORY, PR_NUMBER, PR_HEAD_SHA (the gate-observed head),
# REVIEW_AUTOFIX_CALLER_WORKFLOW_REF (github.workflow_ref of the caller),
# ALLOW_WORKFLOW_EDITS, FRESHNESS_CI_FAILED_REDISPATCH_ENABLED (unset, empty
# or `true` = on; `false` = off; anything else = off with a warning).
# FRESHNESS_CI_FAILED_RETRY_SLEEP_SECS (default 5): wait between marker POST
# attempts.
# API budget (§15): one comment POST (up to 3 attempts on failure) and one
# workflow dispatch (a failed dispatch tries the next review workflow name).
# Never fails the job.
# Logs: FRESHNESS_CI_FAILED pr=<n> head=<sha> outcome=redispatched|disabled|invalid_input|marker_failed|dispatch_failed [workflow=<file>]

set -uo pipefail

FRESHNESS_CI_FAILED_MARKER_PREFIX="<!-- ai:freshness-ci-failed:v1"

freshness_ci_failed_log()
{
	echo "FRESHNESS_CI_FAILED pr=${PR_NUMBER:-?} head=${PR_HEAD_SHA:-?} outcome=$1${2:+ $2}"
}

# Dispatch the PR's review workflow once, trying each candidate name in turn.
# Env: REPOSITORY, PR_NUMBER, REVIEW_AUTOFIX_CALLER_WORKFLOW_REF,
# ALLOW_WORKFLOW_EDITS. Prints the dispatched workflow file and returns 0, or
# returns 1 when every candidate failed. Also used by
# review_autofix_step_deterministic_skip_security_pass.sh (#8333).
freshness_ci_failed_dispatch_review_workflow()
{
	local candidate caller_workflow allow_edits
	local -a candidates=()

	allow_edits=false
	[ "${ALLOW_WORKFLOW_EDITS:-}" = "true" ] && allow_edits=true
	caller_workflow=""
	if [ -n "${REVIEW_AUTOFIX_CALLER_WORKFLOW_REF:-}" ]; then
		caller_workflow="$(basename "${REVIEW_AUTOFIX_CALLER_WORKFLOW_REF%%@*}")"
	fi
	if ! [[ "${caller_workflow}" =~ ^[A-Za-z0-9._-]+\.ya?ml$ ]]; then
		caller_workflow="internal-review.yml"
	fi
	# Same order as review_autofix_step_post_commit_retrigger.sh: the
	# PR-named review wrappers first, then a differently named caller, then
	# review_autofix.yml.
	case "${caller_workflow}" in
		ai-review.yml|internal-review.yml) candidates+=("${caller_workflow}") ;;
	esac
	for candidate in ai-review.yml internal-review.yml; do
		[ "${candidate}" != "${caller_workflow}" ] && candidates+=("${candidate}")
	done
	case "${caller_workflow}" in
		ai-review.yml|internal-review.yml|review_autofix.yml) ;;
		*) candidates+=("${caller_workflow}") ;;
	esac
	candidates+=(review_autofix.yml)
	for candidate in "${candidates[@]}"; do
		if gh workflow run "${candidate}" --repo "${REPOSITORY}" \
			-f pr_number="${PR_NUMBER}" -f allow_workflow_edits="${allow_edits}" >/dev/null 2>&1; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done
	return 1
}

freshness_ci_failed_main()
{
	local switch candidate body

	switch="$(printf '%s' "${FRESHNESS_CI_FAILED_REDISPATCH_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
	case "${switch}" in
		''|true) ;;
		false)
			freshness_ci_failed_log disabled
			return 0
			;;
		*)
			echo "::warning::FRESHNESS_CI_FAILED_REDISPATCH_ENABLED has an unrecognised value; the PR is not sent back to review. Use true or false."
			freshness_ci_failed_log disabled "reason=invalid_switch"
			return 0
			;;
	esac

	if ! [[ "${REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${REPOSITORY}" == *..* ]] \
		|| ! [[ "${PR_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${PR_HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then
		freshness_ci_failed_log invalid_input
		return 0
	fi

	# The backticks are Markdown code spans, not command substitution.
	# shellcheck disable=SC2016
	body="$(printf '**CI failed on the updated head `%s`.** This head was an update from the base branch, so it skipped the reviewers and only waited for CI. CI failed on the combined tree, so the PR goes back to a normal review and autofix run, which sees the failing checks.\n\n%s pr=%s head=%s -->' \
		"${PR_HEAD_SHA:0:12}" "${FRESHNESS_CI_FAILED_MARKER_PREFIX}" "${PR_NUMBER}" "${PR_HEAD_SHA}")"
	# Up to 3 attempts: a transient API error must not strand the PR, and a
	# duplicate marker (a lost success response) is harmless.
	local marker_attempt=1 marker_posted=false
	local retry_sleep="${FRESHNESS_CI_FAILED_RETRY_SLEEP_SECS:-5}"
	# 0-60 seconds: a huge repository-variable value would otherwise hold
	# the job in sleeps before the remaining attempts and the dispatch.
	{ [[ "${retry_sleep}" =~ ^[0-9]{1,2}$ ]] && [ "$((10#${retry_sleep}))" -le 60 ]; } || retry_sleep=5
	while [ "${marker_attempt}" -le 3 ]; do
		if gh api -X POST "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${body}" >/dev/null 2>&1; then
			marker_posted=true
			break
		fi
		[ "${marker_attempt}" -lt 3 ] && sleep "${retry_sleep}"
		marker_attempt=$((marker_attempt + 1))
	done
	if [ "${marker_posted}" != "true" ]; then
		# Without the marker the redispatched run would take the fast path
		# again and refuse the merge again, so do not dispatch.
		echo "::warning::Could not post the freshness CI-failure marker on PR #${PR_NUMBER}; not sending it back to review."
		freshness_ci_failed_log marker_failed
		return 0
	fi

	if candidate="$(freshness_ci_failed_dispatch_review_workflow)"; then
		freshness_ci_failed_log redispatched "workflow=${candidate}"
		return 0
	fi
	echo "::warning::Could not dispatch a review workflow for PR #${PR_NUMBER}; the next push or stall recovery re-runs review."
	freshness_ci_failed_log dispatch_failed
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	freshness_ci_failed_main
fi
