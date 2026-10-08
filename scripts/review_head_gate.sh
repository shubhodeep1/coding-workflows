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

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	set -euo pipefail
	# shellcheck source=/dev/null
	source "$(dirname -- "${BASH_SOURCE[0]}")/gh_helpers.sh"
	case "${1:-}" in
		gate) review_head_gate_main ;;
		*) echo "Usage: review_head_gate.sh gate" >&2; exit 2 ;;
	esac
fi
