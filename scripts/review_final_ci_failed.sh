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
# A failed sync-quick run (FINAL_CI_FAILED_SYNC_QUICK=true, the run's
# `sync_quick` input; scripts/ci_sync_quick.sh) is not sent back to review:
# it skipped the full suite on the dispatcher's word, so its failure may say
# nothing about the PR. It dispatches the head's full final run instead
# (FINAL_CI_FAILED_CI_WORKFLOW, default ci.yml), using the head ref from the
# same `GET pulls/<n>` read, and posts no marker. That full run is named
# `CI [pr:<n>]` without the suffix, so if it fails too the PR goes back to
# review as usual and the hand-off cannot loop.
# Infra failures (docs/plans/final-ci-failure-ci-fix-plan.md Phase 1): before
# the marker POST, attempt 1 of the run (FINAL_CI_FAILED_RUN_ID,
# FINAL_CI_FAILED_RUN_ATTEMPT) is classified with
# `scripts/ci_cancelled_rerun.py classify-run`, ignoring this job's own
# in-progress `final-ci-failed` job. A runner shutdown, action-resolution,
# checkout-fetch or abandoned-job verdict posts no marker and dispatches
# nothing (outcome=infra_rerun_deferred reason=<verdict>): the review sweep's
# "Re-run cancelled CI failed jobs once" step re-runs the failed jobs on its
# next tick, and a second failure (attempt 2) is handed back as usual. Any
# other verdict, a classifier error or timeout (120 s), attempt >= 2, or a
# switch off keeps the hand-back. Switches: FINAL_CI_FAILED_INFRA_RERUN_ENABLED
# (unset, empty or `true` = on; `false` = off; anything else = off with a
# warning) and CI_CANCELLED_AUTO_RERUN_ENABLED (parsed like the sweep: unset
# or `true` = on, anything else = the sweep never re-runs, so nothing is
# deferred). Only shubhodeep1/coding-workflows defers: the sweep's re-run
# runs nowhere else. The per-cause CI_*_RERUN_ENABLED switches
# are read by the classifier, as in the sweep. Extra API budget: one jobs GET
# plus at most 10 annotation GETs, only on a non-sync-quick run on the current
# head with no marker yet.
# Logs: REVIEW_FINAL_CI_FAILED pr=<n> head=<sha> outcome=redispatched|already_reported|superseded|disabled|invalid_input|read_failed|marker_failed|dispatch_failed|full_suite_dispatched|full_suite_dispatch_failed|infra_rerun_deferred [workflow=<file>] [current=<sha>] [reason=<verdict>]

set -uo pipefail
# Signed job-token comments (run-volume plan item 15): route this script's
# gh comment calls through the shim even when no earlier step loaded it.
[ -z "${BASH_SOURCE[0]:-}" ] || AI_COMMENT_SIG_ACTIVATE_ONLY=1 source "$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh" >/dev/null 2>&1 || true

REVIEW_FINAL_CI_FAILED_MARKER_PREFIX="<!-- ai:final-ci-failed:v1"
REVIEW_FINAL_CI_FAILED_SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

review_final_ci_failed_log()
{
	echo "REVIEW_FINAL_CI_FAILED pr=${PR_NUMBER:-?} head=${PR_HEAD_SHA:-?} outcome=$1${2:+ $2}"
}

# Print the classify-run verdict when the run may be deferred to the sweep's
# re-run, or nothing when it must be handed back (fail toward the hand-back).
review_final_ci_failed_infra_verdict()
{
	local final_ci_infra_switch final_ci_infra_verdict
	final_ci_infra_switch="$(printf '%s' "${FINAL_CI_FAILED_INFRA_RERUN_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
	case "${final_ci_infra_switch}" in
		''|true) ;;
		false) return 0 ;;
		*)
			echo "::warning::FINAL_CI_FAILED_INFRA_RERUN_ENABLED has an unrecognised value; infra failures are handed back to review. Use true or false." >&2
			return 0
			;;
	esac
	# Defer only where the sweep re-runs: its source repository, with its
	# switch parsed the same way (unset = on, empty or anything but `true` = off).
	if [ "${REPOSITORY}" != "shubhodeep1/coding-workflows" ] \
		|| [ "$(printf '%s' "${CI_CANCELLED_AUTO_RERUN_ENABLED-true}" | tr '[:upper:]' '[:lower:]')" != "true" ] \
		|| [ "${FINAL_CI_FAILED_RUN_ATTEMPT:-}" != "1" ] \
		|| ! [[ "${FINAL_CI_FAILED_RUN_ID:-}" =~ ^[1-9][0-9]*$ ]]; then
		return 0
	fi
	final_ci_infra_verdict="$(PYTHONDONTWRITEBYTECODE=1 timeout 120 python3 "${REVIEW_FINAL_CI_FAILED_SCRIPT_DIR}/ci_cancelled_rerun.py" classify-run \
		--repo "${REPOSITORY}" --run-id "${FINAL_CI_FAILED_RUN_ID}" --ignore-job final-ci-failed 2>/dev/null | tail -n 1)"
	case "${final_ci_infra_verdict}" in
		shutdown|action_resolution|checkout_fetch|abandoned) printf '%s' "${final_ci_infra_verdict}" ;;
	esac
	return 0
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
	local current_head="" current_pull final_ci_failed_head_ref="" final_ci_failed_head_repo="" final_ci_failed_wf
	current_pull="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq 'if .state == "open" then "\(.head.sha // "") \(.head.ref // "-") \(.head.repo.full_name // "-")" else "closed - -" end' 2>/dev/null || echo "")"
	read -r current_head final_ci_failed_head_ref final_ci_failed_head_repo <<<"${current_pull}"
	if [ -z "${current_head}" ]; then
		echo "::warning::Could not read PR #${PR_NUMBER}; not sending it back to review this run."
		review_final_ci_failed_log read_failed
		return 0
	fi
	if [ "${current_head}" != "${PR_HEAD_SHA}" ]; then
		review_final_ci_failed_log superseded "current=${current_head}"
		return 0
	fi

	if [ "${FINAL_CI_FAILED_SYNC_QUICK:-false}" = "true" ]; then
		final_ci_failed_wf="${FINAL_CI_FAILED_CI_WORKFLOW:-ci.yml}"
		if [[ "${final_ci_failed_wf}" =~ ^[A-Za-z0-9._-]+\.ya?ml$ ]] \
			&& [ "${final_ci_failed_head_repo}" = "${REPOSITORY}" ] \
			&& [[ "${final_ci_failed_head_ref}" =~ ^[A-Za-z0-9._/-]+$ ]] && [[ "${final_ci_failed_head_ref}" != *..* ]] && [[ "${final_ci_failed_head_ref}" != -* ]] \
			&& gh workflow run "${final_ci_failed_wf}" --repo "${REPOSITORY}" --ref "${final_ci_failed_head_ref}" -f pr_number="${PR_NUMBER}" -f head_sha="${PR_HEAD_SHA}" >/dev/null 2>&1; then
			review_final_ci_failed_log full_suite_dispatched "workflow=${final_ci_failed_wf}"
			return 0
		fi
		echo "::warning::Could not dispatch the full final CI run for PR #${PR_NUMBER} after its sync-quick run failed; the next _pr_ensure_final_ci call dispatches it (a failed sync-quick run does not count as the head's final result)."
		review_final_ci_failed_log full_suite_dispatch_failed "workflow=${final_ci_failed_wf}"
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

	local final_ci_infra_reason
	final_ci_infra_reason="$(review_final_ci_failed_infra_verdict)"
	if [ -n "${final_ci_infra_reason}" ]; then
		echo "::notice::The final CI run for PR #${PR_NUMBER} failed for an infrastructure reason (${final_ci_infra_reason}); the review sweep re-runs its failed jobs once instead of sending the PR back to review."
		review_final_ci_failed_log infra_rerun_deferred "reason=${final_ci_infra_reason}"
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
