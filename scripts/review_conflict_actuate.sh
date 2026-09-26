#!/usr/bin/env bash
set -euo pipefail

source "${SUPPORT_SCRIPTS_DIR:-scripts}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

candidate_file="${RESOLVER_RETRY_STATE_CANDIDATE_FILE:-}"
actuation_required="${RESOLVER_ACTUATION_REQUIRED:-false}"
if [ ! -s "${candidate_file:-/nonexistent}" ] && [ "${actuation_required}" != "true" ]; then
	exit 0
fi
if [ "${IS_INTEGRATION_SYNC:-false}" != "true" ]; then
	exit 0
fi
if ! [[ "${PR_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] \
	|| ! [[ "${INTEGRATION_TRACKING_NUM:-}" =~ ^[1-9][0-9]*$ ]] \
	|| [ "${TARGET_BRANCH:-}" != "orchestrator/project-${INTEGRATION_TRACKING_NUM:-0}" ] \
	|| ! [[ "${GITHUB_REPOSITORY:-}" =~ ^[^/]+/[^/]+$ ]]; then
	echo "::error::Resolver actuation context is invalid."
	exit 1
fi

live_pr_json="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}")"
live_head_sha="$(printf '%s' "${live_pr_json}" | jq -r '.head.sha // empty')"
expected_head_sha="$(jq -r '.head.sha // empty' "${PR_PAYLOAD_FILE}" 2>/dev/null || echo '')"
if ! [[ "${live_head_sha}" =~ ^[0-9a-f]{40}$ ]] || [ "${live_head_sha}" != "${expected_head_sha}" ]; then
	echo "::warning::Resolver actuation discarded because the PR head changed."
	exit 0
fi

producer_json="$(gh_retry gh api user)"
producer_id="$(printf '%s' "${producer_json}" | jq -r '.id // empty')"
if ! [[ "${producer_id}" =~ ^[1-9][0-9]*$ ]]; then
	echo "::error::Resolver actuation could not establish producer identity."
	exit 1
fi

if [ -s "${candidate_file:-/nonexistent}" ]; then
	signed_file="$(mktemp)"
	comment_file="$(mktemp)"
	selection_file="$(mktemp)"
	trap 'rm -f "${signed_file}" "${comment_file}" "${selection_file}"' EXIT
	PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR:-scripts}/orchestrate_state_v2.py" sign-resolver-retry \
		--candidate-file "${candidate_file}" \
		--repository "${GITHUB_REPOSITORY}" \
		--tracking-issue "${INTEGRATION_TRACKING_NUM}" \
		--integration-branch "${TARGET_BRANCH}" \
		--source-pr "${PR_NUMBER}" \
		--head-sha "${live_head_sha}" \
		--producer-id "${producer_id}" \
		--out-file "${signed_file}"
	{
		printf '%s\n' '<!-- AUTOFIX_RESOLVER_RETRY_STATE_V2'
		cat "${signed_file}"
		printf '%s' '-->'
	} > "${comment_file}"
	existing_comment_id="${RESOLVER_RETRY_STATE_COMMENT_ID:-}"
	if ! [[ "${existing_comment_id}" =~ ^[1-9][0-9]*$ ]]; then
		if [ ! -r "${PR_ISSUE_COMMENTS_FILE:-}" ]; then
			echo "::error::Resolver retry-state comment snapshot is unavailable; refusing to create a duplicate marker."
			exit 1
		fi
		if PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR:-scripts}/orchestrate_state_v2.py" select-resolver-retry \
			--comments-json "${PR_ISSUE_COMMENTS_FILE}" \
			--repository "${GITHUB_REPOSITORY}" \
			--tracking-issue "${INTEGRATION_TRACKING_NUM}" \
			--integration-branch "${TARGET_BRANCH}" \
			--source-pr "${PR_NUMBER}" \
			--head-sha "${live_head_sha}" \
			--producer-id "${producer_id}" \
			--out-file "${selection_file}"; then
			existing_comment_id="$(jq -r '.comment_id' "${selection_file}")"
		else
			selector_rc=$?
			if [ "${selector_rc}" -ne 1 ]; then
				echo "::error::Resolver retry-state lookup failed; refusing to create a duplicate marker."
				exit 1
			fi
		fi
	fi
	comment_payload="$(jq -n --rawfile body "${comment_file}" '{body:$body}')"
	if [[ "${existing_comment_id}" =~ ^[1-9][0-9]*$ ]]; then
		persisted_comment_json="$(printf '%s' "${comment_payload}" | gh_retry gh api -X PATCH "repos/${GITHUB_REPOSITORY}/issues/comments/${existing_comment_id}" --input -)"
	else
		persisted_comment_json="$(printf '%s' "${comment_payload}" | gh_retry gh api -X POST "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" --input -)"
	fi
	persisted_comment_id="$(printf '%s' "${persisted_comment_json}" | jq -r '.id // empty' 2>/dev/null || true)"
	if ! [[ "${persisted_comment_id}" =~ ^[1-9][0-9]*$ ]]; then
		echo "::error::Resolver retry-state comment write did not return a valid comment ID."
		exit 1
	fi
	if [ "$(jq -r '.escalated // false' "${signed_file}")" = "true" ]; then
		gh_retry gh issue edit "${PR_NUMBER}" --repo "${GITHUB_REPOSITORY}" --add-label "ai:resolver-escalated" >/dev/null
	fi
	# The existing PR read established the head used to sign the envelope.
	# Store the locator on that immutable commit instead of replacing the PR body.
	if ! gh_retry gh api -X POST "repos/${GITHUB_REPOSITORY}/statuses/${live_head_sha}" \
		-f state=success \
		-f context=ai/resolver-retry-state-locator \
		-f description="comment_id=${persisted_comment_id}" >/dev/null; then
		echo "::warning::Could not persist the resolver retry-state locator; continuing after the authoritative comment update."
	fi
fi

if [ "${actuation_required}" = "true" ] && [ -x "${SUPPORT_SCRIPTS_DIR:-scripts}/orchestrate_force_tick.sh" ]; then
	bash "${SUPPORT_SCRIPTS_DIR:-scripts}/orchestrate_force_tick.sh" \
		--repo "${GITHUB_REPOSITORY}" \
		--issue "${PR_NUMBER}" \
		--reason "resolver-failed" \
		--source-workflow "review_conflict_actuate" \
		--run-id "${GITHUB_RUN_ID:-}" || true
fi
