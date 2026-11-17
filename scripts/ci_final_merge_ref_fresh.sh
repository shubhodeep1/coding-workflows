#!/usr/bin/env bash
# Final-CI merge-ref freshness check (final-CI self-heal, 2026-11-17).
#
# A final CI run (ci.yml `workflow_dispatch`, cut-ci-runner-time Phase 4)
# checks out `refs/pull/<n>/merge` in every job. GitHub refreshes that ref
# lazily after the base moves, so a job can test the head merged into an
# older base tip: PR #9602's run on 2026-11-17 07:21 UTC tested a merge with
# base commit df30f80a although the base had been at its fix 8a1664a5 (push
# CI green) since 05:37, and failed on tests the base had already fixed. The
# shallow-checkout steps only checked the merge's second parent (the PR head).
#
# Runs as the step right after each final-CI checkout. It compares the
# checked-out merge commit's first parent with the base branch's live tip
# (`git ls-remote`, no API call):
#   - equal: logs `outcome=fresh` and exits 0;
#   - different: up to CI_FINAL_MERGE_REF_REFRESH_ATTEMPTS times
#     (default 6, 1-20), CI_FINAL_MERGE_REF_REFRESH_SECONDS apart (default 10,
#     1-60), reads `pulls/<n>` (which makes GitHub recompute the merge ref),
#     re-fetches the merge ref and re-reads the tip. When the new merge
#     commit still merges the dispatched head (GITHUB_SHA) and its first
#     parent is a base tip seen during this step, the job checks it out
#     (`outcome=refreshed`) and continues on the current combination;
#   - still stale after the attempts, or GitHub reports the PR as not
#     mergeable (a conflict, so no merge ref can be computed): `::error::` and
#     exit 1 (`outcome=stale` / `outcome=conflict`). The red `lint` sends the
#     PR back through `final-ci-failed`, and the review sweep's base sync
#     (scripts/review_final_ci_sweep.sh) updates a head that is behind.
# Fail-open: not a final-CI run, the switch off, an unreadable base branch,
# tip or commit, a merge ref that could not be re-fetched at all (e.g. a
# private repository and a checkout without persisted credentials), or a head
# that moved (the run is superseded anyway) logs and exits 0, so the job tests
# what it checked out, as before this check.
#
# Usage: ci_final_merge_ref_fresh.sh [--depth <n>]   (fetch depth of the
# job's checkout, default 1, 1-50; the re-fetch keeps the same depth)
# Env: FINAL_CI_PR_NUMBER (empty = not a final-CI run, no-op), GITHUB_SHA,
# GITHUB_REPOSITORY, GH_TOKEN (optional: without it the PR is read from the
# public REST API unauthenticated; CI_FINAL_MERGE_REF_API_URL overrides the
# API base for tests), CI_FINAL_MERGE_REF_FRESH_ENABLED (default true;
# true/1/yes/on), the two refresh knobs above.
# API budget (§15): one `GET pulls/<n>` for the base branch name (the job's
# shallow-checkout step reads the same PR later for its own exports; the
# check must run before that step, so it cannot reuse that read), plus one
# `GET pulls/<n>` per refresh attempt, only when the merge ref is stale.
# Log: `CI_FINAL_MERGE_REF pr=<n> head=<sha> outcome=fresh|refreshed|stale|conflict|skipped reason=<token> [base=<ref>] [tested_base=<sha>] [tip=<sha>]`.

set -uo pipefail

ci_final_merge_ref_log()
{
	echo "CI_FINAL_MERGE_REF pr=${FINAL_CI_PR_NUMBER:-?} head=${GITHUB_SHA:-?} outcome=$1 reason=$2${3:+ $3}"
}

# `ci_final_merge_ref_parents <rev>` prints "<first> <second>" from the raw
# commit object: in a depth-1 clone `git rev-parse <rev>^1` cannot see a
# parent that was not fetched, but the commit object still names it.
ci_final_merge_ref_parents()
{
	git cat-file -p "$1" 2>/dev/null | awk '/^$/ { exit } /^parent / { p = p (p == "" ? "" : " ") $2 } END { print p }'
}

# `ci_final_merge_ref_pull <jq filter>`: one `GET pulls/<n>` through `gh api`.
# A job that gets no token (tests-sandbox-python passes none, see
# tests/test_sandbox_python_coverage.py) reads the public REST endpoint
# without credentials instead. Prints nothing and returns 1 on failure.
ci_final_merge_ref_pull()
{
	local filter="$1" pull_json=""
	if [ -n "${GH_TOKEN:-}" ] && gh api "repos/${GITHUB_REPOSITORY}/pulls/${FINAL_CI_PR_NUMBER}" --jq "${filter}" 2>/dev/null; then
		return 0
	fi
	pull_json="$(curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" \
		"${CI_FINAL_MERGE_REF_API_URL:-https://api.github.com}/repos/${GITHUB_REPOSITORY}/pulls/${FINAL_CI_PR_NUMBER}" 2>/dev/null)" || return 1
	printf '%s' "${pull_json}" | jq -r "${filter}" 2>/dev/null
}

ci_final_merge_ref_tip()
{
	git ls-remote origin "refs/heads/$1" 2>/dev/null | awk 'NR == 1 { print $1 }'
}

ci_final_merge_ref_main()
{
	local depth=1 switch attempts sleep_s base_ref tip first second seen_tips attempt mergeable fetched fetched_any=false
	while [ "$#" -gt 0 ]; do
		case "$1" in
			--depth) depth="${2:-}"; shift 2 ;;
			*) shift ;;
		esac
	done
	{ [[ "${depth}" =~ ^[0-9]{1,2}$ ]] && [ "$((10#${depth}))" -ge 1 ] && [ "$((10#${depth}))" -le 50 ]; } || depth=1
	depth=$((10#${depth}))
	if [ -z "${FINAL_CI_PR_NUMBER:-}" ]; then
		ci_final_merge_ref_log skipped not_final_ci
		return 0
	fi
	switch="$(printf '%s' "${CI_FINAL_MERGE_REF_FRESH_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
	case "${switch}" in
		true|1|yes|on) ;;
		*)
			ci_final_merge_ref_log skipped disabled
			return 0
			;;
	esac
	if ! [[ "${FINAL_CI_PR_NUMBER}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${GITHUB_SHA:-}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${GITHUB_REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
		ci_final_merge_ref_log skipped invalid_input
		return 0
	fi
	attempts="${CI_FINAL_MERGE_REF_REFRESH_ATTEMPTS:-6}"
	{ [[ "${attempts}" =~ ^[0-9]{1,2}$ ]] && [ "$((10#${attempts}))" -ge 1 ] && [ "$((10#${attempts}))" -le 20 ]; } || attempts=6
	attempts=$((10#${attempts}))
	sleep_s="${CI_FINAL_MERGE_REF_REFRESH_SECONDS:-10}"
	{ [[ "${sleep_s}" =~ ^[0-9]{1,2}$ ]] && [ "$((10#${sleep_s}))" -ge 1 ] && [ "$((10#${sleep_s}))" -le 60 ]; } || sleep_s=10
	sleep_s=$((10#${sleep_s}))

	read -r first second <<< "$(ci_final_merge_ref_parents HEAD)"
	if ! [[ "${first:-}" =~ ^[0-9a-f]{40}$ ]] || [ "${second:-}" != "${GITHUB_SHA}" ]; then
		# Not a two-parent merge of the dispatched head: the shallow-checkout
		# steps fail such a checkout with their own message.
		ci_final_merge_ref_log skipped not_a_merge_of_the_head "tested_base=${first:-none}"
		return 0
	fi
	base_ref="$(ci_final_merge_ref_pull '.base.ref // ""' || true)"
	if ! [[ "${base_ref}" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "${base_ref}" == *..* ]] || [[ "${base_ref}" == -* ]]; then
		echo "::warning::CI_FINAL_MERGE_REF: could not read the base branch of PR #${FINAL_CI_PR_NUMBER}; testing the checked-out merge ref unverified."
		ci_final_merge_ref_log skipped base_ref_unreadable "tested_base=${first}"
		return 0
	fi
	tip="$(ci_final_merge_ref_tip "${base_ref}")"
	if ! [[ "${tip}" =~ ^[0-9a-f]{40}$ ]]; then
		echo "::warning::CI_FINAL_MERGE_REF: could not read the tip of ${base_ref}; testing the checked-out merge ref unverified."
		ci_final_merge_ref_log skipped tip_unreadable "base=${base_ref} tested_base=${first}"
		return 0
	fi
	if [ "${first}" = "${tip}" ]; then
		ci_final_merge_ref_log fresh base_tip "base=${base_ref} tested_base=${first}"
		return 0
	fi
	echo "CI_FINAL_MERGE_REF: refs/pull/${FINAL_CI_PR_NUMBER}/merge merges ${GITHUB_SHA} into ${first}, but ${base_ref} is at ${tip}; asking GitHub to recompute the merge ref."
	seen_tips=" ${tip} "
	for (( attempt = 1; attempt <= attempts; attempt++ )); do
		mergeable="$(ci_final_merge_ref_pull '.mergeable | tostring' || true)"
		[ -n "${mergeable}" ] || mergeable="unknown"
		if [ "${mergeable}" = "false" ]; then
			echo "::error::CI_FINAL_MERGE_REF: PR #${FINAL_CI_PR_NUMBER} no longer merges cleanly into ${base_ref} (${tip}); GitHub cannot refresh refs/pull/${FINAL_CI_PR_NUMBER}/merge. Resolve the conflict with the base; this is not a test failure."
			ci_final_merge_ref_log conflict not_mergeable "base=${base_ref} tested_base=${first} tip=${tip}"
			return 1
		fi
		sleep "${sleep_s}"
		fetched=false
		if git fetch --no-tags --quiet --depth="${depth}" origin "+refs/pull/${FINAL_CI_PR_NUMBER}/merge:refs/remotes/pull/${FINAL_CI_PR_NUMBER}/merge" 2>/dev/null; then
			fetched=true
			fetched_any=true
		fi
		tip="$(ci_final_merge_ref_tip "${base_ref}")"
		[[ "${tip}" =~ ^[0-9a-f]{40}$ ]] && seen_tips+="${tip} "
		[ "${fetched}" = "true" ] || continue
		read -r first second <<< "$(ci_final_merge_ref_parents "refs/remotes/pull/${FINAL_CI_PR_NUMBER}/merge")"
		if [ "${second:-}" != "${GITHUB_SHA}" ]; then
			# The PR head moved: a newer final run replaces this one.
			ci_final_merge_ref_log skipped head_moved "base=${base_ref} merged_head=${second:-none}"
			return 0
		fi
		if [[ "${first:-}" =~ ^[0-9a-f]{40}$ ]] && [[ "${seen_tips}" == *" ${first} "* ]]; then
			if git checkout --quiet --detach --force "refs/remotes/pull/${FINAL_CI_PR_NUMBER}/merge"; then
				ci_final_merge_ref_log refreshed base_tip "base=${base_ref} tested_base=${first} attempt=${attempt}"
				return 0
			fi
			echo "::warning::CI_FINAL_MERGE_REF: could not check out the refreshed merge ref; retrying."
		fi
	done
	if [ "${fetched_any}" != "true" ]; then
		# No re-fetch worked (e.g. a private repository and a checkout without
		# persisted credentials): staleness is known but cannot be fixed here,
		# so warn and test the checkout rather than fail a run that may pass.
		echo "::warning::CI_FINAL_MERGE_REF: refs/pull/${FINAL_CI_PR_NUMBER}/merge merges into ${first}, not the tip ${tip:-unknown} of ${base_ref}, and could not be re-fetched; testing the checked-out merge ref."
		ci_final_merge_ref_log skipped fetch_unavailable "base=${base_ref} tested_base=${first} tip=${tip:-none}"
		return 0
	fi
	echo "::error::CI_FINAL_MERGE_REF: refs/pull/${FINAL_CI_PR_NUMBER}/merge still merges into ${first:-unknown}, not the current tip ${tip:-unknown} of ${base_ref}, after ${attempts} refresh attempt(s). This run would test an old base, so it stops here; this is not a defect in the PR. The review sweep updates a head that is behind its base, and the next final run tests the current base."
	ci_final_merge_ref_log stale not_refreshed "base=${base_ref} tested_base=${first:-none} tip=${tip:-none}"
	return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	ci_final_merge_ref_main "$@"
fi
