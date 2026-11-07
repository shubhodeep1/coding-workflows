#!/usr/bin/env bash
# Deliver one workflow-log-analysis report to the base branch through a pull
# request (issue #8963).
#
# Why: since 2026-10-26 the default-branch ruleset requires the `lint` and
# `ai-review/head-gate` checks and has no bypass actors, so the old direct
# push of the report to main failed with GH013 on every run. Each run now
# commits its report (and the deep-audit / API-redundancy sections) to its
# own branch, `auto/workflow-log-analysis-report-<run_id>-<run_attempt>`,
# and this script, run only by the `deliver-report` job of
# .github/workflows/workflow-log-analysis.yml, opens one PR for that branch.
# CI and the review workflow then post the required checks and merge it.
#
# Inputs (env):
#   REPORT_BRANCH          run branch (must match the run-branch pattern)
#   REPORT_FILE            analysis/workflow-optimization-<date>(-N).md
#   REPORT_PUSH_TOKEN      GH_PAT, read by a one-shot git credential helper
#   GH_TOKEN               GH_PAT for gh (a github.token PR starts no CI)
#   PURGED_DOCS            space-separated docs the purge deleted (optional)
#   AUDIT_STATUS           deep-audit status (optional)
#   API_REDUNDANCY_STATUS  API-redundancy status (optional)
#   GITHUB_REF_TYPE / GITHUB_REF_NAME / GITHUB_REPOSITORY / GITHUB_RUN_ID
#
# GitHub API calls: 1 paginated `GET pulls?state=open&base=<base>` (fails
# open: only the base branch is checked for collisions), 1 `gh pr create`,
# and 1 `gh pr list --head` only when the create fails. Collision checks
# against other open report branches use one `git fetch`, not the API.
#
# All git work on the run branch happens in a temporary worktree, so the
# trusted checkout this script and the PR-body lint run from never switches
# to the LLM-written run-branch content.
#
# Log contract (stable prefix):
#   WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=pr_opened branch=<b> pr=<n> file=<path>
#   ::error::WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=failed reason=<reason> ...
#   ::warning::WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=degraded reason=<reason>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# gh_retry; a missing helper falls back to a plain gh call below.
# shellcheck source=scripts/gh_helpers.sh
source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true

WLA_DELIVER_PREFIX="WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY"
WLA_DELIVER_BRANCH_RE='^auto/workflow-log-analysis-report-[0-9]+-[0-9]+$'
WLA_DELIVER_DOC_RE='^analysis/workflow-optimization-[0-9]{4}-[0-9]{2}-[0-9]{2}(-[0-9]+)?\.md$'
WLA_DELIVER_RULESET_RE='GH006|GH013|protected branch|push declined|repository rule'
WLA_DELIVER_MAX_PURGED_LISTED=50

wla_deliver_redact()
{
	# Print a captured git/gh log to stderr with any URL credentials masked.
	sed -E 's#(https?://)[^/@[:space:]]+@#\1***@#g' "$1" >&2 || true
}

wla_deliver_fail()
{
	# $1 = reason; remaining args are extra key=value fields.
	local reason="$1"
	shift
	echo "::error::${WLA_DELIVER_PREFIX} outcome=failed reason=${reason}${*:+ $*}"
	exit 1
}

# --- 1. Validate inputs -----------------------------------------------------
if [ "${GITHUB_REF_TYPE:-}" != "branch" ]; then
	echo "Report delivery needs a branch ref (got ref_type='${GITHUB_REF_TYPE:-unknown}')."
	wla_deliver_fail run_context_invalid
fi
TARGET_BRANCH="${GITHUB_REF_NAME:-}"
if ! [[ "${TARGET_BRANCH}" =~ ^[A-Za-z0-9._/-]+$ ]] \
	|| ! [[ "${GITHUB_REPOSITORY:-}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] \
	|| ! [[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]]; then
	wla_deliver_fail run_context_invalid
fi
REPORT_BRANCH="${REPORT_BRANCH:-}"
if ! [[ "${REPORT_BRANCH}" =~ ${WLA_DELIVER_BRANCH_RE} ]]; then
	wla_deliver_fail report_branch_invalid
fi
REPORT_FILE="${REPORT_FILE:-}"
if ! [[ "${REPORT_FILE}" =~ ${WLA_DELIVER_DOC_RE} ]]; then
	wla_deliver_fail report_file_invalid "branch=${REPORT_BRANCH}"
fi
# Fail closed: a PR opened with github.token starts no CI, so it could never
# get the required checks and merge.
if [ -z "${REPORT_PUSH_TOKEN:-}" ] || [ -z "${GH_TOKEN:-}" ]; then
	wla_deliver_fail token_missing "branch=${REPORT_BRANCH}"
fi

# --- 2. Authenticate git ----------------------------------------------------
# The checkout does not persist credentials; a one-shot helper reads the
# token from the env. The token is never echoed.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=credential.helper
export GIT_CONFIG_VALUE_0='!f() { printf "username=x-access-token\npassword=%s\n" "${REPORT_PUSH_TOKEN}"; }; f'

work_tmp="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/wla-report-delivery.XXXXXX")"
worktree_dir="${work_tmp}/wt"
wla_deliver_cleanup()
{
	if [ -d "${worktree_dir}" ]; then
		git worktree remove --force "${worktree_dir}" >/dev/null 2>&1 || true
	fi
	git worktree prune >/dev/null 2>&1 || true
	rm -rf "${work_tmp}"
}
trap wla_deliver_cleanup EXIT

# --- 3. List other open report PRs (one paginated call, fail open) ---------
gh_list_cmd=(gh)
if declare -F gh_retry >/dev/null 2>&1; then
	gh_list_cmd=(gh_retry gh)
fi
other_branches=()
declare -A seen_branches=()
pr_list_raw="${work_tmp}/open-prs.raw"
pr_list_json="${work_tmp}/open-prs.json"
if "${gh_list_cmd[@]}" api --paginate "repos/${GITHUB_REPOSITORY}/pulls?state=open&base=${TARGET_BRANCH}&per_page=100" \
	>"${pr_list_raw}" 2>"${work_tmp}/open-prs.err" \
	&& jq -s 'add // [] | if type == "array" then . else error("not an array") end' "${pr_list_raw}" >"${pr_list_json}" 2>/dev/null; then
	while IFS= read -r head_ref; do
		[[ "${head_ref}" =~ ${WLA_DELIVER_BRANCH_RE} ]] || continue
		[ "${head_ref}" = "${REPORT_BRANCH}" ] && continue
		[ -n "${seen_branches[${head_ref}]:-}" ] && continue
		seen_branches[${head_ref}]=1
		other_branches+=("${head_ref}")
	done < <(jq -r --arg repo "${GITHUB_REPOSITORY}" \
		'.[] | select(type == "object") | select((.head.repo.full_name // "") == $repo) | (.head.ref // empty) | strings' \
		"${pr_list_json}")
else
	wla_deliver_redact "${work_tmp}/open-prs.err"
	echo "::warning::${WLA_DELIVER_PREFIX} outcome=degraded reason=pr_list_failed"
fi
echo "INFO: ${#other_branches[@]} other open report pull request branch(es) to check for collisions."

# --- 4. Fetch the base, the run branch and other report branches -----------
wla_deliver_spec()
{
	printf '+refs/heads/%s:refs/remotes/origin/%s' "$1" "$1"
}
fetch_specs=("$(wla_deliver_spec "${TARGET_BRANCH}")" "$(wla_deliver_spec "${REPORT_BRANCH}")")
for head_ref in ${other_branches[@]+"${other_branches[@]}"}; do
	fetch_specs+=("$(wla_deliver_spec "${head_ref}")")
done
fetch_log="${work_tmp}/fetch.log"
if ! git fetch --no-tags --depth=1 origin "${fetch_specs[@]}" >"${fetch_log}" 2>&1; then
	wla_deliver_redact "${fetch_log}"
	# One missing branch (a PR merged since the listing) fails the whole
	# fetch. The base and the run branch are required; others are optional.
	if ! git fetch --no-tags --depth=1 origin "${fetch_specs[0]}" "${fetch_specs[1]}" >"${fetch_log}" 2>&1; then
		wla_deliver_redact "${fetch_log}"
		wla_deliver_fail fetch_failed "branch=${REPORT_BRANCH}"
	fi
	echo "::warning::${WLA_DELIVER_PREFIX} outcome=degraded reason=fetch_partial"
	for head_ref in ${other_branches[@]+"${other_branches[@]}"}; do
		git fetch --no-tags --depth=1 origin "$(wla_deliver_spec "${head_ref}")" >/dev/null 2>&1 || true
	done
fi
run_ref="refs/remotes/origin/${REPORT_BRANCH}"
if ! git rev-parse --verify -q "${run_ref}^{commit}" >/dev/null; then
	wla_deliver_fail fetch_failed "branch=${REPORT_BRANCH}"
fi
collision_refs=()
for head_ref in "${TARGET_BRANCH}" ${other_branches[@]+"${other_branches[@]}"}; do
	if git rev-parse --verify -q "refs/remotes/origin/${head_ref}^{commit}" >/dev/null; then
		collision_refs+=("refs/remotes/origin/${head_ref}")
	fi
done

# --- 5. Temporary worktree at the run branch --------------------------------
if ! git worktree add --detach "${worktree_dir}" "${run_ref}" >"${work_tmp}/worktree.log" 2>&1; then
	wla_deliver_redact "${work_tmp}/worktree.log"
	wla_deliver_fail worktree_failed "branch=${REPORT_BRANCH}"
fi

# A rerun after an earlier rename: the rename commit names the new path.
if ! git cat-file -e "${run_ref}:${REPORT_FILE}" 2>/dev/null; then
	renamed_path="$(git log -1 --format=%B "${run_ref}" | sed -nE 's/^Report-file: (.*)$/\1/p' | tail -n 1)"
	if [[ "${renamed_path}" =~ ${WLA_DELIVER_DOC_RE} ]] && git cat-file -e "${run_ref}:${renamed_path}" 2>/dev/null; then
		echo "INFO: '${REPORT_FILE}' was already renamed to '${renamed_path}' on ${REPORT_BRANCH}."
		REPORT_FILE="${renamed_path}"
	else
		wla_deliver_fail report_file_missing "branch=${REPORT_BRANCH} file=${REPORT_FILE}"
	fi
fi

# --- 6. Rename on collision (committed locally; pushed after the lint) -----
wla_deliver_path_taken()
{
	local path="$1" ref
	for ref in "${collision_refs[@]}"; do
		if git cat-file -e "${ref}:${path}" 2>/dev/null; then
			return 0
		fi
	done
	return 1
}
renamed_from=""
if wla_deliver_path_taken "${REPORT_FILE}"; then
	[[ "${REPORT_FILE}" =~ ^(analysis/workflow-optimization-[0-9]{4}-[0-9]{2}-[0-9]{2})(-[0-9]+)?\.md$ ]]
	report_base="${BASH_REMATCH[1]}"
	next_suffix=2
	while wla_deliver_path_taken "${report_base}-${next_suffix}.md" \
		|| git cat-file -e "${run_ref}:${report_base}-${next_suffix}.md" 2>/dev/null; do
		next_suffix=$((next_suffix + 1))
	done
	new_report_file="${report_base}-${next_suffix}.md"
	git -C "${worktree_dir}" mv -- "${REPORT_FILE}" "${new_report_file}"
	git -C "${worktree_dir}" \
		-c user.name="github-actions[bot]" \
		-c user.email="github-actions[bot]@users.noreply.github.com" \
		commit -q -m "chore: rename workflow log analysis report to avoid collision" -m "Report-file: ${new_report_file}"
	echo "INFO: renamed '${REPORT_FILE}' to '${new_report_file}' (the path is taken on ${TARGET_BRANCH} or another open report branch)."
	renamed_from="${REPORT_FILE}"
	REPORT_FILE="${new_report_file}"
fi

# --- 7. PR body + auto-close lint (before any push) ------------------------
run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
wla_deliver_status()
{
	if [[ "${1:-}" =~ ^[a-z_]+$ ]]; then
		printf '%s' "$1"
	else
		printf 'not run'
	fi
}
purged_listed=()
purged_extra=0
for purged_doc in ${PURGED_DOCS:-}; do
	[[ "${purged_doc}" =~ ${WLA_DELIVER_DOC_RE} ]] || continue
	if [ "${#purged_listed[@]}" -ge "${WLA_DELIVER_MAX_PURGED_LISTED}" ]; then
		purged_extra=$((purged_extra + 1))
		continue
	fi
	purged_listed+=("${purged_doc}")
done
body_file="${work_tmp}/body.md"
{
	echo "Workflow log analysis report from run ${run_url}."
	echo ""
	echo "- Report: \`${REPORT_FILE}\`"
	if [ -n "${renamed_from}" ]; then
		echo "- Renamed from \`${renamed_from}\`, which is already taken on \`${TARGET_BRANCH}\` or another open report branch."
	fi
	echo "- Deep audit section: $(wla_deliver_status "${AUDIT_STATUS:-}")"
	echo "- API redundancy section: $(wla_deliver_status "${API_REDUNDANCY_STATUS:-}")"
	echo ""
	if [ "${#purged_listed[@]}" -gt 0 ]; then
		echo "Older analysis docs deleted by the retention purge (each one is already listed in \`analysis/recommendation-processing-report.md\`):"
		for purged_doc in "${purged_listed[@]}"; do
			echo "- \`${purged_doc}\`"
		done
		if [ "${purged_extra}" -gt 0 ]; then
			echo "- and ${purged_extra} more"
		fi
	else
		echo "No older analysis docs were deleted by the retention purge."
	fi
	echo ""
	echo "The default branch requires status checks and has no bypass actors, so the workflow opens this pull request instead of pushing the report directly. CI and the review workflow merge it."
} >"${body_file}"
if ! python3 "${SCRIPT_DIR}/lint_pr_body_auto_close.py" --pr-body-file "${body_file}" --repo "${GITHUB_REPOSITORY}"; then
	wla_deliver_fail pr_body_lint_failed "branch=${REPORT_BRANCH}"
fi

# --- 6b. Push the rename (branch only this run writes: fast-forward) -------
if [ -n "${renamed_from}" ]; then
	push_log="${work_tmp}/push.log"
	push_attempt=0
	push_max_attempts=3
	push_backoff_secs=5
	while : ; do
		push_attempt=$((push_attempt + 1))
		push_exit=0
		git -C "${worktree_dir}" push origin "HEAD:refs/heads/${REPORT_BRANCH}" >"${push_log}" 2>&1 || push_exit=$?
		wla_deliver_redact "${push_log}"
		if [ "${push_exit}" -eq 0 ]; then
			break
		fi
		if grep -qiE "${WLA_DELIVER_RULESET_RE}" "${push_log}"; then
			wla_deliver_fail push_rejected_by_ruleset "branch=${REPORT_BRANCH}"
		fi
		if [ "${push_attempt}" -ge "${push_max_attempts}" ]; then
			wla_deliver_fail push_failed "branch=${REPORT_BRANCH} rc=${push_exit}"
		fi
		echo "::warning::Report rename push attempt ${push_attempt} failed; retrying in ${push_backoff_secs}s."
		sleep "${push_backoff_secs}"
		push_backoff_secs=$((push_backoff_secs * 2))
	done
fi

# --- 8. Open the PR (one create; list fallback covers reruns) --------------
pr_title="chore: add workflow log analysis report ($(basename "${REPORT_FILE}"))"
create_err="${work_tmp}/create.err"
pr_number=""
if create_out="$(gh pr create --repo "${GITHUB_REPOSITORY}" --base "${TARGET_BRANCH}" --head "${REPORT_BRANCH}" --title "${pr_title}" --body-file "${body_file}" 2>"${create_err}")"; then
	pr_number="$(printf '%s\n' "${create_out}" | sed -nE 's#.*/pull/([0-9]+).*#\1#p' | tail -n 1)"
fi
if ! [[ "${pr_number}" =~ ^[0-9]+$ ]]; then
	pr_number="$(gh pr list --repo "${GITHUB_REPOSITORY}" --head "${REPORT_BRANCH}" --state open --json number --jq '.[0].number // empty' 2>/dev/null || true)"
fi
if ! [[ "${pr_number}" =~ ^[0-9]+$ ]]; then
	wla_deliver_redact "${create_err}"
	wla_deliver_fail pr_create_failed "branch=${REPORT_BRANCH}"
fi

# --- 9. Report ---------------------------------------------------------------
# The branch stays after the PR opens (the ruleset blocks deletion).
echo "${WLA_DELIVER_PREFIX} outcome=pr_opened branch=${REPORT_BRANCH} pr=${pr_number} file=${REPORT_FILE}"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
	{
		echo "pr_number=${pr_number}"
		echo "report_file=${REPORT_FILE}"
	} >>"${GITHUB_OUTPUT}"
fi
