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
# Fail-open: a failed listing or status read skips that PR this tick.
# Logs: one `REVIEW_FINAL_CI_SWEEP outcome=... ` summary line, plus the
# helper's `REVIEW_FINAL_CI` lines.

set -uo pipefail

FINAL_CI_SWEEP_SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

review_final_ci_sweep_main()
{
	local helpers_dir wf max listing candidates count checked=0 ensured=0 skipped_gate=0 number sha ref state
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
			| select(.head_sha as $s | ($covered | index($s)) == null)]
		| .[] | "\(.number) \(.head_sha) \(.head_ref // "")"' "${FINAL_CI_SWEEP_PR_SNAPSHOT}" 2>/dev/null || true)"
	count=0
	[ -n "${candidates}" ] && count="$(printf '%s\n' "${candidates}" | grep -c .)"
	while read -r number sha ref; do
		[ -n "${number:-}" ] || continue
		[ "${checked}" -lt "${max}" ] || break
		checked=$((checked + 1))
		state="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/commits/${sha}/status" \
			--jq '[.statuses[]? | select(.context == "ai-review/head-gate") | .state] | first // "absent"' 2>/dev/null || echo "unknown")"
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
	done <<< "${candidates}"
	echo "REVIEW_FINAL_CI_SWEEP outcome=done candidates=${count} checked=${checked} gate_not_success=${skipped_gate} ensured=${ensured} cap=${max}"
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	review_final_ci_sweep_main
fi
