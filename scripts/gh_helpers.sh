#!/usr/bin/env bash
# gh_helpers.sh — Rate-limit-aware GitHub API retry helpers.
#
# Source this file in workflow steps and shell scripts that interact
# with the GitHub API via the `gh` CLI or `curl`.
#
# Provides:
#   gh_retry          — run a gh CLI command with rate-limit detection + retry
#   gh_retry_to_file  — like gh_retry but captures stdout to a specified file
#   curl_gh_api       — run a curl command against GitHub API with retry
#
# Rate limit detection: on 403/429 "rate limit" responses the helper
# sleeps 30 s before retrying.  Other transient failures use exponential
# backoff (1 s, 2 s, 4 s, …).

# Guard against double-sourcing
if [ "${_GH_HELPERS_LOADED:-}" = "1" ]; then
	return 0 2>/dev/null || true
fi
_GH_HELPERS_LOADED=1

# ---------------------------------------------------------------
# _is_gh_rate_limit — detect rate-limit text in stderr / body.
# Returns 0 (true) if the text indicates a rate limit.
# ---------------------------------------------------------------
_is_gh_rate_limit()
{
	printf '%s' "$1" | grep -qiE 'rate limit|abuse detection|secondary rate|HTTP 429'
}

# ---------------------------------------------------------------
# gh_retry — Execute a gh CLI command with automatic retry.
#
# Rate-limit errors  → sleep 30 s, retry (up to max_attempts).
# Other failures     → exponential backoff 1 s, 2 s, 4 s, …
#
# Usage:
#   gh_retry gh api repos/owner/repo/issues
#   gh_retry gh issue edit 42 --add-label bug
# ---------------------------------------------------------------
gh_retry()
{
	local max_attempts="${GH_RETRY_MAX_ATTEMPTS:-5}"
	local attempt=1
	local stderr_file
	stderr_file=$(mktemp "${TMPDIR:-/tmp}/gh_retry_stderr.XXXXXX")

	while [ "${attempt}" -le "${max_attempts}" ]; do
		if "$@" 2>"${stderr_file}"; then
			rm -f "${stderr_file}"
			return 0
		fi

		local stderr_content
		stderr_content=$(cat "${stderr_file}" 2>/dev/null || true)

		if _is_gh_rate_limit "${stderr_content}"; then
			echo "::warning::GitHub API rate limit hit (attempt ${attempt}/${max_attempts}), sleeping 30s before retry…" >&2
			sleep 30
		else
			local wait_secs=$(( 2 ** (attempt - 1) ))
			echo "::warning::gh command failed (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
			if [ -n "${stderr_content}" ]; then
				echo "::warning::  stderr: ${stderr_content}" >&2
			fi
			sleep "${wait_secs}"
		fi

		attempt=$(( attempt + 1 ))
	done

	echo "::error::gh command failed after ${max_attempts} attempts: $*" >&2
	if [ -s "${stderr_file}" ]; then
		cat "${stderr_file}" >&2
	fi
	rm -f "${stderr_file}"
	return 1
}

# ---------------------------------------------------------------
# gh_retry_to_file — Like gh_retry but captures stdout to a file.
#
# Each retry truncates the file so only the last attempt's output
# remains.  Useful for paginated / raw-content downloads.
#
# Usage:
#   gh_retry_to_file /tmp/result.json gh api repos/owner/repo/pulls/1
# ---------------------------------------------------------------
gh_retry_to_file()
{
	local outfile="$1"; shift
	local max_attempts="${GH_RETRY_MAX_ATTEMPTS:-5}"
	local attempt=1
	local stderr_file
	stderr_file=$(mktemp "${TMPDIR:-/tmp}/gh_retry_stderr.XXXXXX")

	while [ "${attempt}" -le "${max_attempts}" ]; do
		if "$@" > "${outfile}" 2>"${stderr_file}"; then
			rm -f "${stderr_file}"
			return 0
		fi

		local stderr_content
		stderr_content=$(cat "${stderr_file}" 2>/dev/null || true)

		if _is_gh_rate_limit "${stderr_content}"; then
			echo "::warning::GitHub API rate limit hit (attempt ${attempt}/${max_attempts}), sleeping 30s before retry…" >&2
			sleep 30
		else
			local wait_secs=$(( 2 ** (attempt - 1) ))
			echo "::warning::gh command failed (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
			if [ -n "${stderr_content}" ]; then
				echo "::warning::  stderr: ${stderr_content}" >&2
			fi
			sleep "${wait_secs}"
		fi

		attempt=$(( attempt + 1 ))
	done

	echo "::error::gh command failed after ${max_attempts} attempts: $*" >&2
	if [ -s "${stderr_file}" ]; then
		cat "${stderr_file}" >&2
	fi
	rm -f "${stderr_file}"
	return 1
}

# ---------------------------------------------------------------
# curl_gh_api — curl wrapper with rate-limit retry for GitHub API.
#
# Captures HTTP status code.  On 429 or 403-with-rate-limit body,
# sleeps 30 s and retries.  Other errors use exponential backoff.
# Outputs the response body on success (HTTP 2xx).
#
# Usage:
#   curl_gh_api -s \
#       -H "Authorization: token ${GH_TOKEN}" \
#       -H "Accept: application/vnd.github.v3+json" \
#       "https://api.github.com/repos/owner/repo/issues/1/comments"
# ---------------------------------------------------------------
curl_gh_api()
{
	local max_attempts="${GH_RETRY_MAX_ATTEMPTS:-5}"
	local attempt=1
	local body_file
	body_file=$(mktemp "${TMPDIR:-/tmp}/curl_gh_body.XXXXXX")

	while [ "${attempt}" -le "${max_attempts}" ]; do
		: > "${body_file}"
		local http_code
		http_code=$(curl -o "${body_file}" -w '%{http_code}' "$@" 2>/dev/null) || http_code="000"

		if [ "${http_code}" -ge 200 ] 2>/dev/null && [ "${http_code}" -lt 300 ] 2>/dev/null; then
			cat "${body_file}"
			rm -f "${body_file}"
			return 0
		fi

		local body_content
		body_content=$(cat "${body_file}" 2>/dev/null || true)

		if [ "${http_code}" = "429" ] || { [ "${http_code}" = "403" ] && _is_gh_rate_limit "${body_content}"; }; then
			echo "::warning::GitHub API rate limit (HTTP ${http_code}, attempt ${attempt}/${max_attempts}), sleeping 30s…" >&2
			sleep 30
		else
			local wait_secs=$(( 2 ** (attempt - 1) ))
			echo "::warning::GitHub API curl failed HTTP ${http_code} (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
			sleep "${wait_secs}"
		fi

		attempt=$(( attempt + 1 ))
	done

	rm -f "${body_file}"
	echo "::error::GitHub API curl failed after ${max_attempts} attempts" >&2
	return 1
}
