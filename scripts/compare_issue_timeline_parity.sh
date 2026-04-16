#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURE_DIR="${SCRIPT_DIR}/fixtures/issue-timeline"
REST_FIXTURE="${1:-${FIXTURE_DIR}/rest_timeline_fixture.json}"
GRAPHQL_FIXTURE="${2:-${FIXTURE_DIR}/graphql_timeline_fixture.json}"

if [ ! -s "${REST_FIXTURE}" ] || [ ! -s "${GRAPHQL_FIXTURE}" ]; then
	echo "Missing fixture(s): REST=${REST_FIXTURE} GraphQL=${GRAPHQL_FIXTURE}" >&2
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

echo "Parity checks passed."
