#!/usr/bin/env bash
# SHA-bound review status and stale auto-merge withdrawal for review_autofix.
# The gate CLI runs only from the verified workflow-support checkout.

REVIEW_HEAD_GATE_CONTEXT="ai-review/head-gate"
: "${REVIEW_HEAD_GATE_STATUS_ENABLED:=true}"
: "${REVIEW_STALE_AUTO_MERGE_WITHDRAW_ENABLED:=true}"

review_head_gate_valid_repo()
{
	[[ "$1" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] && [[ "$1" != *..* ]]
}

review_head_gate_post_status()
{
	local status_repo="${1:-}" status_sha="${2:-}" status_state="${3:-}" status_description="${4:-}"
	if [ "${REVIEW_HEAD_GATE_STATUS_ENABLED}" = "false" ]; then
		echo "REVIEW_HEAD_GATE mode=status pr=${PR_NUMBER:-?} head=${status_sha:-?} state=${status_state:-?} outcome=disabled"
		return 0
	fi
	if ! review_head_gate_valid_repo "${status_repo}" || ! [[ "${status_sha}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${status_state}" =~ ^(pending|success|failure)$ ]]; then
		echo "REVIEW_HEAD_GATE mode=status pr=${PR_NUMBER:-?} head=${status_sha:-?} state=${status_state:-?} outcome=invalid"
		return 0
	fi
	status_description="$(LC_ALL=C printf '%s' "${status_description}" | tr -cd ' -~' | cut -c1-140)"
	local -a status_args=(-X POST "repos/${status_repo}/statuses/${status_sha}" -f "state=${status_state}" -f "context=${REVIEW_HEAD_GATE_CONTEXT}" -f "description=${status_description}")
	if [[ "${RUN_URL:-}" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+$ ]]; then
		status_args+=(-f "target_url=${RUN_URL}")
	fi
	# The gate's existing PR GET supplies the head and enrollment, but cannot
	# publish a status; exactly one POST is needed for each state transition.
	if gh_retry gh api "${status_args[@]}" >/dev/null; then
		echo "REVIEW_HEAD_GATE mode=status pr=${PR_NUMBER:-?} head=${status_sha} state=${status_state} outcome=posted"
	else
		echo "::warning::REVIEW_HEAD_GATE mode=status pr=${PR_NUMBER:-?} head=${status_sha} state=${status_state} outcome=failed"
	fi
	return 0
}

# With an unknown enrollment, verify the live PR and expose its JSON via
# REVIEW_HEAD_GATE_LIVE_PR_JSON for the judge's head comparison. A known
# enrollment comes from the gate's existing authenticated PR read.
review_head_gate_withdraw_auto_merge()
{
	local withdraw_repo="${1:-}" withdraw_pr="${2:-}" known_enabled="${3:-}" withdraw_json=""
	REVIEW_HEAD_GATE_LIVE_PR_JSON=""
	if ! review_head_gate_valid_repo "${withdraw_repo}" || ! [[ "${withdraw_pr}" =~ ^[1-9][0-9]*$ ]]; then
		echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr:-?} outcome=failed reason=invalid_input"
		return 1
	fi
	if [ "${known_enabled}" = "false" ]; then
		echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr} outcome=not_enabled"
		return 0
	fi
	if [ "${known_enabled}" != "true" ]; then
		# The judge's earlier PR reads predate its decision; it has no live
		# enrollment cache. The gate passes its existing read when known.
		if ! withdraw_json="$(gh_retry gh api "repos/${withdraw_repo}/pulls/${withdraw_pr}" 2>/dev/null)"; then
			echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr} outcome=failed reason=read_failed"
			return 1
		fi
		if ! jq -e '.state == "open" and (.head.sha | type == "string") and has("auto_merge") and (.auto_merge == null or (.auto_merge | type == "object"))' <<< "${withdraw_json}" >/dev/null 2>&1; then
			echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr} outcome=failed reason=invalid_response"
			return 1
		fi
		REVIEW_HEAD_GATE_LIVE_PR_JSON="${withdraw_json}"
		if jq -e '.auto_merge == null' <<< "${withdraw_json}" >/dev/null 2>&1; then
			echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr} outcome=not_enabled"
			return 0
		fi
	fi
	if ! gh_retry gh pr merge "${withdraw_pr}" --repo "${withdraw_repo}" --disable-auto >/dev/null 2>&1; then
		echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr} outcome=failed reason=disable_failed"
		return 1
	fi
	echo "REVIEW_HEAD_GATE mode=withdraw pr=${withdraw_pr} outcome=disabled"
}

review_head_gate_main()
{
	if [ "${EVENT_NAME:-}" = "pull_request" ]; then
		if [ "${EVENT_ACTION:-}" = "opened" ] || [ "${EVENT_ACTION:-}" = "synchronize" ]; then
			local event_sha="${PR_EVENT_HEAD_SHA:-${GATE_HEAD_SHA:-}}"
			review_head_gate_post_status "${REPOSITORY:-}" "${event_sha}" pending "review pending for this head"
			if [ -n "${GATE_HEAD_SHA:-}" ] && [ "${event_sha}" != "${GATE_HEAD_SHA}" ]; then
				review_head_gate_post_status "${REPOSITORY:-}" "${GATE_HEAD_SHA}" pending "review pending for this head"
			fi
		fi
		if [ "${EVENT_ACTION:-}" = "synchronize" ] && [ "${REVIEW_STALE_AUTO_MERGE_WITHDRAW_ENABLED}" != "false" ]; then
			if ! review_head_gate_withdraw_auto_merge "${REPOSITORY:-}" "${PR_NUMBER:-}" "${GATE_AUTO_MERGE_ENABLED:-}"; then
				echo "::error::REVIEW_HEAD_GATE stale auto-merge withdrawal failed; refusing review gate" >&2
				return 1
			fi
		fi
	fi
	if [ "${DETERMINISTIC_SKIP:-false}" = "true" ]; then
		review_head_gate_post_status "${REPOSITORY:-}" "${GATE_HEAD_SHA:-}" success "deterministic skip evaluated this head"
	fi
}

# Freshness-update fast path (#7084, Q60: A). A merge path that finds the base
# moved under the PR's files updates the branch and posts
#   <!-- ai:merge-base-sync:v1 pr=<n> from=<approved head> sync=<k> -->
# (scripts/pr_checks_lib.sh). The synchronize run for the resulting head may
# skip the reviewer panel only when that head is provably nothing but
# GitHub's merge of the approved head with the base:
#   - the newest such marker by the workflow's own login names <pr>;
#   - the head commit has exactly two parents, the first is the marker's
#     `from`, and GitHub made it (committer `web-flow`, verified signature),
#     so its tree is GitHub's own merge result and no one edited it;
#   - the second parent is contained in <base> (`compare/<parent>...<base>`
#     is `ahead` or `identical`), so the only new code is already on the base.
# Usage: freshness-sync <repo> <pr> <head sha> <base ref> <author login> <comments json file>
# The comments file is the review gate's existing marker-comment read (an array
# of {author_login, body}); this adds no comments call. API budget (§15): one
# `commits/<head>` and one compare read, both only when a marker exists.
# Exit 0 = skip the reviewers; 1 = review normally. Every outcome logs one
# REVIEW_HEAD_GATE mode=freshness_sync line.
review_head_gate_freshness_sync()
{
	local fs_repo="${1:-}" fs_pr="${2:-}" fs_head="${3:-}" fs_base="${4:-}" fs_login="${5:-}" fs_comments="${6:-}"
	local fs_marker fs_from fs_sync fs_commit fs_parent_count fs_first fs_second fs_verified fs_committer fs_status
	fs_log()
	{
		echo "REVIEW_HEAD_GATE mode=freshness_sync pr=${fs_pr:-?} head=${fs_head:-?} outcome=$1 reason=$2${3:+ $3}"
	}
	if ! review_head_gate_valid_repo "${fs_repo}" || ! [[ "${fs_pr}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${fs_head}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! [[ "${fs_base}" =~ ^[A-Za-z0-9._/-]{1,200}$ ]] || [[ "${fs_base}" == *..* ]] \
		|| [ -z "${fs_login}" ] || [ ! -f "${fs_comments}" ]; then
		fs_log review invalid_input
		return 1
	fi
	fs_marker="$(jq -r --arg login "${fs_login}" --arg pr "${fs_pr}" '
		[ .[]? | select(type == "object" and (.author_login // "") == $login)
		  | (.body // "") | select(type == "string")
		  | capture("<!-- ai:merge-base-sync:v1 pr=(?<pr>[0-9]+) from=(?<from>[0-9a-f]{40}) sync=(?<sync>[0-9]+) -->\\s*$")?
		  | select(.pr == $pr) ] | last // empty | "\(.from) \(.sync)"' "${fs_comments}" 2>/dev/null || true)"
	if [ -z "${fs_marker}" ]; then
		fs_log review no_sync_marker
		return 1
	fi
	fs_from="${fs_marker%% *}"
	fs_sync="${fs_marker##* }"
	if ! fs_commit="$(gh_retry gh api "repos/${fs_repo}/commits/${fs_head}" \
		--jq '{parents: [.parents[]?.sha], verified: (.commit.verification.verified // false), committer: (.committer.login // "")}' 2>/dev/null)"; then
		fs_log review commit_read_failed "from=${fs_from} sync=${fs_sync}"
		return 1
	fi
	fs_parent_count="$(printf '%s' "${fs_commit}" | jq -r '.parents | length' 2>/dev/null || echo 0)"
	fs_first="$(printf '%s' "${fs_commit}" | jq -r '.parents[0] // ""' 2>/dev/null || true)"
	fs_second="$(printf '%s' "${fs_commit}" | jq -r '.parents[1] // ""' 2>/dev/null || true)"
	fs_verified="$(printf '%s' "${fs_commit}" | jq -r '.verified' 2>/dev/null || echo false)"
	fs_committer="$(printf '%s' "${fs_commit}" | jq -r '.committer' 2>/dev/null || true)"
	if [ "${fs_parent_count}" != "2" ] || [ "${fs_first}" != "${fs_from}" ]; then
		fs_log review head_is_not_the_update "from=${fs_from} parents=${fs_parent_count} first=${fs_first:-none}"
		return 1
	fi
	if [ "${fs_verified}" != "true" ] || [ "${fs_committer}" != "web-flow" ]; then
		fs_log review merge_not_made_by_github "from=${fs_from} verified=${fs_verified} committer=${fs_committer:-none}"
		return 1
	fi
	if ! [[ "${fs_second}" =~ ^[0-9a-f]{40}$ ]] \
		|| ! fs_status="$(gh_retry gh api "repos/${fs_repo}/compare/${fs_second}...${fs_base}" --jq '.status // ""' 2>/dev/null)"; then
		fs_log review base_compare_failed "from=${fs_from} base_parent=${fs_second:-none}"
		return 1
	fi
	case "${fs_status}" in
		ahead|identical) ;;
		*)
			fs_log review base_parent_not_on_base "from=${fs_from} base_parent=${fs_second} status=${fs_status:-none}"
			return 1
			;;
	esac
	fs_log skip freshness_sync "from=${fs_from} base_parent=${fs_second} sync=${fs_sync}"
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	set -euo pipefail
	# shellcheck source=/dev/null
	source "$(dirname -- "${BASH_SOURCE[0]}")/gh_helpers.sh"
	case "${1:-}" in
		gate) review_head_gate_main ;;
		freshness-sync) shift; review_head_gate_freshness_sync "$@" ;;
		*) echo "Usage: review_head_gate.sh gate | freshness-sync <repo> <pr> <head sha> <base ref> <author login> <comments json file>" >&2; exit 2 ;;
	esac
fi
