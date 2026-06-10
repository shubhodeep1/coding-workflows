#!/usr/bin/env bash

gh_api_safe()
{
	local output=""
	local err_file
	GH_API_SAFE_OUTPUT=""
	if [ -z "${RATE_LIMIT_BACKOFF+x}" ] || [[ ! "${RATE_LIMIT_BACKOFF}" =~ ^[0-9]+$ ]]; then
		RATE_LIMIT_BACKOFF=0
	fi
	err_file="$(mktemp)"
	if output="$(gh api "$@" 2>"${err_file}")"; then
		RATE_LIMIT_BACKOFF=0
		GH_API_SAFE_OUTPUT="${output}"
		rm -f "${err_file}"
		return 0
	fi

	if grep -qi "rate limit" "${err_file}" 2>/dev/null; then
		if [ "${RATE_LIMIT_BACKOFF}" -eq 0 ]; then
			RATE_LIMIT_BACKOFF=30
		elif [ "${RATE_LIMIT_BACKOFF}" -lt 120 ]; then
			RATE_LIMIT_BACKOFF=$((RATE_LIMIT_BACKOFF * 2))
		fi
		rm -f "${err_file}"
		sleep "${RATE_LIMIT_BACKOFF}"
	elif [ -s "${err_file}" ]; then
		echo "::error::gh api call failed: $*"
		cat "${err_file}" >&2
		rm -f "${err_file}"
	else
		rm -f "${err_file}"
	fi

	return 1
}

list_dispatch_runs()
{
	gh_api_safe "repos/${GITHUB_REPOSITORY}/actions/workflows/${WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=100"
}
