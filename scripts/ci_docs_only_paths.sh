#!/usr/bin/env bash
# Docs-only final CI (run-volume plan item 4,
# docs/plans/reduce-actions-run-volume-plan.md, Refs #9528).
#
# One list of paths that no test job and no CI step reads. A PR whose changed
# files are all on it gets a final CI run (ci.yml workflow_dispatch) with
# `docs_only=true`: `static-checks` and `lint` run, the test jobs are skipped.
#
#   ci_docs_only_paths.sh classify
#       Reads NUL-separated repository paths on stdin and prints one line:
#       `docs_only` when there is at least one path and every path is on the
#       list, otherwise `code <reason>` (`empty`, or the first path that is
#       not on the list, shell-quoted with %q so the line never splits).
#       Always exits 0.
#   ci_docs_only_paths.sh verify
#       Run by ci.yml's `lint` job in a docs-only run, on a checkout of
#       refs/pull/<n>/merge with fetch-depth 2. Passes only when HEAD is a
#       merge whose second parent is CI_DOCS_ONLY_HEAD_SHA (the head the run's
#       check-runs attach to) and every file the merge changes against its
#       first parent (the base) is on the list. Anything else, including a git
#       failure, exits 1, so a wrong `docs_only=true` input fails `lint`.
#
# Log: `CI_DOCS_ONLY_GATE mode=classify|verify outcome=docs_only|code|pass|fail reason=<token>`
# (classify logs to stderr; stdout carries only the result line).
#
# The list (keep in step with docs/plans/reduce-actions-run-volume-plan.md):
#   analysis/**, docs/plans/**, docs/postmortems/**, docs/completed/**,
#   docs/deploy-activation/**, docs/triage/**, docs/how-it-works.md,
#   docs/hardenaireviewprompt.md, docs/changelog-style.md,
#   docs/*-future-improvements.md (top level of docs/ only).

set -uo pipefail

ci_docs_only_path_is_safe()
{
	local candidate_path="$1"
	case "${candidate_path}" in
		""|/*|../*|*/../*|*/..|./*|*/./*) return 1 ;;
	esac
	case "${candidate_path}" in
		analysis/?*|docs/plans/?*|docs/postmortems/?*|docs/completed/?*|docs/deploy-activation/?*|docs/triage/?*)
			return 0
			;;
		docs/how-it-works.md|docs/hardenaireviewprompt.md|docs/changelog-style.md)
			return 0
			;;
		docs/*/*)
			return 1
			;;
		docs/?*-future-improvements.md)
			return 0
			;;
	esac
	return 1
}

ci_docs_only_classify_stdin()
{
	local candidate_path seen_paths=0
	# A last path without a trailing NUL still counts (read returns non-zero).
	while IFS= read -r -d '' candidate_path || [ -n "${candidate_path}" ]; do
		seen_paths=$((seen_paths + 1))
		if ! ci_docs_only_path_is_safe "${candidate_path}"; then
			echo "CI_DOCS_ONLY_GATE mode=classify outcome=code reason=path_not_listed paths_read=${seen_paths}" >&2
			# %q keeps the verdict on one line even for a path with a newline.
			printf 'code %q\n' "${candidate_path}"
			return 0
		fi
	done
	if [ "${seen_paths}" -eq 0 ]; then
		echo "CI_DOCS_ONLY_GATE mode=classify outcome=code reason=empty" >&2
		printf 'code empty\n'
		return 0
	fi
	echo "CI_DOCS_ONLY_GATE mode=classify outcome=docs_only reason=all_listed paths=${seen_paths}" >&2
	printf 'docs_only\n'
	return 0
}

ci_docs_only_verify_merge()
{
	local expected_head="${CI_DOCS_ONLY_HEAD_SHA:-}" merge_parents="" second_parent="" changed_list="" verdict=""
	local -a merge_parent_list=()
	if ! [[ "${expected_head}" =~ ^[0-9a-f]{40}$ ]]; then
		echo "CI_DOCS_ONLY_GATE mode=verify outcome=fail reason=head_sha_invalid"
		echo "::error::Docs-only final CI: CI_DOCS_ONLY_HEAD_SHA is not a commit SHA."
		return 1
	fi
	if ! merge_parents="$(git rev-list --parents -n 1 HEAD 2>/dev/null)"; then
		echo "CI_DOCS_ONLY_GATE mode=verify outcome=fail reason=merge_unreadable"
		echo "::error::Docs-only final CI: the checked-out merge commit could not be read."
		return 1
	fi
	read -r -a merge_parent_list <<< "${merge_parents}"
	if [ "${#merge_parent_list[@]}" -ne 3 ]; then
		echo "CI_DOCS_ONLY_GATE mode=verify outcome=fail reason=not_a_merge parents=$(( ${#merge_parent_list[@]} - 1 ))"
		echo "::error::Docs-only final CI: HEAD is not the PR's merge commit, so the changed files cannot be checked."
		return 1
	fi
	second_parent="${merge_parent_list[2]}"
	if [ "${second_parent}" != "${expected_head}" ]; then
		echo "CI_DOCS_ONLY_GATE mode=verify outcome=fail reason=head_moved merge_head=${second_parent} run_head=${expected_head}"
		echo "::error::Docs-only final CI: the merge ref tests ${second_parent}, not the head this run reports on (${expected_head})."
		return 1
	fi
	changed_list="$(mktemp "${TMPDIR:-/tmp}/ci_docs_only_paths.XXXXXX")" || {
		echo "CI_DOCS_ONLY_GATE mode=verify outcome=fail reason=mktemp_failed"
		echo "::error::Docs-only final CI: could not create a temp file."
		return 1
	}
	# --no-renames lists both sides of a rename, so moving a file onto the
	# list still names the path it left.
	if ! git diff --no-renames --name-only -z "${merge_parent_list[1]}" HEAD > "${changed_list}" 2>/dev/null; then
		rm -f "${changed_list}"
		echo "CI_DOCS_ONLY_GATE mode=verify outcome=fail reason=diff_failed"
		echo "::error::Docs-only final CI: git diff against the base failed."
		return 1
	fi
	verdict="$(ci_docs_only_classify_stdin < "${changed_list}" 2>/dev/null)"
	rm -f "${changed_list}"
	if [ "${verdict}" != "docs_only" ]; then
		echo "CI_DOCS_ONLY_GATE mode=verify outcome=fail reason=code_changed first=${verdict#code }"
		echo "::error::Docs-only final CI: the PR changes ${verdict#code }, which is not on the docs-only list, but the test jobs were skipped."
		return 1
	fi
	echo "CI_DOCS_ONLY_GATE mode=verify outcome=pass reason=all_listed head=${expected_head}"
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	case "${1:-}" in
		classify) ci_docs_only_classify_stdin ;;
		verify) ci_docs_only_verify_merge ;;
		*)
			echo "usage: ci_docs_only_paths.sh classify|verify" >&2
			exit 2
			;;
	esac
fi
