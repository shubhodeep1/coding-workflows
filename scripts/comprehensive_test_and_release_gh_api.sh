#!/usr/bin/env bash

# Release polling uses the same bounded GitHub retry policy as other jobs.
# This file is sourced only from the checked-out workflow support tree.
_COMPREHENSIVE_GH_API_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${_COMPREHENSIVE_GH_API_DIR}/gh_helpers.sh"
unset _COMPREHENSIVE_GH_API_DIR

gh_api_safe()
{
	local output=""
	local quiet_stderr="${GH_API_SAFE_QUIET_STDERR:-0}"
	GH_API_SAFE_OUTPUT=""
	if [ -z "${RATE_LIMIT_BACKOFF+x}" ] || [[ ! "${RATE_LIMIT_BACKOFF}" =~ ^[0-9]+$ ]]; then
		RATE_LIMIT_BACKOFF=0
	fi
	local _retry_rc=0
	if [ "${quiet_stderr}" = 1 ]; then
		output="$(GH_RETRY_MAX_ATTEMPTS=4 gh_retry gh api "$@" 2>/dev/null)" || _retry_rc=$?
	else
		output="$(GH_RETRY_MAX_ATTEMPTS=4 gh_retry gh api "$@")" || _retry_rc=$?
	fi
	if [ "${_retry_rc}" -ne 0 ]; then
		if [ "${quiet_stderr}" != 1 ]; then echo "::error::gh api call failed"; fi
		return "${_retry_rc}"
	fi
	RATE_LIMIT_BACKOFF=0
	GH_API_SAFE_OUTPUT="${output}"
	return 0
}

gh_api_safe_print()
{
	if gh_api_safe "$@"; then
		printf '%s' "${GH_API_SAFE_OUTPUT}"
		return 0
	fi

	return 1
}

gh_api_safe_quiet()
{
	GH_API_SAFE_QUIET_STDERR=1 gh_api_safe "$@"
}

gh_api_safe_quiet_print()
{
	if GH_API_SAFE_QUIET_STDERR=1 gh_api_safe "$@"; then
		printf '%s' "${GH_API_SAFE_OUTPUT}"
		return 0
	fi

	return 1
}

list_dispatch_runs()
{
	gh_api_safe "repos/${GITHUB_REPOSITORY}/actions/workflows/${WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=100"
}

# find_latest_scoped_run_field REPO CREATED_AFTER NAME_RE TITLE FIELD [MAX_PAGES]
#
# Prints FIELD of the newest workflow run in REPO created after
# CREATED_AFTER whose name matches NAME_RE (case-insensitive), whose
# conclusion is not `skipped`, and, when TITLE is non-empty, whose
# display_title equals TITLE. Prints nothing when no run matches.
#
# The runs endpoint lists every workflow of the repo newest first, and the
# release window alone can create more than 100 runs: release run
# 36374918973 saw 166 runs between the smoke issue and the Plan capture,
# with the real Plan run just past position 100 behind two `skipped` Plan
# runs for the same issue, so a single page missed it and deep verify failed.
# Pages are therefore read in order until one holds a match or comes back
# short (the last page).
#
# API calls: one REST read per 100 runs, newest first, at most MAX_PAGES
# reads (default FIND_LATEST_SCOPED_RUN_MAX_PAGES, 5; the release smoke
# test's run-ID captures pass 10, GitHub's 1,000-result ceiling for this
# list). A page with a match ends the scan, so the usual cost is one read.
# Failure: returns 1 when a page cannot be read, or is not a runs listing,
# and nothing matched yet (callers already fall back with `|| echo ""`).
# When MAX_PAGES is given, returns 2 when every page up to it was full and
# none matched, so the caller need not retry a walk that would repeat the
# same reads (PR #4730 review). Returns 0 otherwise.
FIND_LATEST_SCOPED_RUN_MAX_PAGES=5

find_latest_scoped_run_field()
{
	local repo="$1"
	local created_after="$2"
	local name_re="$3"
	local title="$4"
	local field="$5"
	local max_pages="${6:-${FIND_LATEST_SCOPED_RUN_MAX_PAGES}}"
	local page=1
	local runs_json=""
	local match=""
	local page_count=0

	while [ "${page}" -le "${max_pages}" ]; do
		if ! runs_json="$(gh_api_safe_print "repos/${repo}/actions/runs?per_page=100&page=${page}&created=>${created_after}")"; then
			return 1
		fi
		# A page that is not a runs listing would otherwise end the scan as a
		# short page (PR #4730 conformance audit).
		if ! printf '%s' "${runs_json}" | jq -e '.workflow_runs | type == "array"' >/dev/null 2>&1; then
			return 1
		fi
		match="$(printf '%s' "${runs_json}" | jq -r \
			--arg name_re "${name_re}" \
			--arg title "${title}" \
			--arg field "${field}" \
			'[.workflow_runs[]
				| select(.name | test($name_re; "i"))
				| select(.conclusion != "skipped")
				| select($title == "" or .display_title == $title)]
				| sort_by(.created_at)
				| last
				| .[$field] // empty' 2>/dev/null || echo "")"
		if [ -n "${match}" ] && [ "${match}" != "null" ]; then
			printf '%s' "${match}"
			return 0
		fi
		page_count="$(printf '%s' "${runs_json}" | jq -r '.workflow_runs | length' 2>/dev/null || echo 0)"
		if ! [[ "${page_count}" =~ ^[0-9]+$ ]] || [ "${page_count}" -lt 100 ]; then
			return 0
		fi
		page=$((page + 1))
	done
	if [ -n "${6:-}" ]; then
		return 2
	fi
	return 0
}
