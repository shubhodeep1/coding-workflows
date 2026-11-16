#!/usr/bin/env bash
# Send a PR whose final CI run failed back to review/autofix
# (cut-ci-runner-time Phase 4, docs/plans/cut-ci-runner-time-plan.md,
# Refs #9528).
#
# The full CI suite runs once per PR head, dispatched when review finished
# with that head (`_pr_ensure_final_ci` in scripts/pr_checks_lib.sh). When
# that run fails, nothing else would look at the head again: review already
# passed and the merge paths only refuse the red `lint`. This runs from the
# `final-ci-failed` job of .github/workflows/ci.yml (workflow_dispatch runs
# only, after `lint` failed). It posts one marker comment bound to the head,
#   <!-- ai:final-ci-failed:v1 pr=<n> head=<40-hex sha> -->
# and dispatches the PR's review workflow, which collects the failing
# check-runs as usual so the autofix can fix them; its new head then gets its
# own final run. A head that already carries the marker is left alone, and
# `_pr_ensure_final_ci` never re-dispatches CI for a head whose run failed,
# so a head review cannot fix stays red instead of looping.
#
# Env: REPOSITORY, PR_NUMBER, PR_HEAD_SHA (the head the run tested),
# FINAL_CI_FAILED_REDISPATCH_ENABLED (unset, empty or `true` = on; `false` =
# off; anything else = off with a warning), FINAL_CI_FAILED_RETRY_SLEEP_SECS
# (default 5, 0-60): wait between marker POST attempts. The review workflow
# is dispatched in the order scripts/review_freshness_ci_failed.sh uses
# (internal-review.yml first in this repository).
# API budget (§15): one `GET pulls/<n>` (the head is still the tested one), one
# paginated comments read (the marker check; no existing call in this job
# reads the PR's comments), one comment POST (up to
# 3 attempts) and one workflow dispatch (a failed dispatch tries the next
# review workflow name). Never fails the job.
# Logs: REVIEW_FINAL_CI_FAILED pr=<n> head=<sha> outcome=redispatched|already_reported|superseded|disabled|invalid_input|read_failed|marker_failed|dispatch_failed [workflow=<file>] [current=<sha>]

set -uo pipefail

REVIEW_FINAL_CI_FAILED_MARKER_PREFIX="<!-- ai:final-ci-failed:v1"
REVIEW_FINAL_CI_FAILED_SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

review_final_ci_failed_log()
{
	echo "REVIEW_FINAL_CI_FAILED pr=${PR_NUMBER:-?} head=${PR_HEAD_SHA:-?} outcome=$1${2:+ $2}"
}

review_final_ci_failed_main()
{
	local switch body comments_json marker_seen candidate

	switch="$(printf '%s' "${FINAL_CI_FAILED_REDISPATCH_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
	case "${switch}" in
		''|true) ;;
		false)
			review_final_ci_failed_log disabled
			return 0
			;;
		*)
			echo "::warning::FINAL_CI_FAILED_REDISPATCH_ENABLED has an unrecognised value; the PR is not sent back to review. Use true or false."
			review_final_ci_failed_log disabled "reason=invalid_switch"
			return 0
			;;
	esac

	if ! [[ "${REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${REPOSITORY}" == *..* ]] \
		|| ! [[ "${PR_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${PR_HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then
		review_final_ci_failed_log invalid_input
		return 0
	fi

	# The PR must still be open at the head this run tested; a newer head gets
	# its own final run, so an old head is never sent back to review.
	local current_head
	current_head="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq 'if .state == "open" then (.head.sha // "") else "closed" end' 2>/dev/null || echo "")"
	if [ -z "${current_head}" ]; then
		echo "::warning::Could not read PR #${PR_NUMBER}; not sending it back to review this run."
		review_final_ci_failed_log read_failed
		return 0
	fi
	if [ "${current_head}" != "${PR_HEAD_SHA}" ]; then
		review_final_ci_failed_log superseded "current=${current_head}"
		return 0
	fi

	# Marker check: only a trusted author's marker for this exact head counts,
	# so an outside comment cannot suppress the hand-back.
	if ! comments_json="$(gh api --paginate "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments?per_page=100" 2>/dev/null)"; then
		echo "::warning::Could not read the comments of PR #${PR_NUMBER}; not sending it back to review this run."
		review_final_ci_failed_log read_failed
		return 0
	fi
	marker_seen="$(printf '%s' "${comments_json}" | jq -rs --arg marker "${REVIEW_FINAL_CI_FAILED_MARKER_PREFIX} pr=${PR_NUMBER} head=${PR_HEAD_SHA} -->" '
		[ .[] | (if type == "array" then .[] else empty end)
			| select(type == "object")
			| select(((.user.type // "") == "Bot") or ((.author_association // "") | IN("OWNER", "MEMBER", "COLLABORATOR")))
			| select((.body // "") | contains($marker)) ] | length > 0' 2>/dev/null | tail -n1)"
	if [ "${marker_seen}" = "true" ]; then
		review_final_ci_failed_log already_reported
		return 0
	fi
	if [ "${marker_seen}" != "false" ]; then
		echo "::warning::Could not parse the comments of PR #${PR_NUMBER}; not sending it back to review this run."
		review_final_ci_failed_log read_failed
		return 0
	fi

	# The backticks are Markdown code spans, not command substitution.
	# shellcheck disable=SC2016
	body="$(printf '**The final CI run failed on head `%s`.** CI runs the full suite once, after review finished with this head. The PR goes back to review and autofix, which see the failing checks; the next head gets its own final run.\n\n%s pr=%s head=%s -->' \
		"${PR_HEAD_SHA:0:12}" "${REVIEW_FINAL_CI_FAILED_MARKER_PREFIX}" "${PR_NUMBER}" "${PR_HEAD_SHA}")"
	local marker_attempt=1 marker_posted=false
	local retry_sleep="${FINAL_CI_FAILED_RETRY_SLEEP_SECS:-5}"
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
		echo "::warning::Could not post the final CI failure marker on PR #${PR_NUMBER}; not sending it back to review."
		review_final_ci_failed_log marker_failed
		return 0
	fi

	# Reuse the freshness hand-back's dispatch order (one implementation).
	# shellcheck source=/dev/null
	if ! source "${REVIEW_FINAL_CI_FAILED_SCRIPT_DIR}/review_freshness_ci_failed.sh" 2>/dev/null \
		|| ! type freshness_ci_failed_dispatch_review_workflow >/dev/null 2>&1; then
		echo "::warning::scripts/review_freshness_ci_failed.sh is unavailable; the next push or stall recovery re-runs review for PR #${PR_NUMBER}."
		review_final_ci_failed_log dispatch_failed "reason=helper_missing"
		return 0
	fi
	if candidate="$(REVIEW_AUTOFIX_CALLER_WORKFLOW_REF="" freshness_ci_failed_dispatch_review_workflow)"; then
		review_final_ci_failed_log redispatched "workflow=${candidate}"
		return 0
	fi
	echo "::warning::Could not dispatch a review workflow for PR #${PR_NUMBER}; the next push or stall recovery re-runs review."
	review_final_ci_failed_log dispatch_failed
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	review_final_ci_failed_main
fi
