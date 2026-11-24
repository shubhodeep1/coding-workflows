#!/usr/bin/env bash
# Signed job-token comments (run-volume plan item 15): route this script's
# gh comment calls through the shim even when no earlier step loaded it.
[ -z "${BASH_SOURCE[0]:-}" ] || AI_COMMENT_SIG_ACTIVATE_ONLY=1 source "$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh" >/dev/null 2>&1 || true
# shellcheck shell=bash
# Smoke-fixture project backstop, sourced by scripts/orchestrate_poll_process.sh.
#
# The release gate (.github/workflows/test-and-mark-stable.yml) creates
# orchestrator projects as test fixtures and closes them in its own `if:
# always()` cleanups. A fixture created after the gate's cleanup ran (an
# orchestrate bootstrap left queued past the watcher's timeout) or one whose job
# was killed before cleanup stays open, and the poller keeps driving it: waves,
# CI triage, implement runs, PRs (gates 43445515854, 43590943288, 43708007145
# left projects #9490, #9523, #9550).
#
# smoke_fixture_reap_if_gate_done <tracking_num> <title> <labels_json>
#
# Called only for tracking issues tracking_issue_is_smoke_fixture already
# matched. Reads the gate run id from the title (`E2E Orchestrate Smoke <id>`,
# or `(run <id>)` on a `[E2E ` title), else from the tracking issue body. When
# that run is a test-and-mark-stable.yml run in this repository and its status
# is `completed`, it closes, in this order:
#   1. open PRs with base or head `orchestrator/project-<N>`;
#   2. open `ai:orchestrator-managed` issues whose body names
#      `Tracking issue: #<N>` (exact number, so #95 never matches #9550);
#   3. the tracking issue, then one comment listing what was closed.
# It deletes nothing (no branch, no run). The tracking issue closes last, so a
# failed close leaves it open and the next tick retries.
#
# Returns 0 when the caller must not process the project this tick (it was
# closed, or a close failed and the next tick retries); returns 1 otherwise
# (not a fixture with a readable gate run, gate still running, any lookup
# failed). Fail-open: an API error never blocks the tick.
#
# API calls (CLAUDE.md §15): per matched tracking issue and tick, one run GET,
# plus one issue GET only when the title has no gate run id. A reap adds two
# PR listings, one paginated issue listing, one PATCH per item and one comment.
# The gate run is looked up in GITHUB_REPOSITORY, so a consumer repository used
# as the gate's test_repo is never reaped (the lookup returns 404 there).
#
# Log line (stable prefix, agents.md "Stable log prefixes (contractual)"):
#   SMOKE_FIXTURE_REAP tracking_issue=<N> gate_run=<id|none> outcome=<closed|skip|failed> reason=<...>[ closed_prs=<n> closed_issues=<n>]

_smoke_fixture_reap_gate_workflow_file="test-and-mark-stable.yml"

# Print the gate run id carried by text $1, or nothing.
_smoke_fixture_reap_gate_run_id_from_text()
{
	local text="${1:-}"
	local text_lc="${text,,}"
	local re_orch='e2e orchestrate smoke ([1-9][0-9]{4,19})'
	local re_run='\(run ([1-9][0-9]{4,19})\)'
	if [[ "${text_lc}" =~ ${re_orch} ]]; then
		printf '%s' "${BASH_REMATCH[1]}"
		return 0
	fi
	case "${text}" in
		"[E2E "*)
			if [[ "${text}" =~ ${re_run} ]]; then
				printf '%s' "${BASH_REMATCH[1]}"
				return 0
			fi
			;;
	esac
	return 1
}

_smoke_fixture_reap_log()
{
	echo "SMOKE_FIXTURE_REAP $*"
}

smoke_fixture_reap_if_gate_done()
{
	local tracking_num="${1:-}"
	local title="${2:-}"
	local repo="${GITHUB_REPOSITORY:-}"
	[[ "${tracking_num}" =~ ^[1-9][0-9]*$ ]] || return 1
	[ -n "${repo}" ] || return 1

	local gate_run=""
	gate_run="$(_smoke_fixture_reap_gate_run_id_from_text "${title}" || true)"
	if [ -z "${gate_run}" ]; then
		local body=""
		if ! body="$(gh_retry gh api "repos/${repo}/issues/${tracking_num}" --jq '.body // ""' 2>/dev/null)"; then
			_smoke_fixture_reap_log "tracking_issue=${tracking_num} gate_run=none outcome=skip reason=body_unavailable"
			return 1
		fi
		gate_run="$(_smoke_fixture_reap_gate_run_id_from_text "${body}" || true)"
	fi
	if [ -z "${gate_run}" ]; then
		_smoke_fixture_reap_log "tracking_issue=${tracking_num} gate_run=none outcome=skip reason=no_gate_run_id"
		return 1
	fi

	local run_info=""
	if ! run_info="$(gh_retry gh api "repos/${repo}/actions/runs/${gate_run}" \
		--jq '[(.status // ""), ((.path // "") | split("@")[0] | split("/") | last)] | @tsv' 2>/dev/null)"; then
		_smoke_fixture_reap_log "tracking_issue=${tracking_num} gate_run=${gate_run} outcome=skip reason=gate_run_unavailable"
		return 1
	fi
	local gate_status="${run_info%%$'\t'*}"
	local gate_file="${run_info#*$'\t'}"
	if [ "${gate_file}" != "${_smoke_fixture_reap_gate_workflow_file}" ]; then
		_smoke_fixture_reap_log "tracking_issue=${tracking_num} gate_run=${gate_run} outcome=skip reason=not_gate_run"
		return 1
	fi
	if [ "${gate_status}" != "completed" ]; then
		_smoke_fixture_reap_log "tracking_issue=${tracking_num} gate_run=${gate_run} outcome=skip reason=gate_running status=${gate_status:-unknown}"
		return 1
	fi

	local branch="orchestrator/project-${tracking_num}"
	local owner="${repo%%/*}"
	local prs_base="" prs_head="" children=""
	if ! prs_base="$(gh_retry gh api "repos/${repo}/pulls?state=open&base=${branch}&per_page=100" --jq '.[].number' 2>/dev/null)" \
		|| ! prs_head="$(gh_retry gh api "repos/${repo}/pulls?state=open&head=${owner}:${branch}&per_page=100" --jq '.[].number' 2>/dev/null)" \
		|| ! children="$(gh_retry gh api --paginate "repos/${repo}/issues?state=open&labels=ai:orchestrator-managed&per_page=100" \
			--jq ".[] | select(.pull_request | not) | select((.body // \"\") | test(\"Tracking issue: #${tracking_num}([^0-9]|\$)\")) | .number" 2>/dev/null)"; then
		_smoke_fixture_reap_log "tracking_issue=${tracking_num} gate_run=${gate_run} outcome=skip reason=listing_failed"
		return 1
	fi

	local failed=0 closed_prs=0 closed_issues=0 closed_list="" item
	for item in $(printf '%s\n' "${prs_base}" "${prs_head}" | grep -E '^[1-9][0-9]*$' | sort -un); do
		if gh_retry gh api -X PATCH "repos/${repo}/pulls/${item}" -f state=closed >/dev/null 2>&1; then
			closed_prs=$((closed_prs + 1))
			closed_list+=" #${item}"
		else
			failed=$((failed + 1))
		fi
	done
	for item in $(printf '%s\n' "${children}" | grep -E '^[1-9][0-9]*$' | sort -un); do
		if gh_retry gh api -X PATCH "repos/${repo}/issues/${item}" -f state=closed -f state_reason=not_planned >/dev/null 2>&1; then
			closed_issues=$((closed_issues + 1))
			closed_list+=" #${item}"
		else
			failed=$((failed + 1))
		fi
	done
	if [ "${failed}" -gt 0 ]; then
		_smoke_fixture_reap_log "tracking_issue=${tracking_num} gate_run=${gate_run} outcome=failed reason=item_close_failed failed=${failed} closed_prs=${closed_prs} closed_issues=${closed_issues}"
		echo "::warning::Smoke fixture project #${tracking_num}: ${failed} PR/issue close(s) failed; the tracking issue stays open and the next tick retries."
		return 0
	fi

	local comment="Closed by the orchestrator poller's smoke-fixture backstop: this project was created by release-gate run ${gate_run} (test-and-mark-stable.yml), which has completed without closing it. Closed with it:${closed_list:- nothing else}. No branch or run was deleted.

<!-- ai:smoke-fixture-reap gate_run=${gate_run} -->"
	# Close first, comment after: a failed close is retried next tick, and
	# commenting only after a successful close keeps retries from stacking
	# duplicate comments.
	if ! gh_retry gh api -X PATCH "repos/${repo}/issues/${tracking_num}" -f state=closed -f state_reason=not_planned >/dev/null 2>&1; then
		_smoke_fixture_reap_log "tracking_issue=${tracking_num} gate_run=${gate_run} outcome=failed reason=tracking_close_failed closed_prs=${closed_prs} closed_issues=${closed_issues}"
		echo "::warning::Smoke fixture project #${tracking_num}: closing the tracking issue failed; the next tick retries."
		return 0
	fi
	gh_retry gh api -X POST "repos/${repo}/issues/${tracking_num}/comments" -f body="${comment}" >/dev/null 2>&1 || true
	_smoke_fixture_reap_log "tracking_issue=${tracking_num} gate_run=${gate_run} outcome=closed reason=gate_completed closed_prs=${closed_prs} closed_issues=${closed_issues}"
	return 0
}
