#!/usr/bin/env bash
# Review sweep safety net for the final CI run (cut-ci-runner-time Phase 4,
# docs/plans/cut-ci-runner-time-plan.md, Refs #9528).
#
# The full CI suite runs once per PR head, dispatched when review finishes
# with that head (`_pr_ensure_final_ci` in scripts/pr_checks_lib.sh, called
# from the review workflow). A head whose review posted `ai-review/head-gate`
# success but whose review run never reached that call (it left the
# auto-merge step early, or the dispatch failed) would otherwise never get
# CI and never merge. Every sweep tick this script finds such heads among
# the open non-draft PRs and hands each to `_pr_ensure_final_ci`.
#
# Env:
#   REVIEW_FINAL_CI_WORKFLOW  workflow file to ensure (empty = off; the
#                             script then makes no API call)
#   REPOSITORY                owner/repo
#   FINAL_CI_SWEEP_PR_SNAPSHOT  the sweep's open-PR snapshot (JSON array of
#                             {number, head_ref, head_sha}), written by
#                             "Enumerate open PRs and dispatch internal-review.yml"
#   FINAL_CI_SWEEP_MAX_PER_TICK  candidates checked per tick (default 10, 1-50)
#   HEAD_REF_FILTER, DRY_RUN  the sweep's dispatch inputs
#   FINAL_CI_SWEEP_HELPERS_DIR  directory holding gh_helpers.sh and
#                             pr_checks_lib.sh (default: this script's dir)
# API budget (§15): one `GET actions/workflows/<file>/runs?per_page=100`
# listing per tick, matched locally against the snapshot's head SHAs (the
# snapshot itself is the sweep's existing PR read; it carries no commit
# statuses). Then, for at most FINAL_CI_SWEEP_MAX_PER_TICK heads with no run
# in that listing: one `GET commits/<sha>/status` (ai-review/head-gate), and,
# for a head whose gate is success, `_pr_ensure_final_ci` (one head-SHA runs
# read, at most one dispatch). orchestrator/project-* heads are skipped:
# their push CI run already covers them.
# Snapshot rows that carry `head_gate_state` (written by
# scripts/review_sweep_head_gate.sh in the enumerate step, one GraphQL read
# per 100 PRs) need no status read: a known non-success head is counted as
# gate_not_success without spending the cap, and known-success heads are
# checked first. Before this, the cap went to gate-pending heads in PR order
# and starved approved heads further down (2026-11-17 08:30: candidates=42
# checked=10 gate_not_success=10 ensured=0). Rows without the field (the
# GraphQL read failed) are read one by one after the known-success heads, as
# before.
# Fail-open: a failed listing or status read skips that PR this tick.
# Logs: one `REVIEW_FINAL_CI_SWEEP outcome=... ` summary line, plus the
# helper's `REVIEW_FINAL_CI` lines.
#
# Base sync after a failed final CI run (final-CI self-heal, 2026-11-17).
# A head whose only verdict is a failed CI run never gets another one:
# `_pr_ensure_final_ci` answers `exists failure`, and the review/autofix
# hand-back (scripts/review_final_ci_failed.sh) makes no edit when the failure
# is not in the PR's diff. In practice those failures came from the base: a
# head many commits behind runs its own stale ci.yml (the dispatched workflow
# file is the head branch's), an old base still fires the retired
# `pull_request` trigger (no hand-back at all), or the run tested a merge ref
# GitHub had not refreshed after the base fixed the failure (#9454, #9562,
# #9602, #9614, #9621). So, for an open non-draft head outside
# orchestrator/project-* whose completed runs of the workflow are all failures
# (any event; no success, none in flight; a failed sync-quick run alone does
# not count), whose newest failure finished at least
# FINAL_CI_SWEEP_BASE_SYNC_MIN_AGE_MINUTES ago (the hand-back's review gets
# the first chance), that has no queued, pending or running review/autofix
# run in the enumerate step's snapshot (FINAL_CI_SWEEP_ACTIVE_REVIEW_RUNS; a
# missing or incomplete snapshot waits too: a branch update would race that
# run's push), and whose newest `ai-review/head-gate` status is success and
# was posted by the token's own login (the marker below lets the update skip
# the reviewers, so a status any status writer could post does not count),
# the sweep:
#   1. skips unless the head is behind its base (`_pr_base_freshness` outcome
#      clean or overlap; fresh means a base sync cannot help);
#   2. skips while the base's newest completed push run of the workflow failed
#      (main, stable and orchestrator/project-* have push CI; other bases such
#      as ai/issue-<n> have none and are synced anyway), so a red base is
#      fixed on the base first;
#   3. skips once FINAL_CI_SWEEP_BASE_SYNC_MAX sweep syncs exist on the PR;
#   4. updates the branch bound to the head (`_pr_base_sync_for_fresh_ci`),
#      verifies GitHub's merge commit, posts the `ai:merge-base-sync:v1`
#      marker (so the synchronize review only waits for CI, as for a merge-path
#      update; the marker also carries `<!-- ai:final-ci-base-sync:v1 pr=<n> -->`
#      for the cap) and dispatches the new head's final CI run
#      (`_pr_base_sync_dispatch_final_ci`).
# The new head carries the base's tree and its ci.yml, so its final run tests
# the current combination, and a red result goes back to review as usual.
# Env: FINAL_CI_SWEEP_BASE_SYNC_ENABLED (default true; true/1/yes/on),
# FINAL_CI_SWEEP_BASE_SYNC_MIN_AGE_MINUTES (default 30, 0-1440),
# FINAL_CI_SWEEP_BASE_SYNC_MAX (default 3, 0-20; 0 turns syncing off),
# FINAL_CI_SWEEP_ACTIVE_REVIEW_RUNS (file the enumerate step writes). At most
# FINAL_CI_SWEEP_MAX_PER_TICK candidates per tick reach an API call, oldest
# failure first; one skipped by the age or in-flight check is not counted.
# API budget (§15), per counted candidate, in this order and stopping at the
# first skip: one `user` plus one paginated comments read
# (`_pr_base_sync_count`: the cap and the trusted login), one
# `commits/<sha>/statuses?per_page=100` read (the combined `status` read of
# the no-run pass carries no creator), `_pr_base_freshness` (one `pulls/<n>`,
# one compare, one paginated PR-files read), for a push-CI base one
# `runs?branch=<base>&event=push` read only when the tick's listing holds no
# completed push run of that base (cached per base for the tick), then the
# update-branch PUT and the existing observe / marker / dispatch calls. A
# failed read skips the PR this tick.
# Log: `REVIEW_FINAL_CI_BASE_SYNC pr=<n> head=<sha> outcome=synced|skipped|failed|dry_run reason=<token>`
# (skip reasons include gate_untrusted and review_in_flight).
#
# Stale workflow file (issue #10093). scripts/review_final_ci_failed.sh posts
# `<!-- ai:final-ci-stale-workflow:v1 pr=<n> head=<sha> -->` instead of a
# hand-back when the head's final run was cut off at a job time limit while
# the base's ci.yml differs. The same comments read (`_pr_base_sync_count
# <pr> <sha>`) finds that marker, so it costs no extra call; a head that
# also carries its `ai:final-ci-failed:v1` marker was already handed back and
# counts as unmarked. A marked head
# that is behind goes through the sync above unchanged (counted against
# FINAL_CI_SWEEP_BASE_SYNC_MAX). A marked head that cannot be synced is
# handed back to review on a terminal skip (max_syncs, up_to_date,
# freshness_disabled, gate_failure|error|absent|untrusted,
# update_branch_refused, which is how a conflicting update surfaces, or
# FINAL_CI_STALE_WORKFLOW_SYNC_ENABLED=false at sweep time, reason
# stale_workflow_sync_disabled): the sweep runs the default-branch
# scripts/review_final_ci_failed.sh with the stale and infra switches off
# (its PR read, failed-marker check, marker POST and review dispatch; the
# failed marker caps it at one hand-back per head) and appends
# ` stale_workflow=handed_back` to the candidate's log line
# (` stale_workflow=dry_run` under DRY_RUN, which runs nothing). Transient
# skips (too_recent, review_in_flight, sync_count_unknown, gate_pending,
# gate_unknown, other freshness outcomes, base_red) retry on a later tick.
# Env: FINAL_CI_STALE_WORKFLOW_SYNC_ENABLED (default true; `false` or any
# other value hands a marked head back), plus what the hand-back script
# reads (FINAL_CI_FAILED_REDISPATCH_ENABLED, ALLOW_WORKFLOW_EDITS).

set -uo pipefail

FINAL_CI_SWEEP_SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

review_final_ci_sweep_base_sync_log()
{
	echo "REVIEW_FINAL_CI_BASE_SYNC pr=${1:-?} head=${2:-?} outcome=$3 reason=$4${5:+ $5}"
}

# `review_final_ci_sweep_base_ci_red <base ref>`: returns 0 when the base has
# push CI and its newest completed (success or failure) push run of the
# workflow failed, 1 otherwise (green, no push CI, or unknown: an unreadable
# base result does not block the sync). Reads the tick's listing first
# (FINAL_CI_SWEEP_LISTING_FILE), then at most one runs read per base, cached
# in FINAL_CI_SWEEP_BASE_STATE for the tick.
declare -A FINAL_CI_SWEEP_BASE_STATE=()
review_final_ci_sweep_base_ci_red()
{
	local base="$1" state="" base_runs=""
	case "${base}" in
		main|stable|orchestrator/project-*) ;;
		*) return 1 ;;
	esac
	if [ -n "${FINAL_CI_SWEEP_BASE_STATE[${base}]+x}" ]; then
		state="${FINAL_CI_SWEEP_BASE_STATE[${base}]}"
	else
		state="$(jq -r --arg base "${base}" '
			[.workflow_runs[]? | select(type == "object")
				| select((.event // "") == "push" and (.head_branch // "") == $base and (.status // "") == "completed")
				| select((.conclusion // "") | IN("success", "failure"))]
			| sort_by(.created_at // "") | last | (.conclusion // "")' "${FINAL_CI_SWEEP_LISTING_FILE}" 2>/dev/null | tail -n1)"
		if [ -z "${state}" ]; then
			base_runs="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/actions/workflows/${REVIEW_FINAL_CI_WORKFLOW}/runs?branch=${base}&event=push&status=completed&per_page=10" 2>/dev/null || echo "")"
			state="$(printf '%s' "${base_runs}" | jq -r '
				[.workflow_runs[]? | select(type == "object") | select((.conclusion // "") | IN("success", "failure"))]
				| sort_by(.created_at // "") | last | (.conclusion // "")' 2>/dev/null | tail -n1)"
		fi
		FINAL_CI_SWEEP_BASE_STATE["${base}"]="${state:-unknown}"
		state="${state:-unknown}"
	fi
	[ "${state}" = "failure" ]
}

# `review_final_ci_sweep_active_review <pr> <head ref>`: returns 0 when the
# enumerate step's snapshot (FINAL_CI_SWEEP_ACTIVE_REVIEW_RUNS) lists a queued,
# pending or running internal-review.yml / review_autofix.yml run for the PR
# (keyed by head ref or `pr:<n>`, as the dispatch skip reads it), or when the
# snapshot is missing or incomplete: a branch update would race the push that
# run may make, so the sync waits. Returns 1 only for a complete snapshot
# with no such run. No API call.
review_final_ci_sweep_active_review()
{
	local number="$1" ref="$2" snapshot="${FINAL_CI_SWEEP_ACTIVE_REVIEW_RUNS:-}"
	[ -n "${snapshot}" ] && [ -f "${snapshot}" ] || return 0
	grep -qx "incomplete" "${snapshot}" && return 0
	awk -v a="internal-review.yml:${ref}" -v b="review_autofix.yml:${ref}" \
		-v c="internal-review.yml:pr:${number}" -v d="review_autofix.yml:pr:${number}" '
		($1 == a || $1 == b || $1 == c || $1 == d) && ($2 + 0) > 0 { found = 1 }
		END { exit found ? 0 : 1 }' "${snapshot}"
}

# FINAL_CI_STALE_WORKFLOW_SYNC_ENABLED at sweep time (issue #10093): returns
# 0 when on (unset, empty or `true`); `false` is off; anything else is off
# with a warning (the value is never echoed).
review_final_ci_sweep_stale_sync_enabled()
{
	case "$(printf '%s' "${FINAL_CI_STALE_WORKFLOW_SYNC_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		''|true) return 0 ;;
		false) return 1 ;;
		*)
			echo "::warning::FINAL_CI_STALE_WORKFLOW_SYNC_ENABLED has an unrecognised value; stale-workflow heads are handed back to review. Use true or false."
			return 1
			;;
	esac
}

# `review_final_ci_sweep_stale_hand_back <pr> <head sha>` (issue #10093):
# hands a stale-workflow-marked head back to review through the
# default-branch scripts/review_final_ci_failed.sh, with the stale and infra
# switches off so it cannot defer again. Sets FINAL_CI_SWEEP_STALE_NOTE to
# the suffix the caller appends to its REVIEW_FINAL_CI_BASE_SYNC line.
review_final_ci_sweep_stale_hand_back()
{
	local hand_back_dir="${FINAL_CI_SWEEP_HELPERS_DIR:-${FINAL_CI_SWEEP_SCRIPT_DIR}}"
	if [ "${DRY_RUN:-false}" = "true" ]; then
		FINAL_CI_SWEEP_STALE_NOTE=" stale_workflow=dry_run"
		return 0
	fi
	PR_NUMBER="$1" PR_HEAD_SHA="$2" FINAL_CI_STALE_WORKFLOW_SYNC_ENABLED=false \
		FINAL_CI_FAILED_INFRA_RERUN_ENABLED=false FINAL_CI_FAILED_SYNC_QUICK=false \
		timeout 120 bash "${hand_back_dir}/review_final_ci_failed.sh" || true
	FINAL_CI_SWEEP_STALE_NOTE=" stale_workflow=handed_back"
	return 0
}

# `review_final_ci_sweep_base_sync <pr> <head sha> <head ref> <failed at>`:
# one candidate of the base-sync pass (see the header). The checks that need
# no API call come first; returns 2 when it stopped there, so the caller does
# not count the candidate against the per-tick cap, and 0 otherwise.
review_final_ci_sweep_base_sync()
{
	local number="$1" sha="$2" ref="$3" failed_at="$4"
	local min_age="${FINAL_CI_SWEEP_BASE_SYNC_MIN_AGE_MINUTES:-30}" sync_cap="${FINAL_CI_SWEEP_BASE_SYNC_MAX:-3}"
	local failed_epoch now_epoch base_ref sync_done sync_max gate stale_marked=""
	FINAL_CI_SWEEP_STALE_NOTE=""
	{ [[ "${min_age}" =~ ^[0-9]{1,4}$ ]] && [ "$((10#${min_age}))" -le 1440 ]; } || min_age=30
	min_age=$((10#${min_age}))
	{ [[ "${sync_cap}" =~ ^[0-9]{1,2}$ ]] && [ "$((10#${sync_cap}))" -le 20 ]; } || sync_cap=3
	sync_cap=$((10#${sync_cap}))
	if [ "${sync_cap}" -eq 0 ]; then
		review_final_ci_sweep_base_sync_log "${number}" "${sha}" skipped cap_zero
		return 2
	fi
	# GNU date reads "" and "-" as today's midnight, so only an ISO timestamp counts.
	if ! [[ "${failed_at}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z$ ]] || ! failed_epoch="$(date -u -d "${failed_at}" +%s 2>/dev/null)"; then
		review_final_ci_sweep_base_sync_log "${number}" "${sha}" skipped failed_at_unknown
		return 2
	fi
	now_epoch="$(date -u +%s)"
	if [ $(( now_epoch - failed_epoch )) -lt $(( min_age * 60 )) ]; then
		review_final_ci_sweep_base_sync_log "${number}" "${sha}" skipped too_recent "failed_at=${failed_at} min_age_minutes=${min_age}"
		return 2
	fi
	if review_final_ci_sweep_active_review "${number}" "${ref}"; then
		review_final_ci_sweep_base_sync_log "${number}" "${sha}" skipped review_in_flight "head_ref=${ref}"
		return 2
	fi
	if ! _pr_base_sync_count "${number}" "${sha}" || ! [[ "${PR_BASE_SYNC_FINAL_CI_COUNT:-}" =~ ^[0-9]+$ ]] || [ -z "${PR_BASE_SYNC_LOGIN:-}" ]; then
		review_final_ci_sweep_base_sync_log "${number}" "${sha}" skipped sync_count_unknown
		return 0
	fi
	# Issue #10093: a head whose final run was cut off under an old ci.yml.
	stale_marked="${PR_BASE_SYNC_STALE_WORKFLOW_MARKED:-false}"
	if [ "${stale_marked}" = "true" ] && ! review_final_ci_sweep_stale_sync_enabled; then
		review_final_ci_sweep_stale_hand_back "${number}" "${sha}"
		review_final_ci_sweep_base_sync_log "${number}" "${sha}" skipped stale_workflow_sync_disabled "${FINAL_CI_SWEEP_STALE_NOTE# }"
		return 0
	fi
	if [ "${PR_BASE_SYNC_FINAL_CI_COUNT}" -ge "${sync_cap}" ]; then
		[ "${stale_marked}" = "true" ] && review_final_ci_sweep_stale_hand_back "${number}" "${sha}"
		review_final_ci_sweep_base_sync_log "${number}" "${sha}" skipped max_syncs "syncs=${PR_BASE_SYNC_FINAL_CI_COUNT} max=${sync_cap}${FINAL_CI_SWEEP_STALE_NOTE}"
		return 0
	fi
	# The gate status authorizes the reviewer skip the marker grants, so only
	# the pipeline's own status counts (a principal that can merely write
	# commit statuses cannot vouch for a head; same rule as the gate-supersede
	# check in scripts/pr_checks_lib.sh). Newest ai-review/head-gate status
	# first; anything else is `untrusted`.
	gate="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/commits/${sha}/statuses?per_page=100" 2>/dev/null \
		| jq -r --arg login "${PR_BASE_SYNC_LOGIN}" '
			if type == "array" then
				([.[] | select(type == "object" and (.context // "") == "ai-review/head-gate")] | first) as $s
				| if $s == null then "absent"
				  elif (($s.creator.login // "") != $login) then "untrusted"
				  else ($s.state // "unknown") end
			else "unknown" end' 2>/dev/null | tail -n1)"
	if [ "${gate}" != "success" ]; then
		case "${gate}" in
			failure|error|absent|untrusted)
				[ "${stale_marked}" = "true" ] && review_final_ci_sweep_stale_hand_back "${number}" "${sha}"
				;;
		esac
		review_final_ci_sweep_base_sync_log "${number}" "${sha}" skipped "gate_${gate:-unknown}" "${FINAL_CI_SWEEP_STALE_NOTE# }"
		return 0
	fi
	_pr_base_freshness "${number}" "${sha}" "" >/dev/null
	case "${PR_BASE_FRESHNESS_OUTCOME:-unknown}" in
		clean|overlap) ;;
		fresh)
			[ "${stale_marked}" = "true" ] && review_final_ci_sweep_stale_hand_back "${number}" "${sha}"
			review_final_ci_sweep_base_sync_log "${number}" "${sha}" skipped up_to_date "${FINAL_CI_SWEEP_STALE_NOTE# }"
			return 0
			;;
		*)
			[ "${stale_marked}" = "true" ] && [ "${PR_BASE_FRESHNESS_OUTCOME:-unknown}" = "disabled" ] \
				&& review_final_ci_sweep_stale_hand_back "${number}" "${sha}"
			review_final_ci_sweep_base_sync_log "${number}" "${sha}" skipped "freshness_${PR_BASE_FRESHNESS_OUTCOME:-unknown}" "detail=${PR_BASE_FRESHNESS_LAST_REASON:-none}${FINAL_CI_SWEEP_STALE_NOTE}"
			return 0
			;;
	esac
	base_ref="${PR_BASE_FRESHNESS_BASE_REF:-}"
	if review_final_ci_sweep_base_ci_red "${base_ref}"; then
		review_final_ci_sweep_base_sync_log "${number}" "${sha}" skipped base_red "base=${base_ref} behind_by=${PR_BASE_FRESHNESS_BEHIND_BY:-?}"
		return 0
	fi
	if [ "${DRY_RUN:-false}" = "true" ]; then
		review_final_ci_sweep_base_sync_log "${number}" "${sha}" dry_run behind "base=${base_ref} behind_by=${PR_BASE_FRESHNESS_BEHIND_BY:-?}"
		return 0
	fi
	if ! _pr_base_sync_for_fresh_ci "${number}" "${sha}"; then
		[ "${stale_marked}" = "true" ] && review_final_ci_sweep_stale_hand_back "${number}" "${sha}"
		review_final_ci_sweep_base_sync_log "${number}" "${sha}" failed update_branch_refused "base=${base_ref}${FINAL_CI_SWEEP_STALE_NOTE}"
		return 0
	fi
	sync_done="${PR_BASE_SYNC_COUNT:-0}"
	sync_max="${MERGE_BASE_FRESHNESS_MAX_SYNCS:-2}"
	[[ "${sync_max}" =~ ^[0-9]$ ]] || sync_max=2
	if _pr_base_sync_observe_update_commit "${number}" "${sha}" "${base_ref}"; then
		# The backticks are Markdown code spans, not command substitution.
		# shellcheck disable=SC2016
		PR_BASE_SYNC_MARKER_LEAD="$(printf 'The final CI run on this head failed and `%s` has gained %s commit(s) since, so the branch now carries the current base and the final CI run tests that combination. <!-- ai:final-ci-base-sync:v1 pr=%s -->' \
			"${base_ref}" "${PR_BASE_FRESHNESS_BEHIND_BY:-?}" "${number}")" \
			_pr_base_sync_marker_post "${number}" "${sha}" "$(( sync_done + 1 ))" "${sync_max}" "${PR_BASE_SYNC_BASE_SHA}" "${PR_BASE_SYNC_MERGE_SHA}"
		_pr_base_sync_dispatch_final_ci "${number}" "${sha}" "${PR_BASE_SYNC_MERGE_SHA}"
		review_final_ci_sweep_base_sync_log "${number}" "${sha}" synced behind "base=${base_ref} behind_by=${PR_BASE_FRESHNESS_BEHIND_BY:-?} merge=${PR_BASE_SYNC_MERGE_SHA}"
	else
		# No verified update commit, no marker: the synchronize run reviews the
		# new head normally and the final CI run follows that review.
		review_final_ci_sweep_base_sync_log "${number}" "${sha}" synced unverified_update "base=${base_ref} observe=${PR_BASE_SYNC_OBSERVE_REASON:-unknown}"
	fi
	return 0
}

review_final_ci_sweep_main()
{
	local helpers_dir wf max listing candidates count checked=0 ensured=0 skipped_gate=0 number sha ref state known_state
	local base_sync_switch base_sync_queue="" failed_candidates base_sync_checked=0 failed_at
	wf="${REVIEW_FINAL_CI_WORKFLOW:-}"
	if [ -z "${wf}" ]; then
		echo "REVIEW_FINAL_CI_SWEEP outcome=disabled"
		return 0
	fi
	if ! [[ "${wf}" =~ ^[A-Za-z0-9._-]+\.ya?ml$ ]] || ! [[ "${REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
		|| [[ "${REPOSITORY}" == *..* ]]; then
		echo "::warning::REVIEW_FINAL_CI_SWEEP outcome=invalid_input (workflow or repository is not usable)"
		return 0
	fi
	if [ ! -s "${FINAL_CI_SWEEP_PR_SNAPSHOT:-}" ]; then
		echo "REVIEW_FINAL_CI_SWEEP outcome=skip reason=no_snapshot"
		return 0
	fi
	max="${FINAL_CI_SWEEP_MAX_PER_TICK:-10}"
	{ [[ "${max}" =~ ^[0-9]{1,2}$ ]] && [ "$((10#${max}))" -ge 1 ] && [ "$((10#${max}))" -le 50 ]; } || max=10
	max=$((10#${max}))

	helpers_dir="${FINAL_CI_SWEEP_HELPERS_DIR:-${FINAL_CI_SWEEP_SCRIPT_DIR}}"
	# shellcheck source=/dev/null
	source "${helpers_dir}/gh_helpers.sh" 2>/dev/null || true
	type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
	type _safe_gh_jq >/dev/null 2>&1 || _safe_gh_jq() { gh api "$@"; }
	# shellcheck source=/dev/null
	source "${helpers_dir}/pr_checks_lib.sh" 2>/dev/null || true
	if ! type _pr_ensure_final_ci >/dev/null 2>&1; then
		echo "::warning::REVIEW_FINAL_CI_SWEEP outcome=skip reason=helper_missing"
		return 0
	fi

	listing="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/actions/workflows/${wf}/runs?per_page=100" 2>/dev/null || echo "")"
	if ! printf '%s' "${listing}" | jq -e 'type == "object" and (.workflow_runs | type) == "array"' >/dev/null 2>&1; then
		echo "REVIEW_FINAL_CI_SWEEP outcome=skip reason=listing_failed"
		return 0
	fi
	# Candidates: snapshot heads with no run of the workflow in the listing
	# (a run that is only cancelled / timed out / startup_failure does not
	# count), outside orchestrator/project-*, matching HEAD_REF_FILTER.
	candidates="$(jq -r --slurpfile listing <(printf '%s' "${listing}") --arg filter "${HEAD_REF_FILTER:-}" '
		([$listing[0].workflow_runs[]
			| select((.status // "") != "completed" or ((.conclusion // "") | IN("success", "failure")))
			| .head_sha] | unique) as $covered
		| [.[] | select(type == "object")
			| select(((.number | tostring) | test("^[1-9][0-9]*$")) and ((.head_sha // "") | test("^[0-9a-f]{40}$")))
			| select(((.head_ref // "") | startswith("orchestrator/project-")) | not)
			| select($filter == "" or ((.head_ref // "") | contains($filter)))
			| select(.head_sha as $s | ($covered | index($s)) == null)
			| . + {known_gate: (.head_gate_state // "unknown" | if type == "string" and test("^[a-z]{1,16}$") then . else "unknown" end)}]
		| (map(select(.known_gate == "success")) + map(select(.known_gate != "success")))
		| .[] | "\(.number) \(.head_sha) \(.known_gate) \(.head_ref // "")"' "${FINAL_CI_SWEEP_PR_SNAPSHOT}" 2>/dev/null || true)"
	count=0
	[ -n "${candidates}" ] && count="$(printf '%s\n' "${candidates}" | grep -c .)"
	while read -r number sha known_state ref; do
		[ -n "${number:-}" ] || continue
		# Known from the snapshot and not success: no read, no cap spent.
		if [ "${known_state:-unknown}" != "unknown" ] && [ "${known_state}" != "success" ]; then
			skipped_gate=$((skipped_gate + 1))
			continue
		fi
		[ "${checked}" -lt "${max}" ] || continue
		checked=$((checked + 1))
		if [ "${known_state}" = "success" ]; then
			state="success"
		else
			state="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/commits/${sha}/status" \
				--jq '[.statuses[]? | select(.context == "ai-review/head-gate") | .state] | first // "absent"' 2>/dev/null || echo "unknown")"
		fi
		if [ "${state}" != "success" ]; then
			skipped_gate=$((skipped_gate + 1))
			continue
		fi
		if [ "${DRY_RUN:-false}" = "true" ]; then
			echo "REVIEW_FINAL_CI pr=${number} head=${sha} outcome=dry_run"
			continue
		fi
		_pr_ensure_final_ci "${REPOSITORY}" "${number}" "${sha}" "${ref}" || true
		ensured=$((ensured + 1))
		# An older failed run outside the tick's listing (e.g. a retired
		# `pull_request` run): the head-SHA read above just found it.
		if [ "${PR_FINAL_CI_OUTCOME:-}" = "exists" ] && [ "${PR_FINAL_CI_REASON:-}" = "failure" ]; then
			base_sync_queue+="${number} ${sha} ${ref:--} ${PR_FINAL_CI_FAILED_AT:--}"$'\n'
		fi
	done <<< "${candidates}"
	echo "REVIEW_FINAL_CI_SWEEP outcome=done candidates=${count} checked=${checked} gate_not_success=${skipped_gate} ensured=${ensured} cap=${max}"

	# Base-sync pass (header: "Base sync after a failed final CI run").
	base_sync_switch="$(printf '%s' "${FINAL_CI_SWEEP_BASE_SYNC_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
	case "${base_sync_switch}" in
		true|1|yes|on) ;;
		*)
			echo "REVIEW_FINAL_CI_SWEEP_BASE_SYNC outcome=disabled"
			return 0
			;;
	esac
	# Heads the listing already covers whose completed runs are all failures:
	# no run in flight, no success, at least one failed full (not sync-quick) run.
	failed_candidates="$(jq -r --slurpfile listing <(printf '%s' "${listing}") --arg filter "${HEAD_REF_FILTER:-}" --arg path ".github/workflows/${wf}" '
		[$listing[0].workflow_runs[]? | select(type == "object")
			| select(((.path // "") | split("@") | .[0]) == $path)] as $runs
		| [.[] | select(type == "object")
			| select(((.number | tostring) | test("^[1-9][0-9]*$")) and ((.head_sha // "") | test("^[0-9a-f]{40}$")))
			| select(((.head_ref // "") | startswith("orchestrator/project-")) | not)
			| select($filter == "" or ((.head_ref // "") | contains($filter)))
			| . as $pr
			| [$runs[] | select((.head_sha // "") == $pr.head_sha)] as $mine
			| select(($mine | length) > 0)
			| select(all($mine[]; (.status // "") == "completed"))
			| select(any($mine[]; (.conclusion // "") == "success") | not)
			| ([$mine[] | select((.conclusion // "") == "failure" and (((.display_title // "") | endswith(" [sync-quick]")) | not)) | (.updated_at // "")]
				| map(select(test("^[0-9]{4}-"))) | max) as $failed_at
			| select($failed_at != null)
			| "\(.number) \(.head_sha) \(.head_ref // "-") \($failed_at)"]
		| .[]' "${FINAL_CI_SWEEP_PR_SNAPSHOT}" 2>/dev/null || true)"
	[ -n "${failed_candidates}" ] && base_sync_queue+="${failed_candidates}"$'\n'
	FINAL_CI_SWEEP_LISTING_FILE="$(mktemp "${TMPDIR:-/tmp}/final_ci_sweep_listing.XXXXXX" 2>/dev/null || echo "")"
	if [ -z "${FINAL_CI_SWEEP_LISTING_FILE}" ] || ! printf '%s' "${listing}" > "${FINAL_CI_SWEEP_LISTING_FILE}"; then
		echo "REVIEW_FINAL_CI_SWEEP_BASE_SYNC outcome=skip reason=tmp_failed"
		return 0
	fi
	# Read by the pr_checks_lib.sh helpers the base-sync pass calls.
	# shellcheck disable=SC2034
	PR_CHECKS_REPOSITORY="${REPOSITORY}"
	# Oldest failure first, so a long-red PR is not starved by newer ones; a
	# candidate that stopped before any API call (return 2) is not counted.
	while read -r number sha ref failed_at; do
		[ -n "${number:-}" ] || continue
		[ "${base_sync_checked}" -lt "${max}" ] || break
		review_final_ci_sweep_base_sync "${number}" "${sha}" "${ref}" "${failed_at}"
		[ "$?" -eq 2 ] || base_sync_checked=$((base_sync_checked + 1))
	done < <(printf '%s' "${base_sync_queue}" | grep . | sort -k4,4)
	rm -f "${FINAL_CI_SWEEP_LISTING_FILE}"
	echo "REVIEW_FINAL_CI_SWEEP_BASE_SYNC outcome=done candidates=$(printf '%s' "${base_sync_queue}" | grep -c .) checked=${base_sync_checked} cap=${max}"
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	review_final_ci_sweep_main
fi
