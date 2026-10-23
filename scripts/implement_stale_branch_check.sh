#!/usr/bin/env bash
# Decide whether implement.yml may replace a leftover ai/issue-<N> branch.
#
# Usage: implement_stale_branch_check.sh <owner/repo> <branch>
#
# implement.yml builds every run's commit on the fresh base branch and pushes
# it to ai/issue-<N>. When that branch still exists on origin, the push is
# rejected as non-fast-forward, and the push step used to rebase the fresh
# commit onto the old branch. The old branch is always left over from an
# earlier PR (the run skips an issue that already has an open PR, and runs one
# at a time per issue), so the rebase stacked new work on history that was
# already squash-merged. The PR then re-showed the earlier phase's diff and
# conflicted with the base (shubhodeep1/tele-funtoken-msg-scoring#4752: six
# phases of issue #4726 reused one branch).
#
# This helper makes one live REST call for open PRs whose head is <branch>.
# Exit 0 (outcome=replace) only when that call succeeds and lists none; the
# caller then supersedes the branch with an ours-strategy merge (this run's
# tree, the leftover tip as second parent) and a plain fast-forward push, so
# no force push is needed. Every other case exits 1 (outcome=keep) and the
# caller keeps its rebase fallback:
#   reason=not_issue_branch    <branch> is not ai/issue-<N>
#   reason=invalid_repo        <owner/repo> is malformed
#   reason=open_pr             an open PR uses the branch (count=<n>)
#   reason=api_unavailable     the lookup failed (rc=<n>)
#   reason=malformed_response  the lookup did not return a count
# GitHub API calls: exactly one per invocation for an ai/issue-<N> branch,
# none otherwise. Log line: IMPLEMENT_STALE_BRANCH branch=<b> outcome=<o> reason=<r>.
set -euo pipefail

stale_branch_repo="${1:-}"
stale_branch_name="${2:-}"

stale_branch_log()
{
	echo "IMPLEMENT_STALE_BRANCH branch=${stale_branch_name:-none} outcome=$1 reason=$2${3:+ $3}"
}

if ! [[ "${stale_branch_name}" =~ ^ai/issue-[0-9]+$ ]]; then
	stale_branch_log keep not_issue_branch
	exit 1
fi
if ! [[ "${stale_branch_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
	stale_branch_log keep invalid_repo
	exit 1
fi

# Retry transient errors through gh_retry when implement.yml staged
# gh_helpers.sh beside this copy; otherwise use one raw call.
STALE_BRANCH_GH_HELPERS_FILE="$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh"
# shellcheck disable=SC1090
if ! { [ -f "${STALE_BRANCH_GH_HELPERS_FILE}" ] && source "${STALE_BRANCH_GH_HELPERS_FILE}" && declare -F gh_retry >/dev/null; }; then
	gh_retry()
	{
		"$@"
	}
fi

stale_branch_owner="${stale_branch_repo%%/*}"
stale_branch_rc=0
stale_branch_count="$(gh_retry gh api "repos/${stale_branch_repo}/pulls?state=open&head=${stale_branch_owner}:${stale_branch_name}&per_page=100" --jq 'length')" || stale_branch_rc=$?
if [ "${stale_branch_rc}" -ne 0 ]; then
	stale_branch_log keep api_unavailable "rc=${stale_branch_rc}"
	exit 1
fi
if ! [[ "${stale_branch_count}" =~ ^[0-9]+$ ]]; then
	stale_branch_log keep malformed_response
	exit 1
fi
if [ "${stale_branch_count}" -ne 0 ]; then
	stale_branch_log keep open_pr "count=${stale_branch_count}"
	exit 1
fi
stale_branch_log replace no_open_pr
exit 0
