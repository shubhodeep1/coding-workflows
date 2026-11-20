#!/usr/bin/env bash
# Sync-quick final CI for merge-base freshness updates (merge-base sync churn,
# 2026-11-17; operator decision Q3: B).
#
# When the merge-base freshness gate (scripts/pr_checks_lib.sh
# _pr_base_fresh_for_merge) updates an approved PR from its base, the update
# head needs a CI run on the combined tree before it merges. When the base
# commits overlap the PR only in CI-wiring files (the list below) and the
# approved head already passed a full final CI run, the update head gets a
# quick run instead: static-checks, the PR's own test files and the tests that
# pin ci.yml's structure. The full suite still runs on `main` after the merge.
# A base move that touches no PR file merges with no CI at all, so the quick
# run is strictly more checking than that case gets.
#
#   ci_sync_quick.sh classify
#       Reads repository paths on stdin (NUL- or newline-separated) and prints
#       one line: `ci_wiring_only` when there is at least one path and every
#       path is on the list, otherwise `other <path>` (`other empty` for no
#       paths; %q-quoted). Always exits 0.
#   ci_sync_quick.sh full-ci-green <repo> <pr> <sha> [<workflow file>]
#       Exits 0 when <sha> has a successful full final CI run of <workflow
#       file> (default ci.yml; the dispatcher passes REVIEW_FINAL_CI_WORKFLOW)
#       for <pr>: event workflow_dispatch, run name exactly `CI [pr:<pr>]` (a
#       sync-quick run is named `CI [pr:<pr>] [sync-quick]` and never counts).
#       One `actions/workflows/ci.yml/runs?head_sha=` read. Any read failure
#       exits 1 (not green).
#   ci_sync_quick.sh verify
#       Run by ci.yml's tests-sync-quick job on a checkout of
#       refs/pull/<n>/merge with fetch-depth 3. Passes only when:
#         - CI_SYNC_QUICK_DOCS_ONLY is not `true` (the two modes never mix);
#         - HEAD is a merge whose second parent is CI_SYNC_QUICK_HEAD_SHA (the
#           update head the run's check-runs attach to);
#         - that update head is a two-parent merge [approved head, base tip];
#         - every path both the PR (base tip..update head) and the base
#           (approved head..update head) changed is on the list;
#         - the approved head passed a full final CI run (full-ci-green with
#           CI_SYNC_QUICK_REPOSITORY and CI_SYNC_QUICK_PR_NUMBER).
#       Anything else, including a git or API failure, exits 1, so a wrong
#       `sync_quick=true` input fails the job and with it `lint`.
#   ci_sync_quick.sh run-tests
#       After verify, on the same checkout: runs the PR's changed
#       tests/test_*.py files (pytest) and tests/test_*.sh files (bash) that
#       exist in the tree, plus the CI-structure tests listed below.
#
# The CI-wiring list (paths whose change on the base cannot change what the
# PR's own code does): .github/workflows/ci.yml and the tests that pin ci.yml
# (tests/test_ci_wires_every_test_file.py, tests/test_ci_job_split_contract.py,
# tests/test_ci_poll_test_sharding.py, tests/test_ci_docs_only_final_ci.py,
# tests/test_ci_sync_quick.py).
#
# Log: `CI_SYNC_QUICK mode=classify|full_ci_green|verify|run_tests outcome=<word> reason=<token>`
# (classify logs to stderr; stdout carries only the result line).

set -uo pipefail

CI_SYNC_QUICK_WIRING_PATHS=(
	".github/workflows/ci.yml"
	"tests/test_ci_wires_every_test_file.py"
	"tests/test_ci_job_split_contract.py"
	"tests/test_ci_poll_test_sharding.py"
	"tests/test_ci_docs_only_final_ci.py"
	"tests/test_ci_sync_quick.py"
)

ci_sync_quick_path_is_wiring()
{
	local candidate_path="$1" listed_path
	for listed_path in "${CI_SYNC_QUICK_WIRING_PATHS[@]}"; do
		[ "${candidate_path}" = "${listed_path}" ] && return 0
	done
	return 1
}

ci_sync_quick_classify_stdin()
{
	local candidate_path seen_paths=0
	# NUL or newline separated; a last path without a separator still counts.
	while IFS= read -r candidate_path || [ -n "${candidate_path}" ]; do
		[ -n "${candidate_path}" ] || continue
		seen_paths=$((seen_paths + 1))
		if ! ci_sync_quick_path_is_wiring "${candidate_path}"; then
			echo "CI_SYNC_QUICK mode=classify outcome=other reason=path_not_listed paths_read=${seen_paths}" >&2
			printf 'other %q\n' "${candidate_path}"
			return 0
		fi
	done < <(tr '\0' '\n')
	if [ "${seen_paths}" -eq 0 ]; then
		echo "CI_SYNC_QUICK mode=classify outcome=other reason=empty" >&2
		printf 'other empty\n'
		return 0
	fi
	echo "CI_SYNC_QUICK mode=classify outcome=ci_wiring_only reason=all_listed paths=${seen_paths}" >&2
	printf 'ci_wiring_only\n'
	return 0
}

ci_sync_quick_full_ci_green()
{
	local green_repo="${1:-}" green_pr="${2:-}" green_sha="${3:-}" green_wf="${4:-ci.yml}" green_runs="" green_count=""
	if ! [[ "${green_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${green_repo}" == *..* ]] \
		|| ! [[ "${green_wf}" =~ ^[A-Za-z0-9._-]+\.ya?ml$ ]] \
		|| ! [[ "${green_pr}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${green_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		echo "CI_SYNC_QUICK mode=full_ci_green outcome=fail reason=invalid_input"
		return 1
	fi
	if ! green_runs="$(gh api "repos/${green_repo}/actions/workflows/${green_wf}/runs?head_sha=${green_sha}&per_page=50" 2>/dev/null)"; then
		echo "CI_SYNC_QUICK mode=full_ci_green outcome=fail reason=read_failed sha=${green_sha}"
		return 1
	fi
	green_count="$(printf '%s' "${green_runs}" | jq -r --arg sha "${green_sha}" --arg title "CI [pr:${green_pr}]" --arg path ".github/workflows/${green_wf}" '
		if (type == "object" and (.workflow_runs | type) == "array") then
			[.workflow_runs[] | select(type == "object")
				| select(((.path // "") | split("@") | .[0]) == $path)
				| select((.head_sha // "") == $sha and (.event // "") == "workflow_dispatch")
				| select((.display_title // "") == $title and (.status // "") == "completed" and (.conclusion // "") == "success")]
			| length
		else "invalid" end' 2>/dev/null | tail -n1)"
	if ! [[ "${green_count}" =~ ^[0-9]+$ ]]; then
		echo "CI_SYNC_QUICK mode=full_ci_green outcome=fail reason=invalid_response sha=${green_sha}"
		return 1
	fi
	if [ "${green_count}" -eq 0 ]; then
		echo "CI_SYNC_QUICK mode=full_ci_green outcome=fail reason=no_full_green_run sha=${green_sha}"
		return 1
	fi
	echo "CI_SYNC_QUICK mode=full_ci_green outcome=pass reason=full_green_run sha=${green_sha} runs=${green_count}"
	return 0
}

ci_sync_quick_fail()
{
	echo "CI_SYNC_QUICK mode=verify outcome=fail reason=$1${2:+ $2}"
	echo "::error::Sync-quick final CI: $3 The test jobs were skipped on the dispatcher's word, so this run fails."
	return 1
}

# Sets CI_SYNC_QUICK_UPDATE_HEAD, CI_SYNC_QUICK_APPROVED_HEAD and
# CI_SYNC_QUICK_BASE_TIP from the checked-out merge; returns 1 (logged) when
# the shape is wrong.
ci_sync_quick_read_merge()
{
	local expected_head="${CI_SYNC_QUICK_HEAD_SHA:-}" merge_parents update_parents
	local -a merge_parent_list=() update_parent_list=()
	CI_SYNC_QUICK_UPDATE_HEAD=""
	CI_SYNC_QUICK_APPROVED_HEAD=""
	CI_SYNC_QUICK_BASE_TIP=""
	if ! [[ "${expected_head}" =~ ^[0-9a-f]{40}$ ]]; then
		ci_sync_quick_fail head_sha_invalid "" "CI_SYNC_QUICK_HEAD_SHA is not a commit SHA."
		return 1
	fi
	if ! merge_parents="$(git rev-list --parents -n 1 HEAD 2>/dev/null)"; then
		ci_sync_quick_fail merge_unreadable "" "the checked-out merge commit could not be read."
		return 1
	fi
	read -r -a merge_parent_list <<< "${merge_parents}"
	if [ "${#merge_parent_list[@]}" -ne 3 ]; then
		ci_sync_quick_fail not_a_merge "parents=$(( ${#merge_parent_list[@]} - 1 ))" "HEAD is not the PR's merge commit."
		return 1
	fi
	if [ "${merge_parent_list[2]}" != "${expected_head}" ]; then
		ci_sync_quick_fail head_moved "merge_head=${merge_parent_list[2]} run_head=${expected_head}" \
			"the merge ref tests ${merge_parent_list[2]}, not the head this run reports on (${expected_head})."
		return 1
	fi
	if ! update_parents="$(git rev-list --parents -n 1 "${expected_head}" 2>/dev/null)"; then
		ci_sync_quick_fail update_unreadable "" "the update head ${expected_head} could not be read."
		return 1
	fi
	read -r -a update_parent_list <<< "${update_parents}"
	if [ "${#update_parent_list[@]}" -ne 3 ]; then
		ci_sync_quick_fail update_not_a_merge "parents=$(( ${#update_parent_list[@]} - 1 ))" \
			"the head ${expected_head} is not a merge of an approved head with its base."
		return 1
	fi
	CI_SYNC_QUICK_UPDATE_HEAD="${expected_head}"
	CI_SYNC_QUICK_APPROVED_HEAD="${update_parent_list[1]}"
	CI_SYNC_QUICK_BASE_TIP="${update_parent_list[2]}"
	return 0
}

ci_sync_quick_verify()
{
	local pr_paths base_paths overlap verdict
	if [ "${CI_SYNC_QUICK_DOCS_ONLY:-false}" = "true" ]; then
		ci_sync_quick_fail mixed_modes "" "docs_only and sync_quick were both requested."
		return 1
	fi
	ci_sync_quick_read_merge || return 1
	if ! pr_paths="$(git diff --no-renames --name-only "${CI_SYNC_QUICK_BASE_TIP}" "${CI_SYNC_QUICK_UPDATE_HEAD}" 2>/dev/null)" \
		|| ! base_paths="$(git diff --no-renames --name-only "${CI_SYNC_QUICK_APPROVED_HEAD}" "${CI_SYNC_QUICK_UPDATE_HEAD}" 2>/dev/null)"; then
		ci_sync_quick_fail diff_failed "" "git diff of the update head against its parents failed."
		return 1
	fi
	overlap="$(comm -12 <(printf '%s\n' "${pr_paths}" | sed '/^$/d' | LC_ALL=C sort -u) <(printf '%s\n' "${base_paths}" | sed '/^$/d' | LC_ALL=C sort -u))"
	if [ -n "${overlap}" ]; then
		verdict="$(printf '%s\n' "${overlap}" | ci_sync_quick_classify_stdin 2>/dev/null)"
		if [ "${verdict}" != "ci_wiring_only" ]; then
			ci_sync_quick_fail overlap_not_ci_wiring "first=${verdict#other }" \
				"the base and the PR both changed ${verdict#other }, which is not a CI-wiring file."
			return 1
		fi
	fi
	if ! ci_sync_quick_full_ci_green "${CI_SYNC_QUICK_REPOSITORY:-}" "${CI_SYNC_QUICK_PR_NUMBER:-}" "${CI_SYNC_QUICK_APPROVED_HEAD}"; then
		ci_sync_quick_fail approved_head_not_green "approved_head=${CI_SYNC_QUICK_APPROVED_HEAD}" \
			"the approved head ${CI_SYNC_QUICK_APPROVED_HEAD} has no successful full final CI run."
		return 1
	fi
	echo "CI_SYNC_QUICK mode=verify outcome=pass reason=ci_wiring_overlap head=${CI_SYNC_QUICK_UPDATE_HEAD} approved_head=${CI_SYNC_QUICK_APPROVED_HEAD} overlap=$(printf '%s' "${overlap:-none}" | tr '\n' ',')"
	return 0
}

ci_sync_quick_run_tests()
{
	local changed_path listed_path rc=0
	local -a py_tests=() sh_tests=()
	local -A seen_tests=()
	ci_sync_quick_read_merge || return 1
	while IFS= read -r changed_path; do
		case "${changed_path}" in
			tests/test_*.py|tests/test_*.sh) ;;
			*) continue ;;
		esac
		[ -f "${changed_path}" ] && [ -z "${seen_tests[${changed_path}]:-}" ] || continue
		seen_tests["${changed_path}"]=1
		case "${changed_path}" in
			*.py) py_tests+=("${changed_path}") ;;
			*.sh) sh_tests+=("${changed_path}") ;;
		esac
	done < <(git diff --no-renames --name-only --diff-filter=d "${CI_SYNC_QUICK_BASE_TIP}" "${CI_SYNC_QUICK_UPDATE_HEAD}" 2>/dev/null)
	for listed_path in "${CI_SYNC_QUICK_WIRING_PATHS[@]}"; do
		case "${listed_path}" in
			tests/test_*.py) ;;
			*) continue ;;
		esac
		[ -f "${listed_path}" ] && [ -z "${seen_tests[${listed_path}]:-}" ] || continue
		seen_tests["${listed_path}"]=1
		py_tests+=("${listed_path}")
	done
	echo "CI_SYNC_QUICK mode=run_tests outcome=start reason=selected python=${#py_tests[@]} shell=${#sh_tests[@]}"
	if [ "${#py_tests[@]}" -gt 0 ]; then
		PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider "${py_tests[@]}" || rc=1
	fi
	for listed_path in "${sh_tests[@]}"; do
		bash "${listed_path}" || rc=1
	done
	if [ "${rc}" -ne 0 ]; then
		echo "CI_SYNC_QUICK mode=run_tests outcome=fail reason=tests_failed"
		return 1
	fi
	echo "CI_SYNC_QUICK mode=run_tests outcome=pass reason=tests_passed"
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	case "${1:-}" in
		classify) ci_sync_quick_classify_stdin ;;
		full-ci-green) shift; ci_sync_quick_full_ci_green "$@" ;;
		verify) ci_sync_quick_verify ;;
		run-tests) ci_sync_quick_run_tests ;;
		*)
			echo "usage: ci_sync_quick.sh classify | full-ci-green <repo> <pr> <sha> | verify | run-tests" >&2
			exit 2
			;;
	esac
fi
