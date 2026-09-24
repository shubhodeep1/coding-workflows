#!/usr/bin/env bash
#
# workflow_failure_heal_pr_reconcile.sh
#
# Runs when a pull request in coding-workflows closes (the heal-pr-reconcile
# job in .github/workflows/internal-cancel-on-pr-close.yml). Workflow failure
# heal files a fix for a review/autofix failure on that PR's own head branch
# (scripts/workflow_failure_heal_intake.sh, target_branch_source=source_pr_head),
# so the heal PR's branch carries the source PR's commits. Once the source PR
# closes, that heal PR is stranded (PR #4349 on PR #4323). For every open heal
# PR of the closed source PR:
#
#   * source closed without merging -> close the heal PR with a comment and
#     never re-point it: its branch carries the rejected commits. The heal
#     issue is closed as not planned with a comment.
#   * source merged -> merge the source PR's final head (refs/pull/<n>/head)
#     and then the source PR's base into the heal branch, push it as a normal
#     fast-forward (the repository ruleset rejects force pushes on every
#     branch), and re-point the heal PR at that base; its diff is then only the
#     heal changes. A merge conflict, a heal branch with nothing left to apply,
#     or a missing base closes the heal PR and its issue as above instead.
#
# A heal PR is recognised only through its heal issue: an open issue labelled
# ai:workflow-heal whose body carries
# `<!-- workflow-failure-heal:source=<repo>#<source PR> -->`, whose PR's head
# is `ai/issue-<issue>` and whose base is the source PR's head branch (or,
# after a merge, the source base GitHub already re-pointed it to).
#
# API budget (CLAUDE.md §15): one paginated GET of the open heal issues, one
# GET of the open PRs per matching heal issue, then the writes for each heal
# PR acted on. Nothing else in this path already holds that data. Git reads
# and the (fast-forward) push go through the git remote.
#
# Required env: GH_TOKEN, REPOSITORY, SOURCE_PR_NUMBER, SOURCE_PR_MERGED,
#   SOURCE_HEAD_REF, SOURCE_HEAD_SHA, SOURCE_BASE_REF
# Optional env: SOURCE_HEAD_REPO (skip when it is not REPOSITORY),
#   WORKFLOW_HEAL_PR_RECONCILE_ENABLED ("false" disables; default true),
#   WORKFLOW_HEAL_LABEL (default ai:workflow-heal)
#
# Never fails the job: every exit is 0 and every outcome is one log line
# prefixed WORKFLOW_HEAL_PR_RECONCILE.

set -uo pipefail

log()
{
	echo "WORKFLOW_HEAL_PR_RECONCILE $*"
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${script_dir}/gh_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source "${script_dir}/gh_helpers.sh" 2>/dev/null || true
fi
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

ENABLED="${WORKFLOW_HEAL_PR_RECONCILE_ENABLED:-true}"
REPO="${REPOSITORY:-}"
SOURCE_PR="${SOURCE_PR_NUMBER:-}"
MERGED="${SOURCE_PR_MERGED:-false}"
HEAD_REF="${SOURCE_HEAD_REF:-}"
HEAD_SHA="$(printf '%s' "${SOURCE_HEAD_SHA:-}" | tr '[:upper:]' '[:lower:]')"
BASE_REF="${SOURCE_BASE_REF:-}"
HEAL_LABEL="${WORKFLOW_HEAL_LABEL:-ai:workflow-heal}"

_valid_branch()
{
	local branch="$1"
	[[ "${branch}" =~ ^[A-Za-z0-9._/-]{1,200}$ ]] || return 1
	case "${branch}" in
		-*|/*|*/|*..*|*//*|*.lock|*/.*|.*) return 1 ;;
	esac
	return 0
}

if [ "$(printf '%s' "${ENABLED}" | tr '[:upper:]' '[:lower:]')" = "false" ]; then
	log "skip reason=disabled pr=${SOURCE_PR:-none}"
	exit 0
fi
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || ! [[ "${SOURCE_PR}" =~ ^[1-9][0-9]*$ ]] \
	|| ! _valid_branch "${HEAD_REF}" || ! _valid_branch "${BASE_REF}" || ! [[ "${HEAD_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
	log "skip reason=missing_context repo=${REPO:-none} pr=${SOURCE_PR:-none} head_ref=${HEAD_REF:-none} base_ref=${BASE_REF:-none}"
	exit 0
fi
if [ -n "${SOURCE_HEAD_REPO:-}" ] && [ "${SOURCE_HEAD_REPO}" != "${REPO}" ]; then
	# Heal fixes only ever target branches of this repository.
	log "skip reason=fork_head pr=${SOURCE_PR} head_repo=${SOURCE_HEAD_REPO}"
	exit 0
fi

WORK_DIR="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/workflow_heal_pr_reconcile"
mkdir -p "${WORK_DIR}"
ISSUES_FILE="${WORK_DIR}/heal_issues.json"
SOURCE_MARKER="<!-- workflow-failure-heal:source=${REPO}#${SOURCE_PR} -->"

if ! gh_retry gh api --paginate --method GET "repos/${REPO}/issues" -f state=open -f labels="${HEAL_LABEL}" -f per_page=100 \
	--jq '[.[] | select(.pull_request == null) | {number, body: (.body // "")}]' 2>/dev/null \
	| jq -c -s 'add // []' > "${ISSUES_FILE}" 2>/dev/null; then
	log "skip reason=heal_issue_list_failed pr=${SOURCE_PR}"
	exit 0
fi
mapfile -t HEAL_ISSUES < <(jq -r --arg marker "${SOURCE_MARKER}" '.[] | select(.body | contains($marker)) | .number' "${ISSUES_FILE}" 2>/dev/null)
if [ "${#HEAL_ISSUES[@]}" -eq 0 ]; then
	log "noop reason=no_heal_issue pr=${SOURCE_PR} merged=${MERGED}"
	exit 0
fi

_comment()
{
	local number="$1" body="$2"
	gh_retry gh api --method POST "repos/${REPO}/issues/${number}/comments" -f body="${body}" >/dev/null 2>&1 \
		|| log "warn comment_failed number=${number}"
}

# Close the heal PR and its issue (Q10/Q12): the fix no longer has a branch
# it can land on.
_close_heal()
{
	local heal_pr="$1" heal_issue="$2" reason="$3" pr_detail="$4"
	_comment "${heal_pr}" "${pr_detail}

---
_Workflow failure heal PR reconcile (\`${reason}\`)._"
	if ! gh_retry gh api --method PATCH "repos/${REPO}/pulls/${heal_pr}" -f state=closed >/dev/null 2>&1; then
		log "warn heal_pr_close_failed heal_pr=${heal_pr} source_pr=${SOURCE_PR} reason=${reason}"
		return 0
	fi
	gh_retry gh api --method DELETE "repos/${REPO}/issues/${heal_pr}/labels/ai:merge-queued" >/dev/null 2>&1 || true
	_comment "${heal_issue}" "Closing as not planned: heal PR #${heal_pr} was closed because its source pull request #${SOURCE_PR} closed (\`${reason}\`). If the failure recurs, workflow failure heal files a new issue.

---
_Workflow failure heal PR reconcile._"
	gh_retry gh api --method PATCH "repos/${REPO}/issues/${heal_issue}" -f state=closed -f state_reason=not_planned >/dev/null 2>&1 \
		|| log "warn heal_issue_close_failed heal_issue=${heal_issue}"
	log "closed heal_pr=${heal_pr} heal_issue=${heal_issue} source_pr=${SOURCE_PR} reason=${reason}"
}

# Bring the source PR's base into the heal branch and re-point the heal PR
# (Q11, as a merge: the repository ruleset rejects non-fast-forward pushes on
# every branch, so the heal branch cannot be rebased and force-pushed).
# Merging the source PR's final head first picks up any source commits made
# after the heal branch was cut; merging the base second then only adds what
# the base gained beyond the source PR. The heal PR's diff against the base is
# afterwards exactly the heal changes. Returns 1 with RETARGET_FAIL_REASON set
# when the heal PR must be closed or left alone instead.
RETARGET_FAIL_REASON=""
_retarget_heal()
{
	local heal_pr="$1" heal_branch="$2" heal_head_sha="$3" heal_base="$4"
	local source_ref="refs/remotes/origin/workflow-heal-source-pr-${SOURCE_PR}"
	local heal_remote_ref="refs/remotes/origin/${heal_branch}"
	local base_remote_ref="refs/remotes/origin/${BASE_REF}"
	RETARGET_FAIL_REASON=""
	if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
		RETARGET_FAIL_REASON="no_git_checkout"
		return 1
	fi
	# refs/pull/<n>/head outlives the source branch.
	if ! git fetch --quiet --no-tags origin \
		"+refs/pull/${SOURCE_PR}/head:${source_ref}" \
		"+refs/heads/${heal_branch}:${heal_remote_ref}" >/dev/null 2>&1; then
		RETARGET_FAIL_REASON="fetch_failed"
		return 1
	fi
	if ! git fetch --quiet --no-tags origin "+refs/heads/${BASE_REF}:${base_remote_ref}" >/dev/null 2>&1; then
		RETARGET_FAIL_REASON="base_branch_missing"
		return 1
	fi
	if [ "$(git rev-parse "${heal_remote_ref}" 2>/dev/null)" != "${heal_head_sha}" ]; then
		RETARGET_FAIL_REASON="heal_branch_moved"
		return 1
	fi
	local merge_dir="${WORK_DIR}/merge-${heal_pr}"
	rm -rf "${merge_dir}"
	git worktree prune >/dev/null 2>&1 || true
	if ! git worktree add --quiet --detach "${merge_dir}" "${heal_remote_ref}" >/dev/null 2>&1; then
		RETARGET_FAIL_REASON="worktree_failed"
		return 1
	fi
	local merge_ref merge_message
	for merge_ref in "${source_ref}" "${base_remote_ref}"; do
		if [ "${merge_ref}" = "${source_ref}" ]; then
			merge_message="Merge the final head of #${SOURCE_PR} into ${heal_branch}"
		else
			merge_message="Merge ${BASE_REF} into ${heal_branch} after #${SOURCE_PR} merged"
		fi
		if ! GIT_AUTHOR_NAME="codex-bot" GIT_AUTHOR_EMAIL="codex@users.noreply.github.com" \
			GIT_COMMITTER_NAME="codex-bot" GIT_COMMITTER_EMAIL="codex@users.noreply.github.com" \
			git -C "${merge_dir}" merge --quiet --no-edit -m "${merge_message}" "${merge_ref}" >/dev/null 2>&1; then
			git -C "${merge_dir}" merge --abort >/dev/null 2>&1 || true
			git worktree remove --force "${merge_dir}" >/dev/null 2>&1 || true
			RETARGET_FAIL_REASON="merge_conflict"
			return 1
		fi
	done
	local merged_sha
	merged_sha="$(git -C "${merge_dir}" rev-parse HEAD 2>/dev/null || echo "")"
	if [ -z "${merged_sha}" ] || git diff --quiet "${base_remote_ref}" "${merged_sha}" 2>/dev/null; then
		# The base already carries everything this fix changes.
		git worktree remove --force "${merge_dir}" >/dev/null 2>&1 || true
		RETARGET_FAIL_REASON="nothing_to_apply"
		return 1
	fi
	# A plain fast-forward push: merged_sha descends from heal_head_sha.
	if ! git -C "${merge_dir}" push --quiet origin "${merged_sha}:refs/heads/${heal_branch}" >/dev/null 2>&1; then
		git worktree remove --force "${merge_dir}" >/dev/null 2>&1 || true
		RETARGET_FAIL_REASON="push_rejected"
		return 1
	fi
	git worktree remove --force "${merge_dir}" >/dev/null 2>&1 || true
	if [ "${heal_base}" != "${BASE_REF}" ] \
		&& ! gh_retry gh api --method PATCH "repos/${REPO}/pulls/${heal_pr}" -f base="${BASE_REF}" >/dev/null 2>&1; then
		log "warn heal_pr_retarget_failed heal_pr=${heal_pr} base=${BASE_REF} merged_sha=${merged_sha}"
	fi
	_comment "${heal_pr}" "Source pull request #${SOURCE_PR} merged into \`${BASE_REF}\`. Merged #${SOURCE_PR}'s final head and \`${BASE_REF}\` into this branch (now \`${merged_sha:0:12}\`) and re-pointed this pull request at \`${BASE_REF}\`, so its diff is only this fix.

---
_Workflow failure heal PR reconcile._"
	log "retargeted heal_pr=${heal_pr} heal_branch=${heal_branch} source_pr=${SOURCE_PR} base=${BASE_REF} old_head=${heal_head_sha} new_head=${merged_sha}"
	return 0
}

for heal_issue in "${HEAL_ISSUES[@]}"; do
	[[ "${heal_issue}" =~ ^[1-9][0-9]*$ ]] || continue
	heal_branch="ai/issue-${heal_issue}"
	PULLS_FILE="${WORK_DIR}/heal_pulls_${heal_issue}.json"
	if ! gh_retry gh api --method GET "repos/${REPO}/pulls" -f state=open -f head="${REPO%%/*}:${heal_branch}" -f per_page=10 > "${PULLS_FILE}" 2>/dev/null \
		|| ! jq -e 'type == "array"' "${PULLS_FILE}" >/dev/null 2>&1; then
		log "warn heal_pr_lookup_failed heal_issue=${heal_issue} source_pr=${SOURCE_PR}"
		continue
	fi
	while IFS=$'\t' read -r heal_pr heal_head_ref heal_head_sha heal_base; do
		[[ "${heal_pr}" =~ ^[1-9][0-9]*$ ]] || continue
		[ "${heal_head_ref}" = "${heal_branch}" ] || continue
		heal_head_sha="$(printf '%s' "${heal_head_sha}" | tr '[:upper:]' '[:lower:]')"
		if [ "${heal_base}" != "${HEAD_REF}" ] && ! { [ "${MERGED}" = "true" ] && [ "${heal_base}" = "${BASE_REF}" ]; }; then
			# Not stacked on the closed PR (e.g. it targets stable): leave it.
			log "skip reason=unrelated_base heal_pr=${heal_pr} base=${heal_base} source_pr=${SOURCE_PR}"
			continue
		fi
		if [ "${MERGED}" != "true" ]; then
			_close_heal "${heal_pr}" "${heal_issue}" "source_closed_unmerged" \
				"Closing without merging: source pull request #${SOURCE_PR} closed without merging. This branch carries #${SOURCE_PR}'s commits, so it is not re-pointed at \`${BASE_REF}\`: that would re-propose changes that were not merged."
			continue
		fi
		if _retarget_heal "${heal_pr}" "${heal_branch}" "${heal_head_sha}" "${heal_base}"; then
			continue
		fi
		if [ "${RETARGET_FAIL_REASON}" = "heal_branch_moved" ] || [ "${RETARGET_FAIL_REASON}" = "push_rejected" ]; then
			# Someone else is updating the branch right now: never close over them.
			log "skip reason=${RETARGET_FAIL_REASON} heal_pr=${heal_pr} source_pr=${SOURCE_PR}"
			continue
		fi
		_close_heal "${heal_pr}" "${heal_issue}" "source_merged_${RETARGET_FAIL_REASON}" \
			"Closing without merging: source pull request #${SOURCE_PR} merged into \`${BASE_REF}\`, but this fix could not be moved onto \`${BASE_REF}\` (\`${RETARGET_FAIL_REASON}\`)."
	done < <(jq -r '.[] | [(.number|tostring), (.head.ref // ""), (.head.sha // ""), (.base.ref // "")] | @tsv' "${PULLS_FILE}")
done
exit 0
