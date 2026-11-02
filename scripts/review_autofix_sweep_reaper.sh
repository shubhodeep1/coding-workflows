#!/usr/bin/env bash
# review_autofix_sweep_reaper.sh -- zombie run reaper for the review autofix
# sweep (P11b, root cause 9 of
# docs/plans/remove-unattended-pipeline-stuck-states-plan.md).
#
# Sourced by the "Enumerate open PRs and dispatch internal-review.yml" step of
# .github/workflows/review_autofix_sweep.yml from the trusted default-branch
# checkout (.ci-rerun-support); never executed directly and never loaded from
# a PR head, because it performs Actions writes.
#
# Problem: GitHub can wedge a run in `queued` for months. A normal cancel
# returns 409 ("has not been queued yet"), and the sweep only stopped such a
# run from suppressing dispatch (SWEEP_STALE_QUEUED_MINUTES); it never removed
# it. This helper cancels each review run queued longer than
# ZOMBIE_RUN_MAX_QUEUED_HOURS, and force-cancels it when the cancel returns 409.
#
# Input contract: one or more raw listing files that snapshot_active_review_runs
# already wrote (`${RUNNER_TEMP}/sweep-active-runs-<workflow>.json`): the
# concatenated page objects of
# `GET repos/<repo>/actions/workflows/<wf>/runs?status=queued|in_progress|pending`.
#
# GitHub API (§14): the helper makes NO listing call. It reuses the listing the
# sweep already fetched; its only calls are
# `POST repos/<repo>/actions/runs/<id>/cancel` and, on 409,
# `POST repos/<repo>/actions/runs/<id>/force-cancel`: at most 10 runs per
# sweep, so at most 20 POSTs.
#
# Env (defaults live here, §8):
#   ZOMBIE_RUN_REAPER_ENABLED   (default true; false/0/no/off: no output, no call)
#   ZOMBIE_RUN_MAX_QUEUED_HOURS (default 24; must be a positive integer, else
#                                warn and cancel nothing)
#   ZOMBIE_RUN_REAPER_TOKEN     (default READ_TOKEN, then GH_TOKEN)
#   REPOSITORY                  (owner/repo; required, else nothing is cancelled)
#   DRY_RUN                     (true: report candidates, no POST)
#
# Log lines (stdout):
#   ZOMBIE_RUN_REAPED run=<id> workflow=<path> age_h=<n> result=<cancel|force_cancel|failed>
#   ZOMBIE_RUN_REAPER_END outcome=<ran|dry_run|invalid_max_queued_hours|listing_unreadable|repository_invalid> ...
#
# Fail-open: the function always returns 0, uses only locals, never exits and
# never echoes gh's stderr (token hygiene), so it is safe under the caller's
# `set -euo pipefail` and can never fail the sweep.

# Maximum runs the reaper touches per sweep tick (fixed by the issue).
REVIEW_AUTOFIX_SWEEP_REAPER_MAX_PER_SWEEP=10

review_autofix_sweep_reap_zombie_runs()
{
	local reaper_enabled reaper_hours reaper_hours_safe reaper_max_s reaper_now
	local reaper_token reaper_repo reaper_limit reaper_tsv reaper_rc
	local reaper_err reaper_status reaper_result reaper_line
	local reaper_id reaper_path reaper_age
	local -a reaper_candidates=()
	local reaper_total=0 reaper_attempted=0 reaper_cancelled=0 reaper_force_cancelled=0 reaper_failed=0 reaper_deferred=0

	reaper_enabled="$(printf '%s' "${ZOMBIE_RUN_REAPER_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
	case "${reaper_enabled}" in
		false|0|no|off) return 0 ;;
	esac

	reaper_hours="${ZOMBIE_RUN_MAX_QUEUED_HOURS:-24}"
	if ! [[ "${reaper_hours}" =~ ^[1-9][0-9]{0,5}$ ]]; then
		# Fail closed toward not cancelling: a typo must never cancel runs.
		reaper_hours_safe="$(printf '%s' "${reaper_hours}" | tr -cd 'A-Za-z0-9_.-' | cut -c1-32)"
		echo "::warning::ZOMBIE_RUN_REAPER_END outcome=invalid_max_queued_hours value=${reaper_hours_safe}"
		return 0
	fi

	reaper_repo="${REPOSITORY:-}"
	if ! [[ "${reaper_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
		echo "::warning::ZOMBIE_RUN_REAPER_END outcome=repository_invalid"
		return 0
	fi

	reaper_limit="${REVIEW_AUTOFIX_SWEEP_REAPER_MAX_PER_SWEEP:-10}"
	[[ "${reaper_limit}" =~ ^[1-9][0-9]*$ ]] || reaper_limit=10
	reaper_token="${ZOMBIE_RUN_REAPER_TOKEN:-${READ_TOKEN:-${GH_TOKEN:-}}}"
	reaper_max_s=$((reaper_hours * 3600))
	reaper_now="$(date -u +%s 2>/dev/null || echo 0)"
	if ! [[ "${reaper_now}" =~ ^[1-9][0-9]*$ ]]; then
		echo "::warning::ZOMBIE_RUN_REAPER_END outcome=listing_unreadable reason=clock_unavailable"
		return 0
	fi

	# One jq pass over the slurped pages. A run id listed under more than one
	# status (it moved between the sequential status fetches) is reaped only
	# when every copy says `queued`; a run without a parseable created_at is
	# never reaped. Oldest first, so the choice is deterministic.
	reaper_rc=0
	reaper_tsv="$(cat -- "$@" 2>/dev/null | jq -r -s --argjson now "${reaper_now}" --argjson max_s "${reaper_max_s}" '
		[
			.[]
			| select(type == "object")
			| (.workflow_runs // [])
			| if type == "array" then .[] else empty end
			| select(type == "object")
			| select((.id | type) == "number" and .id > 0 and (.id | floor) == .id)
		]
		| group_by(.id)
		| map(select(all(.[]; (.status // "") == "queued")) | .[0])
		| map(
			. as $run
			| ((.created_at // "")
				| if type == "string" and . != "" then (try fromdateiso8601 catch null) else null end) as $created
			| select($created != null)
			| select(($now - $created) >= $max_s)
			| {
				id: $run.id,
				created: $created,
				path: (($run.path // "")
					| if type == "string" then sub("@.*$"; "") else "" end
					| if . == "" then "unknown" else . end),
				age_h: ((($now - $created) / 3600) | floor)
			}
		)
		| sort_by([.created, .id])
		| .[]
		| [(.id | tostring), .path, (.age_h | tostring)]
		| @tsv
	' 2>/dev/null)" || reaper_rc=$?
	if [ "${reaper_rc}" -ne 0 ]; then
		echo "::warning::ZOMBIE_RUN_REAPER_END outcome=listing_unreadable"
		return 0
	fi

	if [ -n "${reaper_tsv}" ]; then
		mapfile -t reaper_candidates <<< "${reaper_tsv}"
	fi
	reaper_total="${#reaper_candidates[@]}"
	if [ "${reaper_total}" -gt "${reaper_limit}" ]; then
		reaper_deferred=$((reaper_total - reaper_limit))
	fi

	if [ "${DRY_RUN:-false}" = "true" ]; then
		echo "ZOMBIE_RUN_REAPER_END outcome=dry_run candidates=${reaper_total} attempted=0 cancelled=0 force_cancelled=0 failed=0 deferred=${reaper_deferred}"
		return 0
	fi

	reaper_err="$(mktemp "${RUNNER_TEMP:-/tmp}/zombie-run-reaper.XXXXXX" 2>/dev/null || true)"
	[ -n "${reaper_err}" ] || reaper_err=/dev/null

	for reaper_line in "${reaper_candidates[@]}"; do
		[ "${reaper_attempted}" -lt "${reaper_limit}" ] || break
		IFS=$'\t' read -r reaper_id reaper_path reaper_age <<< "${reaper_line}" || true
		# Only a positive integer id ever reaches a REST path.
		[[ "${reaper_id:-}" =~ ^[1-9][0-9]*$ ]] || continue
		reaper_path="${reaper_path//[^A-Za-z0-9_.\/-]/}"
		[ -n "${reaper_path}" ] || reaper_path=unknown
		[[ "${reaper_age:-}" =~ ^[0-9]+$ ]] || reaper_age=unknown
		reaper_attempted=$((reaper_attempted + 1))

		# Called outside $(...) so GH_API_RETRY_LAST_STATUS survives; under the
		# workflow's inline shim it stays empty and the status comes from gh's
		# "(HTTP 409)" stderr suffix instead.
		GH_API_RETRY_LAST_STATUS=""
		reaper_rc=0
		GH_TOKEN="${reaper_token}" gh_api_retry --idempotent --optional -X POST \
			"repos/${reaper_repo}/actions/runs/${reaper_id}/cancel" >/dev/null 2>"${reaper_err}" || reaper_rc=$?
		if [ "${reaper_rc}" -eq 0 ]; then
			reaper_result=cancel
		else
			reaper_status="${GH_API_RETRY_LAST_STATUS:-}"
			if [ -z "${reaper_status}" ] && [ "${reaper_err}" != /dev/null ]; then
				reaper_status="$(grep -oE 'HTTP [0-9]{3}' "${reaper_err}" 2>/dev/null | head -1 | grep -oE '[0-9]{3}' || true)"
			fi
			reaper_result=failed
			if [ "${reaper_status}" = "409" ]; then
				reaper_rc=0
				GH_TOKEN="${reaper_token}" gh_api_retry --idempotent --optional -X POST \
					"repos/${reaper_repo}/actions/runs/${reaper_id}/force-cancel" >/dev/null 2>"${reaper_err}" || reaper_rc=$?
				[ "${reaper_rc}" -eq 0 ] && reaper_result=force_cancel
			fi
		fi
		case "${reaper_result}" in
			cancel) reaper_cancelled=$((reaper_cancelled + 1)) ;;
			force_cancel) reaper_force_cancelled=$((reaper_force_cancelled + 1)) ;;
			*) reaper_failed=$((reaper_failed + 1)) ;;
		esac
		echo "ZOMBIE_RUN_REAPED run=${reaper_id} workflow=${reaper_path} age_h=${reaper_age} result=${reaper_result}"
	done

	[ "${reaper_err}" = /dev/null ] || rm -f -- "${reaper_err}" 2>/dev/null || true
	echo "ZOMBIE_RUN_REAPER_END outcome=ran candidates=${reaper_total} attempted=${reaper_attempted} cancelled=${reaper_cancelled} force_cancelled=${reaper_force_cancelled} failed=${reaper_failed} deferred=${reaper_deferred}"
	return 0
}
