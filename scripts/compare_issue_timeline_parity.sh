#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURE_DIR="${SCRIPT_DIR}/fixtures/issue-timeline"
REST_FIXTURE="${1:-${FIXTURE_DIR}/rest_timeline_fixture.json}"
GRAPHQL_FIXTURE="${2:-${FIXTURE_DIR}/graphql_timeline_fixture.json}"
REST_PR_FIXTURE="${3:-${FIXTURE_DIR}/rest_pr_with_comments_fixture.json}"
GRAPHQL_PR_FIXTURE="${4:-${FIXTURE_DIR}/graphql_pr_with_comments_fixture.json}"

if [ ! -s "${REST_FIXTURE}" ] || [ ! -s "${GRAPHQL_FIXTURE}" ] || [ ! -s "${REST_PR_FIXTURE}" ] || [ ! -s "${GRAPHQL_PR_FIXTURE}" ]; then
	echo "Missing fixture(s): timeline_rest=${REST_FIXTURE} timeline_graphql=${GRAPHQL_FIXTURE} pr_rest=${REST_PR_FIXTURE} pr_graphql=${GRAPHQL_PR_FIXTURE}" >&2
	exit 1
fi

normalize_merged_evidence() {
	local fixture="$1"
	jq -c '
		[
			.[]
			| select(.event == "cross-referenced")
			| select(.source.issue.pull_request.url? | type == "string")
			| ((.source.issue.merged // false) == true)
		] | any
	' "${fixture}"
}

normalize_cross_ref_shape() {
	local fixture="$1"
	jq -c '
		[
			.[]
			| select(.event == "cross-referenced")
			| select(.source.issue.pull_request.url? | type == "string")
			| {
				number: .source.issue.number,
				url: .source.issue.pull_request.url
			}
		] | unique
	' "${fixture}"
}

normalize_latest_linked_pr() {
	local fixture="$1"
	jq -c '
		[
			.[]
			| select(.event == "cross-referenced")
			| select(.source.issue.pull_request != null)
			| .source.issue.number
		] | last // null
	' "${fixture}"
}

normalize_pr_context_shape() {
	local fixture="$1"
	jq -c '
		{
			meta: (.meta // {}),
			comments: (
				(.comments // [])
				| map({author: .author, body: .body, created_at: .created_at})
				| sort_by((.created_at // ""), (.author // ""), (.body // ""))
			),
			review_comments: (
				(.review_comments // [])
				| map({author: .author, path: .path, line: .line, body: .body})
				| sort_by((.path // ""), (.line // 0), (.author // ""), (.body // ""))
			)
		}
	' "${fixture}"
}

assert_equal() {
	local label="$1"
	local left="$2"
	local right="$3"
	if [ "${left}" != "${right}" ]; then
		echo "[FAIL] ${label}" >&2
		echo "  REST:    ${left}" >&2
		echo "  GraphQL: ${right}" >&2
		exit 1
	fi
	echo "[OK] ${label}"
}

rest_merged_evidence="$(normalize_merged_evidence "${REST_FIXTURE}")"
graphql_merged_evidence="$(normalize_merged_evidence "${GRAPHQL_FIXTURE}")"
assert_equal "merged-pr evidence" "${rest_merged_evidence}" "${graphql_merged_evidence}"

rest_cross_ref_shape="$(normalize_cross_ref_shape "${REST_FIXTURE}")"
graphql_cross_ref_shape="$(normalize_cross_ref_shape "${GRAPHQL_FIXTURE}")"
assert_equal "cross-ref url/number extraction" "${rest_cross_ref_shape}" "${graphql_cross_ref_shape}"

rest_latest_linked_pr="$(normalize_latest_linked_pr "${REST_FIXTURE}")"
graphql_latest_linked_pr="$(normalize_latest_linked_pr "${GRAPHQL_FIXTURE}")"
assert_equal "latest linked PR selection" "${rest_latest_linked_pr}" "${graphql_latest_linked_pr}"

rest_pr_context="$(normalize_pr_context_shape "${REST_PR_FIXTURE}")"
graphql_pr_context="$(normalize_pr_context_shape "${GRAPHQL_PR_FIXTURE}")"
assert_equal "pr context parity (meta/comments/review_comments)" "${rest_pr_context}" "${graphql_pr_context}"

echo "Parity checks passed."
