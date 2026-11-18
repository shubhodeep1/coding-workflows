#!/usr/bin/env bash
# Retarget after a merged base (port P6, docs/plans/replace-claude-sessions-with-cli-engine-plan.md
# Phase 8d).
#
# A branch whose pull request has merged is finished: work stacked on it must
# go to that PR's base instead. GitHub retargets stacked PRs only when the
# merged branch is deleted; this covers the case where it is kept.
#
# Usage:
#   retarget_merged_base.sh resolve <owner/repo> <branch> <default_branch>
#       Print the branch new work should target: <branch> itself, or the base
#       of the merged PR whose head it is (followed for up to
#       RETARGET_MERGED_BASE_MAX_HOPS hops). implement.yml uses it for the
#       issue's integration branch before checkout.
#   retarget_merged_base.sh pr <owner/repo> <pr_number> <base_branch> <default_branch>
#       Same lookup for an open PR's base; when it changes, retarget the PR
#       with one PATCH and print the new base (else the current one). The
#       review gate passes the base it already fetched.
#
# An existing branch counts as merged only when a merged PR has it as its
# head AND its tip is still that PR's head commit, so a reused branch name
# is never retargeted. A confirmed 404 for that ref means it was deleted;
# the merged PR's base is then the target for new work. The default branch
# is never looked up (no API call when the work is not stacked).
#
# API budget (CLAUDE.md §15) per hop, only for a non-default branch: one
# closed-PR search by head, one ref read; plus one PATCH in `pr` mode when the
# base changes. Every failure is fail-open: the input branch is printed, a
# warning is logged, and the exit code is 0.
#
# Env: RETARGET_MERGED_BASE_ENABLED (default true; `false` prints the input
# unchanged), RETARGET_MERGED_BASE_MAX_HOPS (default 3), GH_TOKEN for gh.
# Log: RETARGET_MERGED_BASE mode= repo= pr= from= to= merged_pr= outcome= reason=
set -uo pipefail

retarget_log()
{
	echo "RETARGET_MERGED_BASE $*" >&2
}

retarget_urlencode()
{
	python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

# Prints "<merged_pr_number> <base_ref>" for the merged PR whose head is
# <branch> and whose head commit is still the branch tip; prints nothing
# otherwise. Returns 1 when the lookup failed.
retarget_lookup_merged()
{
	local repo="$1" branch="$2"
	local owner="${repo%%/*}" head_query merged_json number base head_sha tip tip_error_file tip_error
	head_query="$(retarget_urlencode "${owner}:${branch}")" || return 1
	if ! merged_json="$(gh api "repos/${repo}/pulls?state=closed&head=${head_query}&per_page=20" \
		--jq '[.[] | select(.merged_at != null)] | sort_by(.merged_at) | last // empty | {number, base: .base.ref, head_sha: .head.sha}' 2>/dev/null)"; then
		return 1
	fi
	[ -n "${merged_json}" ] || return 0
	number="$(printf '%s' "${merged_json}" | jq -r '.number // empty')"
	base="$(printf '%s' "${merged_json}" | jq -r '.base // empty')"
	head_sha="$(printf '%s' "${merged_json}" | jq -r '.head_sha // empty')"
	[[ "${number}" =~ ^[0-9]+$ ]] && [ -n "${base}" ] && [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] || return 0
	tip_error_file="$(mktemp)" || return 1
	if ! tip="$(gh api "repos/${repo}/git/ref/heads/$(retarget_urlencode "${branch}")" --jq '.object.sha // ""' 2>"${tip_error_file}")"; then
		# A confirmed deleted ref cannot have been reused; other API errors
		# must not authorize retargeting from an unverified branch tip.
		tip_error="$(<"${tip_error_file}")"
		rm -f "${tip_error_file}"
		[[ "${tip_error}" == *"(HTTP 404)"* ]] || return 1
		printf '%s %s\n' "${number}" "${base}"
		return 0
	fi
	rm -f "${tip_error_file}"
	[ "${tip}" = "${head_sha}" ] || return 0
	printf '%s %s\n' "${number}" "${base}"
}

# Follows merged bases from <branch>; prints "<target> <first_merged_pr>".
retarget_follow()
{
	local repo="$1" branch="$2" default_branch="$3"
	local max_hops="${RETARGET_MERGED_BASE_MAX_HOPS:-3}" hop=0 current="$2" found merged_pr first_pr="" next
	[[ "${max_hops}" =~ ^[1-9][0-9]*$ ]] || max_hops=3
	while [ "${hop}" -lt "${max_hops}" ] && [ -n "${current}" ] && [ "${current}" != "${default_branch}" ]; do
		if ! found="$(retarget_lookup_merged "${repo}" "${current}")"; then
			return 1
		fi
		[ -n "${found}" ] || break
		merged_pr="${found%% *}"
		next="${found#* }"
		[ -n "${first_pr}" ] || first_pr="${merged_pr}"
		[ "${next}" != "${current}" ] || break
		current="${next}"
		hop=$((hop + 1))
	done
	printf '%s %s\n' "${current}" "${first_pr}"
}

retarget_main()
{
	local mode="${1:-}" repo="${2:-}"
	case "${mode}" in
		resolve)
			local branch="${3:-}" default_branch="${4:-}" result target merged_pr
			if [ -z "${repo}" ] || [ -z "${branch}" ] || [ -z "${default_branch}" ]; then
				echo "usage: $0 resolve <owner/repo> <branch> <default_branch>" >&2
				printf '%s\n' "${branch}"
				return 0
			fi
			if [ "${RETARGET_MERGED_BASE_ENABLED:-true}" = "false" ] || [ "${branch}" = "${default_branch}" ]; then
				printf '%s\n' "${branch}"
				return 0
			fi
			if ! result="$(retarget_follow "${repo}" "${branch}" "${default_branch}")"; then
				retarget_log "mode=resolve repo=${repo} from=${branch} to=${branch} outcome=unchanged reason=lookup_failed"
				echo "::warning::Could not check whether ${branch} was already merged; keeping it." >&2
				printf '%s\n' "${branch}"
				return 0
			fi
			target="${result% *}"
			merged_pr="${result##* }"
			if [ "${target}" != "${branch}" ]; then
				retarget_log "mode=resolve repo=${repo} from=${branch} to=${target} merged_pr=${merged_pr} outcome=retargeted"
			fi
			printf '%s\n' "${target}"
			;;
		pr)
			local pr_number="${3:-}" base="${4:-}" default_branch="${5:-}" result target merged_pr
			if [ -z "${repo}" ] || ! [[ "${pr_number}" =~ ^[0-9]+$ ]] || [ -z "${base}" ] || [ -z "${default_branch}" ]; then
				echo "usage: $0 pr <owner/repo> <pr_number> <base_branch> <default_branch>" >&2
				printf '%s\n' "${base}"
				return 0
			fi
			if [ "${RETARGET_MERGED_BASE_ENABLED:-true}" = "false" ] || [ "${base}" = "${default_branch}" ]; then
				printf '%s\n' "${base}"
				return 0
			fi
			if ! result="$(retarget_follow "${repo}" "${base}" "${default_branch}")"; then
				retarget_log "mode=pr repo=${repo} pr=${pr_number} from=${base} to=${base} outcome=unchanged reason=lookup_failed"
				echo "::warning::Could not check whether the base ${base} of PR #${pr_number} was already merged; keeping it." >&2
				printf '%s\n' "${base}"
				return 0
			fi
			target="${result% *}"
			merged_pr="${result##* }"
			if [ "${target}" = "${base}" ]; then
				printf '%s\n' "${base}"
				return 0
			fi
			if gh api -X PATCH "repos/${repo}/pulls/${pr_number}" -f base="${target}" >/dev/null 2>&1; then
				retarget_log "mode=pr repo=${repo} pr=${pr_number} from=${base} to=${target} merged_pr=${merged_pr} outcome=retargeted"
				printf '%s\n' "${target}"
			else
				retarget_log "mode=pr repo=${repo} pr=${pr_number} from=${base} to=${target} merged_pr=${merged_pr} outcome=unchanged reason=patch_failed"
				echo "::warning::Could not retarget PR #${pr_number} from merged base ${base} to ${target}." >&2
				printf '%s\n' "${base}"
			fi
			;;
		*)
			echo "usage: $0 resolve|pr ..." >&2
			return 2
			;;
	esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	retarget_main "$@"
fi
