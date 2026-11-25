#!/usr/bin/env bash

# gh_api_safe_call_is_read ARGS...
#
# Returns 0 when the `gh api` arguments describe a read and 1 when they
# describe a write. A call is a write when it sets a method other than GET
# or HEAD (`-X <m>`, `-X<m>`, `--method <m>`, `--method=<m>`) or carries any request
# body flag (`-f`, `-F`, `--field`, `--raw-field`, `--input`), even next to
# `-X GET` or `-X HEAD`. gh_api_safe retries transient errors only for reads: a retried
# POST/PATCH whose first attempt reached GitHub could repeat a workflow
# dispatch or a comment.
gh_api_safe_call_is_read()
{
	local expect_method=0
	local skip_value=0
	local arg=""
	local method=""
	for arg in "$@"; do
		if [ "${skip_value}" = "1" ]; then
			skip_value=0
			continue
		fi
		if [ "${expect_method}" = "1" ]; then
			expect_method=0
			method="$(printf '%s' "${arg}" | tr '[:lower:]' '[:upper:]')"
			if [ "${method}" != "GET" ] && [ "${method}" != "HEAD" ]; then
				return 1
			fi
			continue
		fi
		case "${arg}" in
			-X|--method)
				expect_method=1
				;;
			--method=*|-X?*)
				if [ "${arg#--method=}" != "${arg}" ]; then
					method="${arg#--method=}"
				else
					method="${arg#-X}"
				fi
				method="$(printf '%s' "${method}" | tr '[:lower:]' '[:upper:]')"
				if [ "${method}" != "GET" ] && [ "${method}" != "HEAD" ]; then
					return 1
				fi
				;;
			-f|-F|--field|--raw-field|--input|-f?*|-F?*|--field=*|--raw-field=*|--input=*)
				return 1
				;;
			-q|--jq|-t|--template|-H|--header|-p|--preview|--hostname|--cache)
				skip_value=1
				;;
		esac
	done
	# A trailing `-X` / `--method` with no value is treated as a write.
	if [ "${expect_method}" = "1" ]; then
		return 1
	fi
	return 0
}

# gh_api_safe_stderr_is_transient ERR_FILE
#
# Returns 0 when the captured stderr names a transient failure: any
# `HTTP 5xx` status (500-599, as in the shared classifiers in
# scripts/gh_api_retry.py and scripts/gh_helpers.sh), a bare 502/503/504 (only
# as whole words, so a run ID, URL or commit SHA that contains "502" does not
# count), a connection reset, an i/o or header timeout, a TLS handshake
# timeout, or an EOF (matched as a whole word).
gh_api_safe_stderr_is_transient()
{
	local err_file="$1"
	if [ ! -s "${err_file}" ]; then
		return 1
	fi
	if grep -Eqi 'HTTP 5[0-9][0-9]|(^|[^[:alnum:]_])(502|503|504)([^[:alnum:]_]|$)|connection reset|i/o timeout|timeout awaiting|TLS handshake timeout|unexpected EOF|(^|[^[:alnum:]_])EOF([^[:alnum:]_]|$)' "${err_file}" 2>/dev/null; then
		return 0
	fi
	return 1
}

# gh_api_safe ARGS...
#
# Runs `gh api ARGS...` and stores its stdout in GH_API_SAFE_OUTPUT.
# Retries share one budget of 4 attempts:
# - a rate-limit error is retried for every call, waiting RATE_LIMIT_BACKOFF
#   (30 s, doubling up to 120 s), as before;
# - a transient error (see gh_api_safe_stderr_is_transient) is retried only
#   for reads (see gh_api_safe_call_is_read), waiting 5, 10, then 20 s, and
#   logs `GH_API_SAFE_RETRY attempt=<n>/<max> class=transient` to stderr,
#   also in quiet mode (the line carries no arguments, endpoint or token).
# Any other failure, and a transient failure on a write, fails at once.
#
# With GH_API_SAFE_USE_READ_TOKEN=1 (default 0, set inline by polling call
# sites, e.g. through gh_api_safe_quiet_print_read), a read is first tried
# once with the job token (gh_api_safe_job_token_read, issue #9888); when
# that does not succeed, the PAT loop above runs exactly as before.
#
# This helper stays self-contained on purpose and does not source
# scripts/gh_helpers.sh (gh_retry_to_file / gh_api_retry): it is sourced by
# release jobs and scripts/dispatch_and_watch_workflow_run.sh, where
# gh_helpers.sh may not be staged.
gh_api_safe()
{
	local output=""
	local err_file
	local quiet_stderr="${GH_API_SAFE_QUIET_STDERR:-0}"
	local attempt=1
	local max_attempts=4
	local is_read=1
	local transient_retries=0
	local transient_backoff=5
	GH_API_SAFE_OUTPUT=""
	gh_api_safe_call_is_read "$@" || is_read=0
	if [ -z "${RATE_LIMIT_BACKOFF+x}" ] || [[ ! "${RATE_LIMIT_BACKOFF}" =~ ^[0-9]+$ ]]; then
		RATE_LIMIT_BACKOFF=0
	fi
	# Opt-in (issue #9888): try a read once with the job token first.
	if [ "${GH_API_SAFE_USE_READ_TOKEN:-0}" = "1" ] && [ "${is_read}" = "1" ]; then
		if output="$(gh_api_safe_job_token_read "$@")"; then
			RATE_LIMIT_BACKOFF=0
			GH_API_SAFE_OUTPUT="${output}"
			return 0
		fi
	fi
	err_file="$(mktemp)"
	while true; do
		if output="$(gh api "$@" 2>"${err_file}")"; then
			RATE_LIMIT_BACKOFF=0
			GH_API_SAFE_OUTPUT="${output}"
			rm -f "${err_file}"
			return 0
		fi

		if grep -qi "rate limit" "${err_file}" 2>/dev/null; then
			if [ "${attempt}" -ge "${max_attempts}" ]; then
				rm -f "${err_file}"
				return 1
			fi
			if [ "${RATE_LIMIT_BACKOFF}" -eq 0 ]; then
				RATE_LIMIT_BACKOFF=30
			elif [ "${RATE_LIMIT_BACKOFF}" -lt 120 ]; then
				RATE_LIMIT_BACKOFF=$((RATE_LIMIT_BACKOFF * 2))
			fi
			: > "${err_file}"
			sleep "${RATE_LIMIT_BACKOFF}"
			attempt=$((attempt + 1))
			continue
		fi

		if [ "${is_read}" = "1" ] && [ "${attempt}" -lt "${max_attempts}" ] && gh_api_safe_stderr_is_transient "${err_file}"; then
			transient_retries=$((transient_retries + 1))
			case "${transient_retries}" in
				1) transient_backoff=5 ;;
				2) transient_backoff=10 ;;
				*) transient_backoff=20 ;;
			esac
			echo "GH_API_SAFE_RETRY attempt=${attempt}/${max_attempts} class=transient" >&2
			: > "${err_file}"
			sleep "${transient_backoff}"
			attempt=$((attempt + 1))
			continue
		fi

		if [ -s "${err_file}" ]; then
			if [ "${quiet_stderr}" != "1" ]; then
				echo "::error::gh api call failed: $*"
				cat "${err_file}" >&2
			fi
		fi
		rm -f "${err_file}"
		return 1
	done
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

# gh_api_safe_job_token_read ARGS...
#
# The release-gate twin of gh_read in scripts/gh_helpers.sh (issue #9888);
# this file stays self-contained (see gh_api_safe). Runs `gh api ARGS...`
# once with the job's GITHUB_TOKEN (GH_READ_TOKEN) and prints its stdout,
# with the same rules as gh_read:
# - reads only (gh_api_safe_call_is_read; the graphql endpoint is refused);
# - same repository only, judged from the `repos/<owner>/<repo>` endpoint;
# - the GH_READ_TOKEN_ENABLED kill switch (default true) and the same
#   per-job latch file, written on a 401 or a non-rate-limit 403;
# - xtrace off while the token is in use; gh's stderr is never replayed;
# - one `GH_READ_FALLBACK reason=<401|403|404|rate_limit> caller=<script>`
#   line to stderr when the job token is refused or rate limited.
# Returns 0 with the output on success; 2 for a call that is not a read
# (gh is not run); 3 when the caller should use the PAT (feature off, no
# token, latch set, another repository, or a logged fallback); 1 on any
# other failure. Makes at most one API call. xtrace is switched off before
# anything reads GH_READ_TOKEN and restored on return.
gh_api_safe_job_token_read()
{
	local job_token_read_xtrace=0 job_token_read_rc=0
	case "$-" in
		*x*) job_token_read_xtrace=1 ;;
	esac
	{ set +x; } 2>/dev/null
	_gh_api_safe_job_token_read_run "$@" || job_token_read_rc=$?
	if [ "${job_token_read_xtrace}" = "1" ]; then
		set -x
	fi
	return "${job_token_read_rc}"
}

_gh_api_safe_job_token_read_run()
{
	local arg="" endpoint="" target_repo="" skip_value=0 rc=0 xtrace_was_on=0 reason=""
	local out_file="" err_file="" latch_key="" latch_file="" caller="" caller_idx=1
	if ! gh_api_safe_call_is_read "$@"; then
		echo "gh_api_safe_job_token_read: refused a call that is not a read" >&2
		return 2
	fi
	for arg in "$@"; do
		if [ "${skip_value}" = "1" ]; then
			skip_value=0
			continue
		fi
		case "${arg}" in
			-X|--method|-q|--jq|-t|--template|-H|--header|-p|--preview|--hostname|--cache)
				skip_value=1
				;;
			-*)
				;;
			*)
				endpoint="${arg}"
				break
				;;
		esac
	done
	case "${endpoint#/}" in
		graphql|graphql\?*)
			echo "gh_api_safe_job_token_read: refused a call that is not a read" >&2
			return 2
			;;
	esac
	local read_token_enabled="${GH_READ_TOKEN_ENABLED:-true}"
	case "${read_token_enabled,,}" in
		false|0|no|off) return 3 ;;
	esac
	[ -n "${GH_READ_TOKEN:-}" ] || return 3
	latch_key="${GITHUB_RUN_ID:-local}_${GITHUB_RUN_ATTEMPT:-1}_${GITHUB_JOB:-job}"
	latch_key="${latch_key//[^A-Za-z0-9_.-]/_}"
	latch_file="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/gh_read_refused_${latch_key}"
	[ ! -e "${latch_file}" ] || return 3
	if [[ "${endpoint}" =~ ^/?repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)([/?#]|$) ]]; then
		target_repo="${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
	fi
	if [ -z "${target_repo}" ] || [ -z "${GITHUB_REPOSITORY:-}" ] || [ "${target_repo,,}" != "${GITHUB_REPOSITORY,,}" ]; then
		return 3
	fi
	out_file="$(mktemp)" || return 3
	if ! err_file="$(mktemp)"; then
		rm -f "${out_file}"
		return 3
	fi
	case "$-" in
		*x*) xtrace_was_on=1 ;;
	esac
	{ set +x; } 2>/dev/null
	GH_TOKEN="${GH_READ_TOKEN}" GITHUB_TOKEN="${GH_READ_TOKEN}" gh api "$@" >"${out_file}" 2>"${err_file}" || rc=$?
	if [ "${xtrace_was_on}" = "1" ]; then
		set -x
	fi
	if [ "${rc}" -eq 0 ]; then
		cat "${out_file}"
		rm -f "${out_file}" "${err_file}"
		return 0
	fi
	if grep -qiE 'rate limit|abuse detection|secondary rate|HTTP 429' "${err_file}" 2>/dev/null; then
		reason="rate_limit"
	elif grep -q 'HTTP 401' "${err_file}" 2>/dev/null; then
		reason="401"
	elif grep -q 'HTTP 403' "${err_file}" 2>/dev/null; then
		reason="403"
	elif grep -q 'HTTP 404' "${err_file}" 2>/dev/null; then
		reason="404"
	fi
	rm -f "${out_file}" "${err_file}"
	[ -n "${reason}" ] || return 1
	if [ "${reason}" = "401" ] || [ "${reason}" = "403" ]; then
		: > "${latch_file}" 2>/dev/null || true
	fi
	while [ "${caller_idx}" -lt "${#BASH_SOURCE[@]}" ]; do
		caller="$(basename -- "${BASH_SOURCE[${caller_idx}]:-}" 2>/dev/null || true)"
		if [ -n "${caller}" ] && [ "${caller}" != "comprehensive_test_and_release_gh_api.sh" ]; then
			break
		fi
		caller=""
		caller_idx=$((caller_idx + 1))
	done
	caller="${caller//[^A-Za-z0-9_.-]/}"
	echo "GH_READ_FALLBACK reason=${reason} caller=${caller:-unknown}" >&2
	return 3
}

# gh_api_safe_quiet_print_read ARGS...
#
# gh_api_safe_quiet_print for a polling read: tries the job token first
# (GH_API_SAFE_USE_READ_TOKEN=1, issue #9888), then the PAT as before.
gh_api_safe_quiet_print_read()
{
	GH_API_SAFE_USE_READ_TOKEN=1 gh_api_safe_quiet_print "$@"
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
