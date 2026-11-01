#!/usr/bin/env bash
# heal_port_to_default.sh: open a port heal issue on the default branch after a
# heal PR merges into `stable` (P6 step 2, decision D4, of
# docs/plans/heal-evidence-reproduction-and-trends-plan.md; Refs #8434).
#
# Called by the "Open heal port issue on the default branch" step of
# .github/workflows/issue_pr_status.yml, which runs only in
# shubhodeep1/coding-workflows. The heal pipeline never opens PRs on two
# branches itself: when the merged stable patch still applies to the default
# branch, this script files one heal issue there (body: the standard heal
# headers with a port fingerprint, `Target branch:` the default branch, a
# patch summary and a final `Refs #<heal>` line; never an auto-close keyword).
#
# Switch: WORKFLOW_HEAL_CODE_REF_ROUTING_ENABLED (default false); anything but
# `true` (case-insensitive) exits 0 with no API call, no git call and no output.
#
# GitHub API budget (CLAUDE.md §14): 0 calls when the switch is off or the PR
# is not a same-repository ai/issue-<N> PR merged into stable; otherwise one
# GraphQL read (viewer login, the heal issue and the port fingerprint search)
# and at most one single-attempt issue POST.
#
# Log contract: `WORKFLOW_HEAL port outcome=filed|not_needed|not_applicable`
# and `::warning::WORKFLOW_HEAL port error reason=<token>`. The script fails
# open: every error is a warning and the exit status is always 0.
set -uo pipefail

HEAL_PORT_SOURCE_BRANCH_NAME="stable"
HEAL_PORT_SWITCH="$(printf '%s' "${WORKFLOW_HEAL_CODE_REF_ROUTING_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')"
if [ "${HEAL_PORT_SWITCH}" != "true" ]
then
	exit 0
fi

HEAL_PORT_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY="${REPOSITORY:-${GITHUB_REPOSITORY:-}}"
PR_NUMBER="${PR_NUMBER:-}"
PR_BASE_REF="${PR_BASE_REF:-}"
PR_HEAD_REF="${PR_HEAD_REF:-}"
PR_HEAD_REPO="${PR_HEAD_REPO:-}"
PR_MERGE_COMMIT_SHA="${PR_MERGE_COMMIT_SHA:-}"
PR_DEFAULT_BRANCH="${PR_DEFAULT_BRANCH:-}"
HEAL_PY="${HEAL_PY:-${HEAL_PORT_SCRIPT_DIR}/workflow_failure_heal.py}"
HEAL_PORT_RUN_URL=""
if [ -n "${GITHUB_RUN_ID:-}" ]
then
	HEAL_PORT_RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
fi

heal_port_skip()
{
	# Not part of the log contract.
	echo "heal port: skipped ($1)"
	exit 0
}

heal_port_issue=""
if [ "${PR_BASE_REF}" != "${HEAL_PORT_SOURCE_BRANCH_NAME}" ]
then
	heal_port_skip "base_not_stable"
fi
if [ -z "${PR_DEFAULT_BRANCH}" ] || [ "${PR_DEFAULT_BRANCH}" = "${HEAL_PORT_SOURCE_BRANCH_NAME}" ] \
	|| [[ ! "${PR_DEFAULT_BRANCH}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$ ]] || [[ "${PR_DEFAULT_BRANCH}" == *..* ]]
then
	heal_port_skip "default_branch"
fi
if [[ ! "${REPOSITORY}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${REPOSITORY}" == *..* ]]
then
	heal_port_skip "repository"
fi
if [ -z "${PR_HEAD_REPO}" ] || [ "${PR_HEAD_REPO}" != "${REPOSITORY}" ]
then
	heal_port_skip "foreign_head"
fi
if [[ "${PR_HEAD_REF}" =~ ^ai/issue-([1-9][0-9]*)$ ]]
then
	heal_port_issue="${BASH_REMATCH[1]}"
else
	heal_port_skip "not_heal_branch"
fi
if [[ ! "${PR_NUMBER}" =~ ^[1-9][0-9]*$ ]]
then
	heal_port_skip "pr_number"
fi
if [[ ! "${PR_MERGE_COMMIT_SHA}" =~ ^[0-9a-f]{40}$ ]]
then
	heal_port_skip "merge_sha"
fi
if [ ! -f "${HEAL_PY}" ]
then
	heal_port_skip "helper_missing"
fi

if [ -f "${HEAL_PORT_SCRIPT_DIR}/gh_helpers.sh" ]
then
	# shellcheck source=/dev/null
	source "${HEAL_PORT_SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
fi
if ! type gh_retry >/dev/null 2>&1
then
	gh_retry()
	{
		"$@"
	}
fi

heal_port_warn()
{
	echo "::warning::WORKFLOW_HEAL port error reason=$1 heal=${heal_port_issue} pr=${PR_NUMBER}"
	exit 0
}

heal_port_not_applicable()
{
	echo "WORKFLOW_HEAL port outcome=not_applicable heal=${heal_port_issue} pr=${PR_NUMBER} reason=$1"
	exit 0
}

heal_port_work=""
heal_port_worktree_added=""
heal_port_cleanup()
{
	if [ -n "${heal_port_worktree_added}" ]
	then
		git worktree remove --force "${heal_port_work}/default" >/dev/null 2>&1 || true
		git worktree prune >/dev/null 2>&1 || true
	fi
	if [ -n "${heal_port_work}" ] && [ -d "${heal_port_work}" ]
	then
		# Only the temporary directory this script created.
		rm -rf "${heal_port_work}"
	fi
}
trap heal_port_cleanup EXIT

if ! heal_port_work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/heal-port.XXXXXX" 2>/dev/null)"
then
	heal_port_work=""
	heal_port_warn "lookup_failed"
fi

# 1. The one lookup: viewer login, the heal issue and the port fingerprint search.
heal_port_fp="$(python3 -B "${HEAL_PY}" heal-port fingerprint --repo "${REPOSITORY}" --heal-issue "${heal_port_issue}" 2>/dev/null)"
if [[ ! "${heal_port_fp}" =~ ^[0-9a-f]{64}$ ]]
then
	heal_port_warn "lookup_failed"
fi
heal_port_query='query($owner:String!,$name:String!,$number:Int!,$q:String!){viewer{login} repository(owner:$owner,name:$name){issue(number:$number){number title body lastEditedAt author{login} labels(first:50){nodes{name}}}} search(query:$q,type:ISSUE,first:10){nodes{... on Issue{number body author{login}}}}}'
if ! GH_RETRY_IDEMPOTENT=true gh_retry gh api graphql \
	-f query="${heal_port_query}" \
	-f owner="${REPOSITORY%%/*}" \
	-f name="${REPOSITORY#*/}" \
	-F number="${heal_port_issue}" \
	-f q="repo:${REPOSITORY} is:issue in:body ${heal_port_fp}" \
	> "${heal_port_work}/lookup.json" 2>"${heal_port_work}/lookup.err"
then
	heal_port_warn "lookup_failed"
fi

# 2. Is the PR a heal, and is a port already filed?
if ! heal_port_check="$(python3 -B "${HEAL_PY}" heal-port check --repo "${REPOSITORY}" --heal-issue "${heal_port_issue}" \
	--response-json "${heal_port_work}/lookup.json" 2>/dev/null)"
then
	heal_port_warn "lookup_failed"
fi
heal_port_decision="$(printf '%s' "${heal_port_check}" | python3 -B -c 'import json, sys; d = json.load(sys.stdin); print(d.get("decision") or "", d.get("existing") or "")' 2>/dev/null || true)"
heal_port_existing="${heal_port_decision#* }"
heal_port_decision="${heal_port_decision%% *}"
case "${heal_port_decision}" in
	not_heal)
		heal_port_skip "not_heal"
		;;
	not_needed)
		echo "WORKFLOW_HEAL port outcome=not_needed heal=${heal_port_issue} pr=${PR_NUMBER} issue=${heal_port_existing}"
		exit 0
		;;
	candidate)
		;;
	*)
		heal_port_warn "lookup_failed"
		;;
esac

# 3. Does the merged patch apply to the default branch? git only, no REST.
# Fetch stderr can carry the token-bearing origin URL, so it is never printed.
if ! git fetch --no-tags --quiet --depth=1 origin "refs/heads/${PR_DEFAULT_BRANCH}" >/dev/null 2>"${heal_port_work}/fetch.err"
then
	heal_port_warn "fetch_failed"
fi
heal_port_default_sha="$(git rev-parse --verify -q 'FETCH_HEAD^{commit}' 2>/dev/null || true)"
if ! git fetch --no-tags --quiet --depth=2 origin "${PR_MERGE_COMMIT_SHA}" >/dev/null 2>>"${heal_port_work}/fetch.err"
then
	heal_port_warn "fetch_failed"
fi
heal_port_parent="$(git rev-parse --verify -q "${PR_MERGE_COMMIT_SHA}^1^{commit}" 2>/dev/null || true)"
if [[ ! "${heal_port_default_sha}" =~ ^[0-9a-f]{40}$ ]] || [[ ! "${heal_port_parent}" =~ ^[0-9a-f]{40}$ ]]
then
	heal_port_warn "fetch_failed"
fi
heal_port_diff_args=(--no-ext-diff --no-textconv)
if ! git diff "${heal_port_diff_args[@]}" --binary --full-index "${heal_port_parent}" "${PR_MERGE_COMMIT_SHA}" > "${heal_port_work}/patch" 2>/dev/null \
	|| ! git diff "${heal_port_diff_args[@]}" --name-only -z --no-renames "${heal_port_parent}" "${PR_MERGE_COMMIT_SHA}" > "${heal_port_work}/paths" 2>/dev/null \
	|| ! git diff "${heal_port_diff_args[@]}" --stat=200 "${heal_port_parent}" "${PR_MERGE_COMMIT_SHA}" > "${heal_port_work}/stat" 2>/dev/null
then
	heal_port_warn "fetch_failed"
fi
if [ ! -s "${heal_port_work}/patch" ]
then
	heal_port_not_applicable "empty_patch"
fi
if ! git worktree add --detach --quiet "${heal_port_work}/default" "${heal_port_default_sha}" >/dev/null 2>&1
then
	heal_port_warn "worktree_failed"
fi
heal_port_worktree_added="1"
# Check only: the patch is never applied, committed or pushed.
if ! git -C "${heal_port_work}/default" apply --check --binary "${heal_port_work}/patch" >/dev/null 2>&1
then
	heal_port_not_applicable "patch_conflict"
fi

# 4. Compose the port issue (routing-validated, then the scope marker).
python3 -B "${HEAL_PY}" heal-port compose --repo "${REPOSITORY}" --heal-issue "${heal_port_issue}" \
	--response-json "${heal_port_work}/lookup.json" --pr "${PR_NUMBER}" --merge-sha "${PR_MERGE_COMMIT_SHA}" \
	--default-branch "${PR_DEFAULT_BRANCH}" --default-sha "${heal_port_default_sha}" \
	--paths-file "${heal_port_work}/paths" --stat-file "${heal_port_work}/stat" \
	--intake-run-url "${HEAL_PORT_RUN_URL}" \
	--title-out "${heal_port_work}/title" --body-out "${heal_port_work}/body" \
	> "${heal_port_work}/compose.json" 2>"${heal_port_work}/compose.err"
heal_port_compose_rc=$?
cat "${heal_port_work}/compose.err" >&2 2>/dev/null || true
if [ "${heal_port_compose_rc}" -eq 3 ]
then
	heal_port_not_applicable "scope_unavailable"
fi
if [ "${heal_port_compose_rc}" -ne 0 ] || [ ! -s "${heal_port_work}/title" ] || [ ! -s "${heal_port_work}/body" ]
then
	heal_port_warn "compose_failed"
fi

# 5. The one create: a single attempt, so a timed-out POST is never repeated.
heal_port_created="$(GH_RETRY_MAX_ATTEMPTS=1 gh_retry gh api -X POST "repos/${REPOSITORY}/issues" \
	-f title="$(cat "${heal_port_work}/title")" \
	-F "body=@${heal_port_work}/body" \
	-f 'labels[]=ai:workflow-heal' \
	--jq .number 2>"${heal_port_work}/create.err")" || heal_port_created=""
heal_port_created="$(printf '%s' "${heal_port_created}" | tr -d '[:space:]')"
if [[ ! "${heal_port_created}" =~ ^[1-9][0-9]*$ ]]
then
	heal_port_warn "create_failed"
fi
echo "WORKFLOW_HEAL port outcome=filed heal=${heal_port_issue} pr=${PR_NUMBER} issue=${heal_port_created}"
exit 0
