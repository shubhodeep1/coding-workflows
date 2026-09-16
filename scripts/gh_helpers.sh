#!/usr/bin/env bash
# gh_helpers.sh — Rate-limit-aware GitHub API retry helpers.
#
# Source this file in workflow steps and shell scripts that interact
# with the GitHub API via the `gh` CLI or `curl`.
#
# Provides:
#   gh_retry             — run a gh CLI command with rate-limit detection + retry
#   gh_retry_to_file     — like gh_retry but captures stdout to a specified file
#   gh_api_json_to_file  — like gh_retry_to_file but also validates JSON output
#   curl_gh_api          — run a curl command against GitHub API with retry
#
# Rate limit detection: on 403/429 "rate limit" responses the helper
# queries GitHub's GET /rate_limit endpoint (not itself rate-limited)
# to read X-RateLimit-Reset, then sleeps until reset+1 s (capped at
# 600 s, floored at 1 s, fallback 30 s).  Other transient failures
# use exponential backoff (1 s, 2 s, 4 s, …).  curl_gh_api reads the
# 403/429 response headers directly instead: a numeric Retry-After
# (secondary rate limit) takes precedence over X-RateLimit-Reset —
# see _parse_reset_header.

# Guard against double-sourcing
if [ "${_GH_HELPERS_LOADED:-}" = "1" ]; then
	return 0 2>/dev/null || true
fi
_GH_HELPERS_LOADED=1

_GH_HELPERS_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "scripts")"
if [ -f "${_GH_HELPERS_SCRIPT_DIR}/emit_event.sh" ]; then
	# shellcheck disable=SC1091
	source "${_GH_HELPERS_SCRIPT_DIR}/emit_event.sh"
elif [ -f "scripts/emit_event.sh" ]; then
	# shellcheck disable=SC1091
	source scripts/emit_event.sh
fi
unset _GH_HELPERS_SCRIPT_DIR
if ! type emit_event >/dev/null 2>&1; then
	emit_event()
	{
		return 0
	}
fi

# ---------------------------------------------------------------
# _is_gh_rate_limit — detect rate-limit text in stderr / body.
# Returns 0 (true) if the text indicates a rate limit.
# ---------------------------------------------------------------
_is_gh_rate_limit()
{
	printf '%s' "$1" | grep -qiE 'rate limit|abuse detection|secondary rate|HTTP 429'
}

# ---------------------------------------------------------------
# _gh_actions_escape — escape a string for safe interpolation into
# a GitHub Actions workflow command (e.g. ::warning::, ::error::).
#
# Per the workflow-command format, '%', CR, and LF must be encoded
# so they cannot terminate the command line, break annotations, or
# enable workflow-command injection from untrusted stderr.
#
#   %  → %25     (must be first to avoid double-encoding)
#   \r → %0D
#   \n → %0A
# ---------------------------------------------------------------
_gh_actions_escape()
{
	local s="$1"
	s="${s//%/%25}"
	s="${s//$'\r'/%0D}"
	s="${s//$'\n'/%0A}"
	printf '%s' "${s}"
}

# ---------------------------------------------------------------
# _is_gh_permanent_failure — detect deterministic, non-retryable
# failures that no amount of retrying will fix.
#
# Matches:
#   * label-edit idempotent cases:   'X' not found
#     (gh prints this when --remove-label targets a label that is
#     not on the issue; retrying just burns API budget.)
#   * HTTP 404 Not Found             (resource doesn't exist)
#   * HTTP 422 Unprocessable Entity  (validation / already-exists)
#   * "Resource not accessible by integration"      (GITHUB_TOKEN scope)
#   * "Resource not accessible by personal access token" (PAT scope)
#
# Returns 0 (true) if the text indicates such a failure.
# Conservative on purpose — only matches conditions that retrying
# cannot resolve.
# ---------------------------------------------------------------
_is_gh_permanent_failure()
{
	printf '%s' "$1" | grep -qiE "['\"][^'\"]+['\"] not found|gh: Not Found|HTTP 404|404 Not Found|status code 404|HTTP 422|Resource not accessible by (integration|personal access token)"
}

# ---------------------------------------------------------------
# _sleep_until_reset — compute wait from an epoch timestamp.
#
# Caps at 600 s, floors at 1 s, falls back to 30 s if the
# timestamp is empty or unparseable.
# ---------------------------------------------------------------
_sleep_until_reset()
{
	local reset_epoch="$1"
	local wait_secs

	if [ -n "${reset_epoch}" ] && [ "${reset_epoch}" -gt 0 ] 2>/dev/null; then
		wait_secs=$(( reset_epoch - $(date +%s) + 1 ))
		[ "${wait_secs}" -lt 1 ] && wait_secs=1
		[ "${wait_secs}" -gt 600 ] && wait_secs=600
	else
		wait_secs=30
	fi

	echo "::warning::  Rate limit resets in ${wait_secs}s (computed reset epoch: ${reset_epoch:-unknown})" >&2
	sleep "${wait_secs}"
}

# ---------------------------------------------------------------
# _parse_reset_header — derive the rate-limit reset epoch from a
# header dump file (produced by curl -D).
#
# Precedence:
#   1. Retry-After (integer seconds) → now + seconds. GitHub sends
#      this on secondary rate limits (abuse detection); it is the
#      authoritative wait and is usually well under a minute.
#   2. X-RateLimit-Reset (epoch) → primary window reset.
#
# Without step 1 a secondary-limit 403 was timed against the
# primary window, which can be up to an hour away, so the caller
# slept the full 600 s cap in _sleep_until_reset instead of the
# few seconds GitHub asked for. A non-numeric Retry-After (the
# HTTP-date form) is ignored and falls through to step 2.
#
# Prints the epoch timestamp to stdout; empty string on failure.
# Always returns 0: a missing header or header file is the empty
# string, never a non-zero status, so the function is safe under
# `set -euo pipefail` even outside an `if` / `||` context.
# ---------------------------------------------------------------
_parse_reset_header()
{
	local header_file="$1"
	local _retry_after_secs
	_retry_after_secs=$(grep -i '^retry-after:' "${header_file}" 2>/dev/null \
		| head -1 | sed 's/^[^:]*:[[:space:]]*//; s/[[:space:]]*$//' | tr -d '\r' || true)
	case "${_retry_after_secs}" in
		''|*[!0-9]*) : ;;
		*)
			echo "::warning::  Retry-After: ${_retry_after_secs}s present (secondary rate limit); honouring it over X-RateLimit-Reset" >&2
			# 10# forces decimal: a zero-padded value such as "08" would
			# otherwise be parsed as octal and abort the arithmetic.
			echo $(( $(date +%s) + 10#${_retry_after_secs} ))
			return 0
			;;
	esac
	grep -i '^x-ratelimit-reset:' "${header_file}" 2>/dev/null \
		| head -1 | sed 's/^[^:]*:[[:space:]]*//; s/[[:space:]]*$//' | tr -d '\r' || true
	return 0
}

# ---------------------------------------------------------------
# _gh_rate_limit_wait — query GitHub's /rate_limit endpoint via
# the gh CLI and sleep until the reset window passes.
#
# Falls back to 30 s if the header cannot be parsed.
# ---------------------------------------------------------------
_gh_rate_limit_wait()
{
	local _reset_ts
	_reset_ts=$(gh api -i /rate_limit 2>/dev/null \
		| grep -i '^x-ratelimit-reset:' | head -1 \
		| awk '{print $2}' | tr -d '\r') || true
	_sleep_until_reset "${_reset_ts}"
}

# ---------------------------------------------------------------
# Circuit breaker: rate-limit flag file.
#
# When any gh_retry / curl_gh_api helper detects a GitHub API
# rate limit, it touches this file.  Callers within the same job
# can check gh_rate_limit_breaker_tripped to short-circuit further
# API calls and let the next scheduled workflow run handle them
# instead.  (orchestrate_poll.yml previously consumed this flag to
# suppress an end-of-run self-retrigger dispatch; that path has
# been removed — the flag is still written for other scripts that
# want per-job back-pressure.)
#
# The file path defaults to /tmp/.gh_rate_limit_circuit_breaker
# and can be overridden via GH_RATE_LIMIT_BREAKER_FILE env var.
# ---------------------------------------------------------------
_GH_RATE_LIMIT_BREAKER_FILE="${GH_RATE_LIMIT_BREAKER_FILE:-/tmp/.gh_rate_limit_circuit_breaker}"

_gh_rate_limit_trip_breaker()
{
	touch "${_GH_RATE_LIMIT_BREAKER_FILE}" 2>/dev/null || true
}

# gh_rate_limit_breaker_tripped — returns 0 (true) if a rate
# limit was encountered at any point during this run.
gh_rate_limit_breaker_tripped()
{
	[ -f "${_GH_RATE_LIMIT_BREAKER_FILE}" ]
}

# ---------------------------------------------------------------
# _gh_ratelimit_tg_alert — Telegram admin alert when a GitHub
# API rate limit is hit in any workflow or script.
#
# Throttled to at most one message per
# TG_GH_RATELIMIT_ALERT_COOLDOWN_SECS (default 3600 s = 1 h).
#
# State persistence: a Telegram pinned message in the admin chat
# carries an embedded marker `<!-- gh_rl_ts:EPOCH -->`.  The
# function reads the pinned message via `getChat`, suppresses the
# alert if the marker is within the cooldown window, otherwise
# sends a new message and re-pins it (unpinning the stale pin
# best-effort).  This deliberately avoids any GitHub API call so
# the throttle still works while the GitHub API itself is the
# resource being limited.
#
# Fail-closed semantics: on any read/pin error the function
# returns without sending — if pinning fails AFTER the message is
# sent, the sent message is deleted so the invariant
# "≤ 1 alert per cooldown window" is preserved even under
# transient Telegram failures.
#
# Alert level: WARNING. The function honours `ALERT_MSG_LEVEL`
# (the same global threshold `tg_helpers.sh::tg_send_msg` uses);
# when ALERT_MSG_LEVEL is ERROR or CRITICAL the alert is skipped.
#
# Env (all optional, function no-ops when creds are missing):
#   TG_BOT_SECRET, TG_ADMIN_CHAT_ID (fallback TG_CHAT_ID)
#   TG_GH_RATELIMIT_ALERT_COOLDOWN_SECS (default 3600)
#   ALERT_MSG_LEVEL (default DEBUG; suppresses when > WARNING)
#   GITHUB_WORKFLOW, GITHUB_SERVER_URL, GITHUB_REPOSITORY,
#   GITHUB_RUN_ID — used to build a descriptive message.
#
# Emits: best-effort ::warning:: log lines on persistence failure.
# Returns: always 0 — never block a rate-limit retry path.
# ---------------------------------------------------------------
_gh_ratelimit_tg_alert()
{
	# Resolve chat id; no-op without creds.
	local _chat_id="${TG_ADMIN_CHAT_ID:-${TG_CHAT_ID:-}}"
	if [ -z "${TG_BOT_SECRET:-}" ] || [ -z "${_chat_id}" ]; then
		return 0
	fi

	# jq is required for the Telegram state dance; skip silently
	# if not installed (all repo runners already have it).
	if ! command -v jq >/dev/null 2>&1; then
		return 0
	fi

	# Honour the global ALERT_MSG_LEVEL threshold the same way
	# tg_helpers.sh::tg_send_msg does. This alert is WARNING level,
	# so when the operator has configured ALERT_MSG_LEVEL=ERROR,
	# CRITICAL, or SILENT the rate-limit alert is suppressed (no
	# send, no pin update, cooldown window is not advanced).
	# SILENT (added with the test-and-mark-stable smoke-gate change)
	# must be in the suppress list explicitly; the permissive default
	# below would otherwise let SILENT through and the gate would
	# still see rate-limit pings.
	case "$(printf '%s' "${ALERT_MSG_LEVEL:-DEBUG}" | tr '[:lower:]' '[:upper:]')" in
		DEBUG|WARNING) : ;;
		ERROR|CRITICAL|SILENT) return 0 ;;
		*) : ;;  # unknown value → match tg_helpers.sh permissive default
	esac

	# Cooldown window (seconds). Reject non-numeric values, fall
	# back to 3600.
	local _cooldown="${TG_GH_RATELIMIT_ALERT_COOLDOWN_SECS:-3600}"
	case "${_cooldown}" in
		''|*[!0-9]*) _cooldown=3600 ;;
	esac

	local _tg_base="https://api.telegram.org/bot${TG_BOT_SECRET}"

	# --- Read current pinned message state (fail-closed on error) ---
	local _get_chat_resp
	_get_chat_resp=$(curl -sS --max-time 10 -X POST \
		"${_tg_base}/getChat" \
		-d "chat_id=${_chat_id}" 2>/dev/null) || return 0
	[ -n "${_get_chat_resp}" ] || return 0
	if ! printf '%s' "${_get_chat_resp}" | jq -e '.ok == true' >/dev/null 2>&1; then
		return 0
	fi

	local _pinned_text _prev_pin_id _last_epoch _now
	_pinned_text=$(printf '%s' "${_get_chat_resp}" \
		| jq -r '.result.pinned_message.text // ""' 2>/dev/null) || return 0
	_prev_pin_id=$(printf '%s' "${_get_chat_resp}" \
		| jq -r '.result.pinned_message.message_id // ""' 2>/dev/null) || return 0

	_last_epoch=$(printf '%s' "${_pinned_text}" \
		| sed -n 's/.*<!-- gh_rl_ts:\([0-9]\{1,\}\) -->.*/\1/p' | tail -1)

	_now=$(date +%s)
	if [ -n "${_last_epoch}" ] && [ "${_last_epoch}" -gt 0 ] 2>/dev/null; then
		local _age=$(( _now - _last_epoch ))
		if [ "${_age}" -ge 0 ] && [ "${_age}" -lt "${_cooldown}" ]; then
			# Within cooldown — suppress.
			return 0
		fi
	fi

	# --- Best-effort de-race across concurrent workflow runs ---
	#
	# The getChat→send→pin sequence is not atomic: two runners can
	# both read an old marker, both decide the cooldown has expired,
	# and both go on to send+pin alerts, producing duplicate pings.
	# Add a short randomized jitter (1–3 s) and then re-read the
	# pinned message before sending. If a concurrent runner already
	# published a fresh alert in the gap, `_recheck_epoch` will be
	# within the current cooldown window and we suppress. This does
	# not eliminate the race (Telegram has no client-side locks),
	# but shrinks the window by orders of magnitude. The jitter cost
	# is negligible inside a rate-limit branch that is already about
	# to sleep up to 600 s waiting for the GH reset window.
	local _jitter_secs
	_jitter_secs=$(( (RANDOM % 3) + 1 ))
	sleep "${_jitter_secs}"

	local _recheck_resp _recheck_text _recheck_epoch
	_recheck_resp=$(curl -sS --max-time 10 -X POST \
		"${_tg_base}/getChat" \
		-d "chat_id=${_chat_id}" 2>/dev/null) || return 0
	[ -n "${_recheck_resp}" ] || return 0
	if ! printf '%s' "${_recheck_resp}" | jq -e '.ok == true' >/dev/null 2>&1; then
		return 0
	fi
	_recheck_text=$(printf '%s' "${_recheck_resp}" \
		| jq -r '.result.pinned_message.text // ""' 2>/dev/null) || return 0
	_recheck_epoch=$(printf '%s' "${_recheck_text}" \
		| sed -n 's/.*<!-- gh_rl_ts:\([0-9]\{1,\}\) -->.*/\1/p' | tail -1)
	_now=$(date +%s)
	if [ -n "${_recheck_epoch}" ] && [ "${_recheck_epoch}" -gt 0 ] 2>/dev/null; then
		local _recheck_age=$(( _now - _recheck_epoch ))
		if [ "${_recheck_age}" -ge 0 ] && [ "${_recheck_age}" -lt "${_cooldown}" ]; then
			# Another concurrent run already published a fresh
			# pinned alert during the jitter window — suppress.
			return 0
		fi
	fi
	# Refresh _prev_pin_id from the rechecked state so the later
	# unpin guard still targets the right message id if it changed.
	_prev_pin_id=$(printf '%s' "${_recheck_resp}" \
		| jq -r '.result.pinned_message.message_id // ""' 2>/dev/null) || _prev_pin_id=""
	_last_epoch="${_recheck_epoch}"

	# --- Build message body ---
	local _workflow="${GITHUB_WORKFLOW:-unknown}"
	local _repo="${GITHUB_REPOSITORY:-unknown}"
	local _run_url=""
	if [ -n "${GITHUB_SERVER_URL:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ] && [ -n "${GITHUB_RUN_ID:-}" ]; then
		_run_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
	fi
	local _body
	_body="⚠️ WARNING: GitHub API rate limit hit — workflow=${_workflow} repo=${_repo}"
	if [ -n "${_run_url}" ]; then
		_body="${_body} run=${_run_url}"
	fi
	# Embed dedup marker on a new line (sticky inside pinned text).
	_body="${_body}
<!-- gh_rl_ts:${_now} -->"

	# --- Send the alert message ---
	local _send_resp _new_msg_id
	_send_resp=$(curl -sS --max-time 10 -X POST \
		"${_tg_base}/sendMessage" \
		-d "chat_id=${_chat_id}" \
		-d "disable_web_page_preview=true" \
		--data-urlencode "text=${_body}" 2>/dev/null) || return 0
	[ -n "${_send_resp}" ] || return 0
	if ! printf '%s' "${_send_resp}" | jq -e '.ok == true' >/dev/null 2>&1; then
		return 0
	fi
	_new_msg_id=$(printf '%s' "${_send_resp}" \
		| jq -r '.result.message_id // ""' 2>/dev/null)
	[ -n "${_new_msg_id}" ] || return 0

	# --- Pin new message (persists state). Fail-closed: on pin
	# failure, delete the message we just sent so the cooldown
	# invariant is preserved. ---
	local _pin_resp _pin_ok=0
	_pin_resp=$(curl -sS --max-time 10 -X POST \
		"${_tg_base}/pinChatMessage" \
		-d "chat_id=${_chat_id}" \
		-d "message_id=${_new_msg_id}" \
		-d "disable_notification=true" 2>/dev/null) || _pin_resp=""

	if [ -n "${_pin_resp}" ] && printf '%s' "${_pin_resp}" | jq -e '.ok == true' >/dev/null 2>&1; then
		_pin_ok=1
	fi

	if [ "${_pin_ok}" -ne 1 ]; then
		echo "::warning::_gh_ratelimit_tg_alert: failed to pin new alert message (fail-closed); rolling back sent message" >&2
		curl -sS --max-time 10 -X POST \
			"${_tg_base}/deleteMessage" \
			-d "chat_id=${_chat_id}" \
			-d "message_id=${_new_msg_id}" >/dev/null 2>&1 || true
		return 0
	fi

	# --- Best-effort unpin the previous stale pin so the admin
	# chat keeps a single sticky rate-limit alert.
	#
	# IMPORTANT: only unpin when the previous pinned message was
	# itself one of OUR rate-limit alerts. `_last_epoch` is
	# extracted from the `<!-- gh_rl_ts:EPOCH -->` marker in the
	# previous pin's text, so a non-empty value proves the previous
	# pin carried the marker. If an operator has pinned an
	# unrelated important message (ops notice, runbook, etc.),
	# leave it alone — the new rate-limit pin will still be the
	# most-recent pin returned by `getChat` for cooldown reads,
	# which is all we need. ---
	if [ -n "${_last_epoch}" ] && [ "${_last_epoch}" -gt 0 ] 2>/dev/null \
		&& [ -n "${_prev_pin_id}" ] \
		&& [ "${_prev_pin_id}" != "${_new_msg_id}" ]; then
		curl -sS --max-time 10 -X POST \
			"${_tg_base}/unpinChatMessage" \
			-d "chat_id=${_chat_id}" \
			-d "message_id=${_prev_pin_id}" >/dev/null 2>&1 || true
	fi

	return 0
}

# ---------------------------------------------------------------
# gh_retry — Execute a gh CLI command with automatic retry.
#
# Rate-limit errors  → wait until X-RateLimit-Reset, retry (up to max_attempts).
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
	if ! stderr_file=$(mktemp "${TMPDIR:-/tmp}/gh_retry_stderr.XXXXXX" 2>/dev/null); then
		echo "::error::gh_retry: failed to create stderr temp file (mktemp failed); aborting without running: $*" >&2
		return 1
	fi

	while [ "${attempt}" -le "${max_attempts}" ]; do
		if "$@" 2>"${stderr_file}"; then
			rm -f "${stderr_file}"
			return 0
		fi

		local stderr_content
		stderr_content=$(cat "${stderr_file}" 2>/dev/null || true)

		if _is_gh_permanent_failure "${stderr_content}"; then
			echo "::warning::gh command failed with non-retryable error (attempt ${attempt}/${max_attempts}); not retrying: $*" >&2
			if [ -n "${stderr_content}" ]; then
				echo "::warning::  stderr: $(_gh_actions_escape "${stderr_content}")" >&2
			fi
			rm -f "${stderr_file}"
			return 1
		fi

		if _is_gh_rate_limit "${stderr_content}"; then
			echo "::warning::GitHub API rate limit hit (attempt ${attempt}/${max_attempts}), waiting for reset…" >&2
			_gh_ratelimit_tg_alert
			_gh_rate_limit_trip_breaker
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				_gh_rate_limit_wait
			fi
		else
			local wait_secs=$(( 2 ** (attempt - 1) ))
			echo "::warning::gh command failed (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
			if [ -n "${stderr_content}" ]; then
				echo "::warning::  stderr: $(_gh_actions_escape "${stderr_content}")" >&2
			fi
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				sleep "${wait_secs}"
			fi
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

# Run one networked git command with GitHub authentication that exists only
# for that child process. The token is never written to repository config.
git_with_github_auth()
{
	local auth_token="${GH_PAT:-${GH_TOKEN:-}}"
	local auth_header=""
	if [ -z "${auth_token}" ]; then
		git "$@"
		return
	fi
	auth_header="$(printf 'x-access-token:%s' "${auth_token}" | base64 | tr -d '\n')"
	git -c "http.extraHeader=Authorization: Basic ${auth_header}" "$@"
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
	if ! stderr_file=$(mktemp "${TMPDIR:-/tmp}/gh_retry_stderr.XXXXXX" 2>/dev/null); then
		echo "::error::gh_retry_to_file: failed to create stderr temp file (mktemp failed); aborting without running: $*" >&2
		return 1
	fi

	while [ "${attempt}" -le "${max_attempts}" ]; do
		if "$@" > "${outfile}" 2>"${stderr_file}"; then
			rm -f "${stderr_file}"
			return 0
		fi

		local stderr_content
		stderr_content=$(cat "${stderr_file}" 2>/dev/null || true)

		if _is_gh_permanent_failure "${stderr_content}"; then
			echo "::warning::gh command failed with non-retryable error (attempt ${attempt}/${max_attempts}); not retrying: $*" >&2
			if [ -n "${stderr_content}" ]; then
				echo "::warning::  stderr: $(_gh_actions_escape "${stderr_content}")" >&2
			fi
			rm -f "${stderr_file}"
			return 1
		fi

		if _is_gh_rate_limit "${stderr_content}"; then
			echo "::warning::GitHub API rate limit hit (attempt ${attempt}/${max_attempts}), waiting for reset…" >&2
			_gh_ratelimit_tg_alert
			_gh_rate_limit_trip_breaker
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				_gh_rate_limit_wait
			fi
		else
			local wait_secs=$(( 2 ** (attempt - 1) ))
			echo "::warning::gh command failed (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
			if [ -n "${stderr_content}" ]; then
				echo "::warning::  stderr: $(_gh_actions_escape "${stderr_content}")" >&2
			fi
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				sleep "${wait_secs}"
			fi
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
# _safe_gh_jq — run gh api and suppress stdout on failure.
#
# When gh api receives a non-2xx response (e.g. 403 rate limit),
# it dumps the raw error JSON to stdout WITHOUT applying the --jq
# filter.  The common shell pattern
#   val="$(gh api ... --jq '.field' 2>/dev/null || echo "fallback")"
# is broken because the error JSON on stdout combines with the
# fallback string, producing garbage that fails equality checks.
#
# This function captures stdout to a temp file, checks the exit
# code, and only emits output on success.  On failure it outputs
# nothing and returns 1, so `|| echo "fallback"` works correctly.
#
# Usage:
#   val="$(_safe_gh_jq "repos/o/r/pulls/1" --jq '.state' || echo "open")"
# ---------------------------------------------------------------
_safe_gh_jq()
{
	local _tmpf
	if ! _tmpf=$(mktemp "${TMPDIR:-/tmp}/_safe_gh_jq.XXXXXX" 2>/dev/null); then
		echo "::error::_safe_gh_jq: failed to create temp file (mktemp failed); aborting without running: $*" >&2
		return 1
	fi
	if gh api "$@" > "${_tmpf}"; then
		cat "${_tmpf}"
		rm -f "${_tmpf}"
		return 0
	fi
	rm -f "${_tmpf}"
	return 1
}

# ---------------------------------------------------------------
# gh_api_json_to_file — Fetch a GitHub API JSON response to a file,
# with JSON validation and rate-limit-aware retry.
#
# Like gh_retry_to_file but additionally validates the response body
# with `jq empty`.  If the API call succeeds but the response is not
# valid JSON (e.g. truncated due to a network interruption), the
# attempt is retried with exponential backoff.
#
# Usage:
#   tmp="$(mktemp)"
#   gh_api_json_to_file "$tmp" gh api repos/owner/repo/issues/1
#   jq -r '.title' "$tmp"
# ---------------------------------------------------------------
gh_api_json_to_file()
{
	local outfile="$1"; shift
	local max_attempts="${GH_RETRY_MAX_ATTEMPTS:-5}"
	local attempt=1
	local stderr_file
	if ! stderr_file=$(mktemp "${TMPDIR:-/tmp}/gh_api_json_stderr.XXXXXX" 2>/dev/null); then
		echo "::error::gh_api_json_to_file: failed to create stderr temp file (mktemp failed); aborting without running: $*" >&2
		return 1
	fi

	while [ "${attempt}" -le "${max_attempts}" ]; do
		: > "${outfile}"
		if "$@" > "${outfile}" 2>"${stderr_file}"; then
			if [ -s "${outfile}" ] && jq empty "${outfile}" >/dev/null 2>&1; then
				rm -f "${stderr_file}"
				return 0
			fi
			local wait_secs=$(( 2 ** (attempt - 1) ))
			echo "::warning::gh api returned invalid JSON (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
			echo "::group::Raw response (first 50 lines)" >&2
			head -50 "${outfile}" >&2
			echo "::endgroup::" >&2
			sleep "${wait_secs}"
		else
			local stderr_content
			stderr_content=$(cat "${stderr_file}" 2>/dev/null || true)

			if _is_gh_rate_limit "${stderr_content}"; then
				echo "::warning::GitHub API rate limit hit (attempt ${attempt}/${max_attempts}), waiting for reset…" >&2
				_gh_ratelimit_tg_alert
				_gh_rate_limit_trip_breaker
				_gh_rate_limit_wait
			else
				local wait_secs=$(( 2 ** (attempt - 1) ))
				echo "::warning::gh command failed (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
				if [ -n "${stderr_content}" ]; then
					echo "::warning::  stderr: $(_gh_actions_escape "${stderr_content}")" >&2
				fi
				sleep "${wait_secs}"
			fi
		fi

		attempt=$(( attempt + 1 ))
	done

	echo "::error::gh api failed to return valid JSON after ${max_attempts} attempts: $*" >&2
	if [ -s "${stderr_file}" ]; then
		cat "${stderr_file}" >&2
	fi
	rm -f "${stderr_file}"
	: > "${outfile}"
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
	local body_file header_file
	if ! body_file=$(mktemp "${TMPDIR:-/tmp}/curl_gh_body.XXXXXX" 2>/dev/null); then
		echo "::error::curl_gh_api: failed to create body temp file (mktemp failed); aborting without calling curl" >&2
		return 1
	fi
	if ! header_file=$(mktemp "${TMPDIR:-/tmp}/curl_gh_hdr.XXXXXX" 2>/dev/null); then
		echo "::error::curl_gh_api: failed to create header temp file (mktemp failed); aborting without calling curl" >&2
		rm -f "${body_file}"
		return 1
	fi

	while [ "${attempt}" -le "${max_attempts}" ]; do
		: > "${body_file}"
		: > "${header_file}"
		local http_code
		http_code=$(curl -o "${body_file}" -D "${header_file}" -w '%{http_code}' "$@" 2>/dev/null) || http_code="000"

		if [ "${http_code}" -ge 200 ] 2>/dev/null && [ "${http_code}" -lt 300 ] 2>/dev/null; then
			cat "${body_file}"
			rm -f "${body_file}" "${header_file}"
			return 0
		fi

		local body_content
		body_content=$(cat "${body_file}" 2>/dev/null || true)

		if [ "${http_code}" = "429" ] || { [ "${http_code}" = "403" ] && _is_gh_rate_limit "${body_content}"; }; then
			echo "::warning::GitHub API rate limit (HTTP ${http_code}, attempt ${attempt}/${max_attempts}), waiting for reset…" >&2
			_gh_ratelimit_tg_alert
			_gh_rate_limit_trip_breaker
			local _reset_ts
			_reset_ts=$(_parse_reset_header "${header_file}")
			_sleep_until_reset "${_reset_ts}"
		else
			local wait_secs=$(( 2 ** (attempt - 1) ))
			echo "::warning::GitHub API curl failed HTTP ${http_code} (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
			sleep "${wait_secs}"
		fi

		attempt=$(( attempt + 1 ))
	done

	rm -f "${body_file}" "${header_file}"
	echo "::error::GitHub API curl failed after ${max_attempts} attempts" >&2
	return 1
}

# ---------------------------------------------------------------
# _gh_pr_with_all_comments_rest — legacy REST parity fetch path.
#
# Emits JSON object:
# {
#   "meta": {"title", "body", "head_ref", "base_ref", "head_sha"},
#   "comments": [{"id", "author", "author_id", "author_type", "author_association", "body", "created_at"}],
#   "review_comments": [{"author", "path", "line", "body"}]
# }
#
# Manual fixture capture (H4 parity):
#   source scripts/gh_helpers.sh
#   _gh_pr_with_all_comments_rest OWNER REPO PR_NUMBER \
#     > scripts/fixtures/issue-timeline/rest_pr_with_comments_fixture.json
#   gh_pr_with_all_comments OWNER REPO PR_NUMBER \
#     > scripts/fixtures/issue-timeline/graphql_pr_with_comments_fixture.json
# ---------------------------------------------------------------
_gh_pr_with_all_comments_rest()
{
	local owner="$1"
	local repo="$2"
	local pr_number="$3"
	local preloaded_meta_json="${4:-}"
	local repo_path="${owner}/${repo}"

	local meta_json comments_json review_comments_json
	if [ -n "${preloaded_meta_json}" ] && echo "${preloaded_meta_json}" | jq -e 'type == "object"' >/dev/null 2>&1; then
		meta_json="$(echo "${preloaded_meta_json}" | jq -c '{
			title: (.title // ""),
			body: (.body // ""),
			head_ref: (.head_ref // .head.ref // .headRefName // ""),
			base_ref: (.base_ref // .base.ref // .baseRefName // ""),
			head_sha: (.head_sha // .head.sha // .headSha // "")
		}' 2>/dev/null || echo '{}')"
	else
		meta_json="$(gh_retry gh api "repos/${repo_path}/pulls/${pr_number}" 2>/dev/null \
			| jq -c '{title: .title, body: .body, head_ref: .head.ref, base_ref: .base.ref, head_sha: .head.sha}' 2>/dev/null \
			|| echo '{}')"
	fi
	comments_json="$(gh_retry gh api --paginate "repos/${repo_path}/issues/${pr_number}/comments" 2>/dev/null \
		| jq -c -s 'add // [] | [.[] | {id: .id, author: .user.login, author_id: .user.id, author_type: .user.type, author_association: .author_association, body: .body, created_at: .created_at}] | sort_by((.created_at // ""), (.id // 0))' 2>/dev/null \
		|| echo '[]')"
	review_comments_json="$(gh_retry gh api --paginate "repos/${repo_path}/pulls/${pr_number}/comments" 2>/dev/null \
		| jq -c -s 'add // [] | [.[] | {author: .user.login, path: .path, line: .line, body: .body}] | sort_by((.path // ""), (.line // 0), (.author // ""), (.body // ""))' 2>/dev/null \
		|| echo '[]')"

	jq -cn \
		--argjson meta "${meta_json}" \
		--argjson comments "${comments_json}" \
		--argjson review_comments "${review_comments_json}" \
		'{meta: $meta, comments: $comments, review_comments: $review_comments}'
}

# ---------------------------------------------------------------
# gh_pr_with_all_comments — GraphQL-first consolidated PR context.
#
# Inputs:
#   gh_pr_with_all_comments <owner> <repo> <pr_number> [preloaded_meta_json]
#
# Optional:
#   preloaded_meta_json — JSON object with pre-fetched PR metadata.
#   Accepts either legacy flat keys (`headRefName`/`baseRefName`) or
#   normalized keys (`head_ref`/`base_ref`/`head_sha`).
#
# Emits JSON object:
# {
#   "meta": {"title", "body", "head_ref", "base_ref", "head_sha"},
#   "comments": [{"id", "author", "author_id", "author_type", "author_association", "body", "created_at"}],
#   "review_comments": [{"author", "path", "line", "body"}]
# }
#
# Mandatory fail-open fallback to REST parity path when:
# - GraphQL request fails after retry budget
# - GraphQL payload has errors or transform fails
# - Any pagination boundary is hit (`hasNextPage=true` for PR comments,
#   reviews, or nested review comments)
#
# All fallback paths emit:
#   ::warning::rate_limit_audit_fallback helper=gh_pr_with_all_comments ...
# ---------------------------------------------------------------
gh_pr_with_all_comments()
{
	local owner="$1"
	local repo="$2"
	local pr_number="$3"
	local preloaded_meta_json="${4:-}"

	local _fallback_reason=""
	local _gql_file
	local goto_fallback=0
	if ! _gql_file=$(mktemp "${TMPDIR:-/tmp}/gh_pr_with_all_comments.XXXXXX" 2>/dev/null); then
		_fallback_reason="mktemp_failed"
		goto_fallback=1
	fi

	if [ "${goto_fallback:-0}" -eq 0 ]; then
		local gql_query
		gql_query='query($owner: String!, $name: String!, $number: Int!) {
		repository(owner: $owner, name: $name) {
			pullRequest(number: $number) {
				title
				body
				headRefName
				baseRefName
				headRefOid
				comments(first: 100) {
					nodes {
						databaseId
						author {
							login
							__typename
							... on User { databaseId }
							... on Bot { databaseId }
						}
						authorAssociation
						body
						createdAt
					}
					pageInfo { hasNextPage }
				}
				reviews(first: 50) {
					nodes {
						comments(first: 100) {
							nodes {
								author { login }
								path
								line
								body
							}
							pageInfo { hasNextPage }
						}
					}
					pageInfo { hasNextPage }
				}
			}
		}
	}'

		if ! gh_api_json_to_file "${_gql_file}" \
			gh api graphql \
			-f query="${gql_query}" \
			-F owner="${owner}" \
			-F name="${repo}" \
			-F number="${pr_number}"; then
			_fallback_reason="graphql_request_failed"
			goto_fallback=1
		fi
	fi

	if [ "${goto_fallback:-0}" -eq 0 ]; then
		if ! jq -e '.errors | not or length == 0' "${_gql_file}" >/dev/null 2>&1; then
			_fallback_reason="graphql_errors"
			goto_fallback=1
		fi
	fi

	if [ "${goto_fallback:-0}" -eq 0 ]; then
		if ! jq -e '.data.repository.pullRequest != null' "${_gql_file}" >/dev/null 2>&1; then
			_fallback_reason="graphql_missing_pr"
			goto_fallback=1
		fi
	fi

	if [ "${goto_fallback:-0}" -eq 0 ]; then
		local has_next
		has_next="$(jq -r '
			.data.repository.pullRequest as $pr
			| (
				($pr.comments.pageInfo.hasNextPage // false)
				or ($pr.reviews.pageInfo.hasNextPage // false)
				or ([$pr.reviews.nodes[]?.comments.pageInfo.hasNextPage // false] | any)
			)
		' "${_gql_file}" 2>/dev/null || echo 'true')"
		if [ "${has_next}" = "true" ]; then
			_fallback_reason="graphql_has_next_page"
			goto_fallback=1
		fi
	fi

	if [ "${goto_fallback:-0}" -eq 0 ]; then
		if jq -c '
			.data.repository.pullRequest as $pr
			| {
				meta: {
					title: ($pr.title // ""),
					body: ($pr.body // ""),
					head_ref: ($pr.headRefName // ""),
					base_ref: ($pr.baseRefName // ""),
					head_sha: ($pr.headRefOid // "")
				},
				comments: (
					[
						($pr.comments.nodes // [])[]
						| {
							id: (.databaseId // null),
							author: (.author.login // null),
							author_id: (.author.databaseId // null),
							author_type: (if .author.__typename == "User" then "User" else (.author.__typename // null) end),
							author_association: (.authorAssociation // null),
							body: (.body // ""),
							created_at: (.createdAt // null)
						}
					]
					| sort_by((.created_at // ""), (.id // 0))
				),
				review_comments: (
					[
						($pr.reviews.nodes // [])[]
						| (.comments.nodes // [])[]
						| {
							author: (.author.login // null),
							path: (.path // null),
							line: (.line // null),
							body: (.body // "")
						}
					]
					| sort_by((.path // ""), (.line // 0), (.author // ""), (.body // ""))
				)
			}
		' "${_gql_file}"; then
			rm -f "${_gql_file}"
			return 0
		fi
		_fallback_reason="graphql_transform_failed"
		goto_fallback=1
	fi

	rm -f "${_gql_file:-}"
	echo "::warning::rate_limit_audit_fallback helper=gh_pr_with_all_comments reason=${_fallback_reason:-unknown} owner=${owner} repo=${repo} pr=${pr_number}" >&2
	_gh_pr_with_all_comments_rest "${owner}" "${repo}" "${pr_number}" "${preloaded_meta_json}"
}

# Review-blocked terminal decisions are model recommendations until a trusted
# human approves the exact request and decision digest on the PR. Approval and
# refusal checks consume prefetched PR comments; replacement-issue dedup uses
# one bounded search/issues read.
review_blocked_decision_digest()
{
	printf '%s' "${1:?decision JSON required}" | jq -cS . | sha256sum | awk '{print $1}'
}

review_blocked_find_pending_request()
{
	local comments_json="${1:-[]}"
	local pr_number="${2:?PR number required}"
	local head_sha="${3:?head SHA required}"
	local producer_id="${4:?authenticated producer ID required}"
	[[ "${producer_id}" =~ ^[1-9][0-9]*$ ]] || return 1
	printf '%s' "${comments_json}" | PYTHONDONTWRITEBYTECODE=1 python3 -c '
import json, re, sys
comments = json.load(sys.stdin)
pr = int(sys.argv[1]); head = sys.argv[2]; producer_id = int(sys.argv[3])
request_re = re.compile(r"<!-- REVIEW_BLOCKED_APPROVAL_V1\s*\n(\{.*?\})\s*\nREVIEW_BLOCKED_APPROVAL_V1 -->", re.S)
consumed_re = re.compile(r"<!-- REVIEW_BLOCKED_APPROVAL_CONSUMED_V1\s*\n(\{.*?\})\s*\nREVIEW_BLOCKED_APPROVAL_CONSUMED_V1 -->", re.S)
requests = []
consumed = set()
for comment in comments if isinstance(comments, list) else []:
    if not isinstance(comment, dict): continue
    trusted_producer = comment.get("author_id") == producer_id
    if not trusted_producer: continue
    body = comment.get("body", "") if isinstance(comment, dict) else ""
    for match in consumed_re.finditer(body):
        try: consumed.add(json.loads(match.group(1)).get("request_id"))
        except (json.JSONDecodeError, TypeError): pass
    for match in request_re.finditer(body):
        try: request = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError): continue
        if request.get("schema_version") != "review_blocked_approval.v1": continue
        if request.get("pr_number") != pr or request.get("head_sha") != head: continue
        if request.get("action") not in {"merge", "merge_with_followup", "close_and_reissue"}: continue
        if not isinstance(request.get("decision"), dict): continue
        if request["decision"].get("action") != request.get("action"): continue
        if not re.fullmatch(r"review_blocked_approval_\d{14}_[0-9a-f]{10}", str(request.get("request_id", ""))): continue
        if not re.fullmatch(r"[0-9a-f]{64}", str(request.get("decision_digest", ""))): continue
        requests.append(request)
for request in reversed(requests):
    if request.get("request_id") not in consumed:
        print(json.dumps(request, separators=(",", ":"), sort_keys=True))
        break
' "${pr_number}" "${head_sha}" "${producer_id}"
}

review_blocked_build_approval_request()
{
	local pr_number="${1:?PR number required}"
	local issue_number="${2:-0}"
	local head_sha="${3:?head SHA required}"
	local decision_json="${4:?decision JSON required}"
	local action request_id decision_digest created_at helper_dir
	action="$(printf '%s' "${decision_json}" | jq -r '.action // empty')"
	decision_digest="$(review_blocked_decision_digest "${decision_json}")"
	helper_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
	request_id="$(PYTHONPATH="${helper_dir}:${PYTHONPATH:-}" PYTHONDONTWRITEBYTECODE=1 python3 -c 'from ai_memory_lib import make_record_id; print(make_record_id("review_blocked_approval"))')" || return 1
	created_at="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
	jq -cn --arg request_id "${request_id}" --argjson pr_number "${pr_number}" \
		--argjson issue_number "${issue_number:-0}" --arg action "${action}" \
		--arg head_sha "${head_sha}" --arg decision_digest "${decision_digest}" \
		--arg created_at "${created_at}" --argjson decision "${decision_json}" \
		'{schema_version:"review_blocked_approval.v1",request_id:$request_id,pr_number:$pr_number,linked_issue_number:$issue_number,action:$action,head_sha:$head_sha,decision_digest:$decision_digest,created_at:$created_at,decision:$decision}'
}

review_blocked_approval_status()
{
	local comments_json="${1:-[]}"
	local request_json="${2:?request JSON required}"
	local repository="${3:-${GITHUB_REPOSITORY:-}}"
	local pending_json approval_candidate approver repository_role
	pending_json="$(printf '%s' "${request_json}" | jq -c '{status:"pending",request_id:(.request_id // null),decision_digest:(.decision_digest // null)}' 2>/dev/null || printf '%s' '{"status":"pending","request_id":null,"decision_digest":null}')"
	if ! [[ "${repository}" =~ ^[^/[:space:]]+/[^/[:space:]]+$ ]]; then
		printf '%s\n' "${pending_json}"
		return 0
	fi
	approval_candidate="$(printf '%s' "${comments_json}" | PYTHONDONTWRITEBYTECODE=1 python3 -c '
import json, re, sys
try:
    comments = json.load(sys.stdin); request = json.loads(sys.argv[1])
    request_id = request["request_id"]; digest = request["decision_digest"]
except (json.JSONDecodeError, KeyError, TypeError):
    raise SystemExit(0)
command = "/review-blocked-approve {} {}".format(request_id, digest)
trusted = {"OWNER", "MEMBER", "COLLABORATOR"}
selected = None
for comment in comments if isinstance(comments, list) else []:
    if not isinstance(comment, dict) or comment.get("body", "").strip() != command: continue
    if comment.get("author_type") != "User" or comment.get("author_association") not in trusted: continue
    if (comment.get("created_at") or "") < request.get("created_at", ""): continue
    author = comment.get("author")
    if not isinstance(author, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", author): continue
    selected = comment
if selected:
    print(json.dumps(selected, separators=(",", ":")))
' "${request_json}" 2>/dev/null || true)"
	if [ -z "${approval_candidate}" ]; then
		printf '%s\n' "${pending_json}"
		return 0
	fi
	approver="$(printf '%s' "${approval_candidate}" | jq -r '.author // empty' 2>/dev/null || true)"
	if [ -z "${approver}" ]; then
		printf '%s\n' "${pending_json}"
		return 0
	fi
	# The prefetched comment payload carries historical association only; no
	# existing PR-context call exposes the commenter's current repository role.
	# GitHub's legacy `permission` field maps maintain to write, so only the
	# exact role_name can distinguish terminal-action authority safely.
	if ! repository_role="$(gh_retry gh api -X GET "repos/${repository}/collaborators/${approver}/permission" --jq '.role_name // empty' 2>/dev/null)"; then
		echo "::warning::Review-blocked approval remains pending because the current repository role for ${approver} could not be resolved." >&2
		printf '%s\n' "${pending_json}"
		return 0
	fi
	case "${repository_role}" in
		maintain|admin)
			printf '%s' "${approval_candidate}" | jq -c --arg role "${repository_role}" --arg request_id "$(printf '%s' "${request_json}" | jq -r '.request_id')" --arg decision_digest "$(printf '%s' "${request_json}" | jq -r '.decision_digest')" '{status:"approved",request_id:$request_id,decision_digest:$decision_digest,approver:(.author // null),approval_comment_id:(.id // null),repository_role:$role}'
			;;
		*)
			printf '%s\n' "${pending_json}"
			;;
	esac
}

review_blocked_post_approval_request()
{
	local repository="${1:?repository required}"
	local pr_number="${2:?PR number required}"
	local request_json="${3:?request JSON required}"
	local request_id decision_digest action head_sha body
	request_id="$(printf '%s' "${request_json}" | jq -r '.request_id')"
	decision_digest="$(printf '%s' "${request_json}" | jq -r '.decision_digest')"
	action="$(printf '%s' "${request_json}" | jq -r '.action')"
	head_sha="$(printf '%s' "${request_json}" | jq -r '.head_sha')"
	body="## Review-Blocked Judge — Human Approval Required

The model recommends **${action}**, but no terminal PR mutation has run. A GitHub User whose current repository role is exactly maintain or admin must approve this exact request at head \`${head_sha}\` by posting:

\`/review-blocked-approve ${request_id} ${decision_digest}\`

<!-- REVIEW_BLOCKED_APPROVAL_V1
${request_json}
REVIEW_BLOCKED_APPROVAL_V1 -->"
	gh_retry gh api "repos/${repository}/issues/${pr_number}/comments" -f body="${body}" >/dev/null
}

review_blocked_post_consumed_marker()
{
	local repository="${1:?repository required}" pr_number="${2:?PR number required}"
	local request_id="${3:?request ID required}" result="${4:?result required}"
	local consumed_at body
	consumed_at="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
	body="<!-- REVIEW_BLOCKED_APPROVAL_CONSUMED_V1
$(jq -cn --arg request_id "${request_id}" --arg result "${result}" --arg consumed_at "${consumed_at}" '{schema_version:"review_blocked_approval_consumed.v1",request_id:$request_id,result:$result,consumed_at:$consumed_at}')
REVIEW_BLOCKED_APPROVAL_CONSUMED_V1 -->"
	gh_retry gh api "repos/${repository}/issues/${pr_number}/comments" -f body="${body}" >/dev/null
}

review_blocked_find_issue_for_request()
{
	local repository="${1:?repository required}" request_id="${2:?request ID required}"
	# Audited existing calls: PR comment hydration cannot find repository issues
	# by a durable body marker. One bounded search call is required for retry
	# deduplication; failures intentionally return empty and preserve legacy flow.
	gh_retry gh api -X GET search/issues \
		-f q="repo:${repository} is:issue is:open in:body review-blocked-approval-request:${request_id}" \
		--jq '.items // [] | map(select((.body // "") | contains("<!-- review-blocked-approval-request:'"${request_id}"' -->"))) | first | .html_url // empty' \
		2>/dev/null || true
}

review_blocked_build_successor_intent()
{
	local request_json="${1:?approval request JSON required}"
	local successor_type="${2:?successor type required}"
	local successor_title="${3:?successor title required}"
	local successor_body="${4:?successor body required}"
	local required_labels_json="${5:?required labels JSON required}"
	local producer_id="${6:?producer ID required}"
	REQUEST_JSON="${request_json}" SUCCESSOR_TYPE="${successor_type}" \
	SUCCESSOR_TITLE="${successor_title}" SUCCESSOR_BODY="${successor_body}" \
	REQUIRED_LABELS_JSON="${required_labels_json}" PRODUCER_ID="${producer_id}" \
	PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import hashlib
import json
import os
import re
import sys

try:
	request = json.loads(os.environ["REQUEST_JSON"])
	labels = json.loads(os.environ["REQUIRED_LABELS_JSON"])
	producer_id = int(os.environ["PRODUCER_ID"])
except (KeyError, ValueError, TypeError, json.JSONDecodeError):
	raise SystemExit(2)
successor_type = os.environ.get("SUCCESSOR_TYPE", "")
title = os.environ.get("SUCCESSOR_TITLE", "")
body = os.environ.get("SUCCESSOR_BODY", "")
action = request.get("action")
expected_type = {"merge_with_followup": "followup", "close_and_reissue": "reissue"}.get(action)
if (
	request.get("schema_version") != "review_blocked_approval.v1"
	or expected_type != successor_type
	or not re.fullmatch(r"review_blocked_approval_\d{14}_[0-9a-f]{10}", str(request.get("request_id", "")))
	or not isinstance(request.get("pr_number"), int) or request["pr_number"] < 1
	or not isinstance(request.get("linked_issue_number"), int) or request["linked_issue_number"] < 1
	or not re.fullmatch(r"[0-9a-f]{40}", str(request.get("head_sha", "")))
	or producer_id < 1
	or not title or len(title) > 240
	or not body or len(body) > 20000
	or not isinstance(labels, list) or not labels
	or any(not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", label) for label in labels)
):
	raise SystemExit(2)
labels = sorted(set(labels))
intent = {
	"schema_version": "review_blocked_successor.v1",
	"request_id": request["request_id"],
	"action": action,
	"source_pr": request["pr_number"],
	"linked_issue": request["linked_issue_number"],
	"approved_head_sha": request["head_sha"],
	"producer_id": producer_id,
	"successor_type": successor_type,
	"title_digest": hashlib.sha256(title.encode()).hexdigest(),
	"body_digest": hashlib.sha256(body.encode()).hexdigest(),
	"required_labels": labels,
}
canonical = json.dumps(intent, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
intent["payload_digest"] = hashlib.sha256(canonical).hexdigest()
print(json.dumps(intent, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
}

review_blocked_successor_marker()
{
	local intent_json="${1:?successor intent JSON required}"
	printf '<!-- REVIEW_BLOCKED_SUCCESSOR_V1\n%s\nREVIEW_BLOCKED_SUCCESSOR_V1 -->' "${intent_json}"
}

review_blocked_ensure_successor_intent()
{
	local repository="${1:?repository required}"
	local pr_number="${2:?PR number required}"
	local intent_json="${3:?successor intent JSON required}"
	local producer_id="${4:?producer ID required}"
	local comments_json="${5:-[]}"
	local marker response_json
	marker="$(review_blocked_successor_marker "${intent_json}")" || return 1
	if printf '%s' "${comments_json}" | jq -e --arg marker "${marker}" --argjson producer_id "${producer_id}" \
		'any(.[]?; (.author_id == $producer_id) and (.body == $marker))' >/dev/null 2>&1; then
		return 0
	fi
	if ! response_json="$(gh_retry gh api "repos/${repository}/issues/${pr_number}/comments" -f body="${marker}" 2>/dev/null)"; then
		return 1
	fi
	printf '%s' "${response_json}" | jq -e --arg marker "${marker}" --argjson producer_id "${producer_id}" \
		'(.user.id == $producer_id) and (.body == $marker)' >/dev/null 2>&1
}

review_blocked_resolve_successor_issue()
{
	local repository="${1:?repository required}"
	local intent_json="${2:?successor intent JSON required}"
	local producer_id="${3:?producer ID required}"
	local request_id search_json
	request_id="$(printf '%s' "${intent_json}" | jq -r '.request_id // empty' 2>/dev/null || true)"
	if ! [[ "${repository}" =~ ^[^/[:space:]]+/[^/[:space:]]+$ ]] \
		|| ! [[ "${request_id}" =~ ^review_blocked_approval_[0-9]{14}_[0-9a-f]{10}$ ]] \
		|| ! [[ "${producer_id}" =~ ^[1-9][0-9]*$ ]]; then
		printf '%s\n' '{"status":"inconclusive","url":null}'
		return 0
	fi
	# Audited existing calls: PR comment hydration cannot return repository issue
	# candidates. This is one bounded search call; candidate validation is local.
	if ! search_json="$(gh_retry gh api -X GET search/issues \
		-f q="repo:${repository} is:issue is:open in:body REVIEW_BLOCKED_SUCCESSOR_V1 ${request_id}" 2>/dev/null)"; then
		printf '%s\n' '{"status":"inconclusive","url":null}'
		return 0
	fi
	INTENT_JSON="${intent_json}" SEARCH_JSON="${search_json}" PRODUCER_ID="${producer_id}" \
	PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import hashlib
import json
import os
import re

def canonical(value):
	return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

try:
	intent = json.loads(os.environ["INTENT_JSON"])
	search = json.loads(os.environ["SEARCH_JSON"])
	producer_id = int(os.environ["PRODUCER_ID"])
except (KeyError, ValueError, TypeError, json.JSONDecodeError):
	print('{"status":"inconclusive","url":null}')
	raise SystemExit(0)
unsigned = dict(intent)
payload_digest = unsigned.pop("payload_digest", None)
required = {
	"schema_version", "request_id", "action", "source_pr", "linked_issue",
	"approved_head_sha", "producer_id", "successor_type", "title_digest",
	"body_digest", "required_labels",
}
valid_intent = (
	set(unsigned) == required
	and intent.get("schema_version") == "review_blocked_successor.v1"
	and intent.get("producer_id") == producer_id
	and re.fullmatch(r"[0-9a-f]{64}", str(payload_digest or "")) is not None
	and hashlib.sha256(canonical(unsigned).encode()).hexdigest() == payload_digest
)
items = search.get("items") if isinstance(search, dict) else None
if not valid_intent or not isinstance(items, list) or len(items) > 100:
	print('{"status":"inconclusive","url":null}')
	raise SystemExit(0)
opener = "<!-- REVIEW_BLOCKED_SUCCESSOR_V1\n"
closer = "\nREVIEW_BLOCKED_SUCCESSOR_V1 -->"
valid = []
for item in items:
	if not isinstance(item, dict) or item.get("state") != "open":
		continue
	if (item.get("user") or {}).get("id") != producer_id:
		continue
	title = item.get("title")
	body = item.get("body")
	url = item.get("html_url")
	labels = item.get("labels")
	if not isinstance(title, str) or not isinstance(body, str) or not isinstance(url, str) or not isinstance(labels, list):
		continue
	marker_start = body.rfind("\n\n" + opener)
	if marker_start < 0 or not body.endswith(closer):
		continue
	base_body = body[:marker_start]
	marker_json = body[marker_start + 2 + len(opener):-len(closer)]
	try:
		marker_intent = json.loads(marker_json)
	except json.JSONDecodeError:
		continue
	actual_labels = {entry.get("name") for entry in labels if isinstance(entry, dict)}
	if (
		canonical(marker_intent) != canonical(intent)
		or hashlib.sha256(title.encode()).hexdigest() != intent.get("title_digest")
		or hashlib.sha256(base_body.encode()).hexdigest() != intent.get("body_digest")
		or not set(intent.get("required_labels", [])).issubset(actual_labels)
		or re.fullmatch(r"https?://[^\s]+/issues/[1-9][0-9]*", url) is None
	):
		continue
	valid.append(url)
if len(valid) == 1:
	print(canonical({"status": "valid", "url": valid[0]}))
elif valid:
	print('{"status":"inconclusive","url":null}')
else:
	print('{"status":"not_found","url":null}')
PY
}

review_blocked_prepare_successor_issue()
{
	local repository="${1:?repository required}"
	local pr_number="${2:?PR number required}"
	local comments_json="${3:-[]}"
	local request_json="${4:?approval request JSON required}"
	local producer_id="${5:?producer ID required}"
	local successor_type="${6:?successor type required}"
	local successor_title="${7:?successor title required}"
	local successor_body="${8:?successor body required}"
	local required_labels_json="${9:?required labels JSON required}"
	local intent_json resolution_json resolution_status prepared_body
	if ! intent_json="$(review_blocked_build_successor_intent "${request_json}" "${successor_type}" \
		"${successor_title}" "${successor_body}" "${required_labels_json}" "${producer_id}")"; then
		printf '%s\n' '{"status":"inconclusive","url":null,"body":null}'
		return 0
	fi
	if ! review_blocked_ensure_successor_intent "${repository}" "${pr_number}" \
		"${intent_json}" "${producer_id}" "${comments_json}"; then
		printf '%s\n' '{"status":"inconclusive","url":null,"body":null}'
		return 0
	fi
	resolution_json="$(review_blocked_resolve_successor_issue "${repository}" "${intent_json}" "${producer_id}")"
	resolution_status="$(printf '%s' "${resolution_json}" | jq -r '.status // "inconclusive"' 2>/dev/null || echo inconclusive)"
	case "${resolution_status}" in
		valid)
			printf '%s\n' "${resolution_json}"
			;;
		not_found)
			prepared_body="${successor_body}

$(review_blocked_successor_marker "${intent_json}")"
			jq -cn --arg body "${prepared_body}" '{status:"not_found",url:null,body:$body}'
			;;
		*)
			printf '%s\n' '{"status":"inconclusive","url":null,"body":null}'
			;;
	esac
}

_gh_issue_timeline_with_cross_refs_rest()
{
	local owner="$1"
	local repo="$2"
	local issue_number="$3"
	local timeline_json
	local pr_urls
	local pr_url
	local pr_json
	local pr_lookup_json='{}'
	local github_api_base="${GITHUB_API_URL:-https://api.github.com}"
	github_api_base="${github_api_base%/}"
	local pr_api_prefix="${github_api_base}/repos/${owner}/${repo}/pulls/"

	if ! timeline_json="$(gh_retry gh api --paginate "repos/${owner}/${repo}/issues/${issue_number}/timeline" 2>/dev/null | jq -s 'add // []' 2>/dev/null)"; then
		return 1
	fi

	pr_urls="$(printf '%s' "${timeline_json}" | jq -r '[.[] | select(.event == "cross-referenced" and (.source.issue.pull_request.url? | type == "string")) | .source.issue.pull_request.url] | unique | .[]?' 2>/dev/null || true)"
	if [ -n "${pr_urls}" ]; then
		while IFS= read -r pr_url; do
			[ -n "${pr_url}" ] || continue
			if [[ "${pr_url}" != "${pr_api_prefix}"* ]]; then
				continue
			fi
			if pr_json="$(gh_retry gh api "${pr_url}" 2>/dev/null)" && printf '%s' "${pr_json}" | jq -e 'type == "object"' >/dev/null 2>&1; then
				pr_lookup_json="$(jq -c --arg url "${pr_url}" --argjson pr "${pr_json}" '. + {($url): {ok: true, number: ($pr.number // null), state: ($pr.state // null), merged_at: ($pr.merged_at // null), merged: (($pr.merged_at != null) or ($pr.merged == true))}}' <(printf '%s\n' "${pr_lookup_json}") 2>/dev/null || printf '%s' "${pr_lookup_json}")"
			else
				pr_lookup_json="$(jq -c --arg url "${pr_url}" '. + {($url): {ok: false}}' <(printf '%s\n' "${pr_lookup_json}") 2>/dev/null || printf '%s' "${pr_lookup_json}")"
			fi
		done <<< "${pr_urls}"
	fi

	printf '%s' "${timeline_json}" | jq -c --argjson pr_lookup "${pr_lookup_json}" --arg pr_api_prefix "${pr_api_prefix}" '
		map(
			if (.event == "cross-referenced") and (.source.issue.pull_request.url? | type == "string") then
				.source.issue.pull_request.url as $url
				| if ($url | startswith($pr_api_prefix) | not) then
					.source.issue.pull_request = null
				else
					($pr_lookup[$url] // null) as $enrich
					| if ($enrich != null) and ($enrich.ok == true) then
						.source.issue |= (. + {
							number: ($enrich.number // .number // null),
							state: ($enrich.state // null),
							merged_at: ($enrich.merged_at // null),
							merged: ($enrich.merged // false),
							lookup_failed: false
						})
					elif ($enrich != null) and ($enrich.ok == false) then
						.source.issue |= (. + {
							merged: false,
							lookup_failed: true
						})
					else
						.
					end
				end
			else
				.
			end
		)
		| map(select((.event == "cross-referenced") or (.event == "closed")))
	' 2>/dev/null
}

# gh_issue_timeline_with_cross_refs emits the legacy timeline-event shape used
# across jq consumers in scripts/orchestrate_poll_process.sh.
#
# Contract (array of event objects):
# - `.event` (e.g. "cross-referenced", "closed")
# - `.source.issue.number`
# - `.source.issue.pull_request.url` (REST API URL when source is a PR, else null)
# - additive PR enrichment fields for merged checks:
#   `.source.issue.state`, `.source.issue.merged_at`, `.source.issue.merged`,
#   `.source.issue.lookup_failed`
#
# Maintainer fixture capture (manual, replace OWNER/REPO/ISSUE):
# - REST helper output (legacy enriched shape): . scripts/gh_helpers.sh && _gh_issue_timeline_with_cross_refs_rest OWNER REPO ISSUE > scripts/fixtures/issue-timeline/rest_timeline_fixture.json
# - GraphQL helper output (GraphQL-first, fail-open to REST): . scripts/gh_helpers.sh && gh_issue_timeline_with_cross_refs OWNER REPO ISSUE > scripts/fixtures/issue-timeline/graphql_timeline_fixture.json
gh_issue_timeline_with_cross_refs()
{
	local owner="$1"
	local repo="$2"
	local issue_number="$3"
	local graphql_query
	local graphql_json
	local has_next_page
	local transformed_json
	local graphql_api_base="${GITHUB_API_URL:-https://api.github.com}"
	graphql_api_base="${graphql_api_base%/}"

	graphql_query='query($owner: String!, $repo: String!, $issue_number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $issue_number) {
      timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT, CLOSED_EVENT]) {
        pageInfo {
          hasNextPage
        }
        nodes {
          __typename
          ... on CrossReferencedEvent {
            source {
              __typename
              ... on PullRequest {
                number
                state
                mergedAt
                repository {
                  name
                  owner {
                    login
                  }
                }
              }
              ... on Issue {
                number
              }
            }
          }
        }
      }
    }
  }
}'

	if ! graphql_json="$(gh_retry gh api graphql -f query="${graphql_query}" -f owner="${owner}" -f repo="${repo}" -F issue_number="${issue_number}" 2>/dev/null)"; then
		echo "::warning::rate_limit_audit_fallback helper=gh_issue_timeline_with_cross_refs reason=graphql_failed owner=${owner} repo=${repo} issue=${issue_number}" >&2
		_gh_issue_timeline_with_cross_refs_rest "${owner}" "${repo}" "${issue_number}"
		return $?
	fi

	if ! printf '%s' "${graphql_json}" | jq -e '.data.repository.issue.timelineItems.nodes | type == "array"' >/dev/null 2>&1; then
		echo "::warning::rate_limit_audit_fallback helper=gh_issue_timeline_with_cross_refs reason=graphql_payload_invalid owner=${owner} repo=${repo} issue=${issue_number}" >&2
		_gh_issue_timeline_with_cross_refs_rest "${owner}" "${repo}" "${issue_number}"
		return $?
	fi

	if printf '%s' "${graphql_json}" | jq -e '.errors? | (type == "array" and length > 0)' >/dev/null 2>&1; then
		echo "::warning::rate_limit_audit_fallback helper=gh_issue_timeline_with_cross_refs reason=graphql_errors owner=${owner} repo=${repo} issue=${issue_number}" >&2
		_gh_issue_timeline_with_cross_refs_rest "${owner}" "${repo}" "${issue_number}"
		return $?
	fi

	has_next_page="$(printf '%s' "${graphql_json}" | jq -r '.data.repository.issue.timelineItems.pageInfo.hasNextPage // false' 2>/dev/null || echo "true")"
	if [ "${has_next_page}" = "true" ]; then
		echo "::warning::rate_limit_audit_fallback helper=gh_issue_timeline_with_cross_refs reason=timeline_has_next_page owner=${owner} repo=${repo} issue=${issue_number}" >&2
		_gh_issue_timeline_with_cross_refs_rest "${owner}" "${repo}" "${issue_number}"
		return $?
	fi

	if ! transformed_json="$(printf '%s' "${graphql_json}" | jq -c --arg api_base "${graphql_api_base}" --arg owner "${owner}" --arg repo "${repo}" '
		def source_issue($source):
			if ($source | type) != "object" then
				{
					number: null,
					pull_request: null,
					state: null,
					merged_at: null,
					merged: false,
					lookup_failed: false
				}
			elif $source.__typename == "PullRequest" then
				{
					number: ($source.number // null),
					pull_request: (
					if ($source.number != null)
						and ($source.repository.owner.login? != null)
						and ($source.repository.name? != null)
						and (($source.repository.owner.login | ascii_downcase) == ($owner | ascii_downcase))
						and (($source.repository.name | ascii_downcase) == ($repo | ascii_downcase)) then
						{url: ($api_base + "/repos/" + $owner + "/" + $repo + "/pulls/" + ($source.number | tostring))}
					else
							null
						end
					),
					state: (
						if ($source.state // null) == "OPEN" then "open"
						elif (($source.state // null) == "CLOSED") or (($source.state // null) == "MERGED") then "closed"
						else null
						end
					),
					merged_at: ($source.mergedAt // null),
					merged: (($source.mergedAt != null) or (($source.state // "") == "MERGED")),
					lookup_failed: false
				}
			else
				{
					number: ($source.number // null),
					pull_request: null,
					state: null,
					merged_at: null,
					merged: false,
					lookup_failed: false
				}
			end;

		[
			.data.repository.issue.timelineItems.nodes[]?
			| if .__typename == "CrossReferencedEvent" then
				{
					event: "cross-referenced",
					source: {
						issue: source_issue(.source)
					}
				}
			  elif .__typename == "ClosedEvent" then
				{event: "closed"}
			  else
				empty
			  end
		]
	' 2>/dev/null)"; then
		echo "::warning::rate_limit_audit_fallback helper=gh_issue_timeline_with_cross_refs reason=graphql_transform_failed owner=${owner} repo=${repo} issue=${issue_number}" >&2
		_gh_issue_timeline_with_cross_refs_rest "${owner}" "${repo}" "${issue_number}"
		return $?
	fi

	printf '%s\n' "${transformed_json}"
}

# ---------------------------------------------------------------
# autofix_retrigger_has_inflight_peer — detect an already-queued or
# already-running peer review/autofix run on the same PR head branch,
# so the caller can skip an otherwise-redundant workflow_dispatch.
#
# Motivation:
#   review_autofix.yml finishes a commit-and-push cycle and then
#   issues `gh workflow run review_autofix.yml` as a fallback in
#   case the merge-ref for the synchronize event is unbuildable.
#   In the common case the push already fires pull_request.synchronize
#   → internal-review.yml → review_autofix.yml, so both runs land in
#   the same `pr-autofix-${PR}` concurrency group with
#   cancel-in-progress: false. A redundant dispatch can still queue,
#   surface in the Actions UI, and consume a workflow_dispatch API
#   call. This helper lets the
#   retrigger step skip its dispatch when a peer run is already
#   in flight.
#
# Input:
#   $1 pr_number      — PR number (informational, used in log lines)
#   $2 head_branch    — PR head branch name (required for filtering)
#   $3 current_run_id — github.run_id of the CURRENT run, so we can
#                       exclude ourselves from the peer check.
#
# Output (stdout):
#   AUTOFIX_PEER_CHECK pr=<n> branch=<b> current_run=<r> \
#     peer_count=<n> peer_run=<id|-> peer_path=<path|->
#   An AUTOFIX_PEER_QUERY_FAILED line is emitted on API error
#   (stderr) so callers can distinguish a true no-peer result from a
#   probe failure when grepping logs.
#
# Return:
#   0 — at least one in-flight peer found; caller SHOULD skip dispatch
#   1 — no peer found, or the check failed; caller SHOULD proceed
#       (fail-open: never block dispatch on a detection failure)
#
# API calls:
#   Exactly 1 `gh api GET /repos/{repo}/actions/runs` call per
#   invocation, wrapped in gh_retry (rate-limit aware).  Results are
#   not cached — the two retrigger blocks fire at most twice per run
#   and the set of in-flight runs is mutable between those calls.
#
# CLAUDE.md §15 audit:
#   The prior retrigger path dispatched unconditionally, so there is
#   no existing gh call here to extend.  A single list-runs call
#   replaces the wasted dispatch on the collision path, so net API
#   cost is negative when a peer is found and neutral otherwise.
# ---------------------------------------------------------------
autofix_retrigger_has_inflight_peer()
{
	local pr_number="${1:-}"
	local head_branch="${2:-}"
	local current_run_id="${3:-}"

	if [ -z "${head_branch}" ] || [ -z "${current_run_id}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
		echo "AUTOFIX_PEER_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch:-?} reason=missing_inputs" >&2
		return 1
	fi

	local response
	# -X GET is REQUIRED, not decorative: `gh api` infers POST whenever
	# any -f/-F parameter is supplied and no method is given, and
	# POST /repos/{repo}/actions/runs is not a route — it 404s, which
	# _is_gh_permanent_failure classifies as non-retryable, so the
	# probe dies instantly with reason=api_error.  Dropping this flag
	# silently breaks the probe (tele-funtoken-msg-scoring#3763,
	# review run 32732281452).
	if ! response=$(gh_retry gh api \
		-X GET \
		-H "Accept: application/vnd.github+json" \
		"/repos/${GITHUB_REPOSITORY}/actions/runs" \
		-f "branch=${head_branch}" \
		-f "per_page=30" \
		2>/dev/null); then
		echo "AUTOFIX_PEER_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=api_error" >&2
		return 1
	fi

	if [ -z "${response}" ]; then
		echo "AUTOFIX_PEER_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=empty_response" >&2
		return 1
	fi

	# Filter: in-flight (queued or in_progress), not ourselves, and
	# running one of the review/autofix workflow files. Match by
	# workflow file path so renamed jobs in consumer repos still count
	# as peers. The first matched run in API order is surfaced in logs.
	local peer_info
	if ! peer_info=$(printf '%s' "${response}" | jq -r --arg current "${current_run_id}" '
		[
			.workflow_runs[]?
			| select(.status == "queued" or .status == "in_progress")
			| select((.id | tostring) != $current)
			| select(.path | test("(^|/)(review_autofix|internal-review|ai-review)\\.ya?ml$"))
		]
		| {count: length, first_id: (.[0].id // "-"), first_path: (.[0].path // "-")}
		| "\(.count) \(.first_id) \(.first_path)"
	' 2>/dev/null); then
		echo "AUTOFIX_PEER_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=jq_error" >&2
		return 1
	fi

	local peer_count peer_run peer_path
	peer_count=$(printf '%s' "${peer_info}" | awk '{print $1}')
	peer_run=$(printf '%s' "${peer_info}" | awk '{print $2}')
	peer_path=$(printf '%s' "${peer_info}" | awk '{print $3}')

	echo "AUTOFIX_PEER_CHECK pr=${pr_number:-?} branch=${head_branch} current_run=${current_run_id:-?} peer_count=${peer_count:-0} peer_run=${peer_run:--} peer_path=${peer_path:--}"
	emit_event "AUTOFIX_PEER_CHECK" \
		"pr=${pr_number:-?}" \
		"branch=${head_branch}" \
		"current_run=${current_run_id:-?}" \
		"peer_count=${peer_count:-0}" \
		"peer_run=${peer_run:--}" \
		"peer_path=${peer_path:--}"

	if [ "${peer_count:-0}" -gt 0 ] 2>/dev/null; then
		return 0
	fi
	return 1
}

# ---------------------------------------------------------------
# autofix_changes_lost_head_retry_consumed — decide whether the current
# PR head SHA has already consumed its one automated editor-changes-lost
# re-dispatch.
#
# Motivation:
#   The "Re-dispatch review on editor-changes-lost" step in
#   review_autofix.yml must be reachable from workflow_dispatch runs
#   (every autofix iteration after the first is one — the pull_request
#   twins are concurrency-cancelled), yet must not loop: a changes-lost
#   iteration produces NO commit, so the head SHA never advances and an
#   unbounded re-dispatch would spin forever on the same head.  The
#   bound that replaces the old `github.event.pull_request.number`
#   event-payload guard (unreachable on dispatch runs — see the
#   tele-funtoken-msg-scoring#3757 stall, run 32659591000) is the head
#   SHA itself: a completed, non-cancelled review run already recorded
#   on this exact head means this run IS the automated retry, so no
#   further dispatch is allowed.
#
# Input:
#   $1 pr_number      — PR number (informational, used in log lines)
#   $2 head_branch    — PR head branch name (required for filtering)
#   $3 current_run_id — github.run_id of the CURRENT run (excluded)
#   $4 head_sha       — the PR head commit this run reviewed (required)
#
# Output (stdout):
#   AUTOFIX_CHANGES_LOST_BUDGET pr=<n> branch=<b> head_sha=<sha> \
#     current_run=<r> prior_completed=<n>
#   An AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED line is emitted on
#   probe failure (stderr).
#
# Return:
#   0 — budget consumed (a prior completed non-cancelled review run
#       exists on this head, or the probe failed); caller MUST skip
#       the dispatch.  Fail-CLOSED, unlike the peer helper above: this
#       helper guards an otherwise-unbounded dispatch loop, so an
#       unanswerable probe keeps the pre-fix no-dispatch behaviour.
#   1 — budget available; caller may dispatch one automated retry.
#
# API calls:
#   Exactly 1 `gh api GET /repos/{repo}/actions/runs` call per
#   invocation, wrapped in gh_retry (§15: same single branch-scoped
#   list the peer helper issues; the two probes run back-to-back in
#   one step at most once per review run).
# ---------------------------------------------------------------
autofix_changes_lost_head_retry_consumed()
{
	local pr_number="${1:-}"
	local head_branch="${2:-}"
	local current_run_id="${3:-}"
	local head_sha="${4:-}"

	if [ -z "${head_branch}" ] || [ -z "${current_run_id}" ] || [ -z "${head_sha}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
		echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch:-?} reason=missing_inputs" >&2
		return 0
	fi

	local response
	# -X GET is REQUIRED, not decorative: `gh api` infers POST whenever
	# any -f/-F parameter is supplied and no method is given, and
	# POST /repos/{repo}/actions/runs is not a route — it 404s, which
	# _is_gh_permanent_failure classifies as non-retryable, so the
	# probe dies instantly with reason=api_error.  Dropping this flag
	# silently breaks the probe (tele-funtoken-msg-scoring#3763,
	# review run 32732281452).
	if ! response=$(gh_retry gh api \
		-X GET \
		-H "Accept: application/vnd.github+json" \
		"/repos/${GITHUB_REPOSITORY}/actions/runs" \
		-f "branch=${head_branch}" \
		-f "per_page=30" \
		2>/dev/null); then
		echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=api_error" >&2
		return 0
	fi

	if [ -z "${response}" ]; then
		echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=empty_response" >&2
		return 0
	fi

	# Completed, non-cancelled review-family runs on this exact head,
	# excluding the current run.  A cancelled run is the concurrency-
	# cancelled pull_request twin of a dispatch run and never executed
	# the editor, so it does not consume the budget.
	local prior_completed
	if ! prior_completed=$(printf '%s' "${response}" | jq -r \
		--arg current "${current_run_id}" \
		--arg head "${head_sha}" '
		[
			.workflow_runs[]?
			| select(.status == "completed")
			| select((.conclusion // "") != "cancelled")
			| select((.id | tostring) != $current)
			| select((.head_sha // "") == $head)
			| select(.path | test("(^|/)(review_autofix|internal-review|ai-review)\\.ya?ml$"))
		]
		| length
	' 2>/dev/null); then
		echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=jq_error" >&2
		return 0
	fi

	echo "AUTOFIX_CHANGES_LOST_BUDGET pr=${pr_number:-?} branch=${head_branch} head_sha=${head_sha} current_run=${current_run_id} prior_completed=${prior_completed:-0}"

	if [ "${prior_completed:-0}" -gt 0 ] 2>/dev/null; then
		return 0
	fi
	return 1
}

# ---------------------------------------------------------------
# Prompt-input embedding helpers.
#
# Editor / reviewer / judge prompts that used to tell the model
# "Read the following files: - $FOO" pay a per-file exec round-trip
# (cat) that eats reasoning budget before the model can act.  The
# helpers below let the prompt builder inline file contents directly
# while enforcing two invariants the model cannot guarantee on its
# own:
#
#   1. **Per-file cap** — never emit more than `byte_cap` bytes from
#      a single file (default 100000).  For diff files the caller
#      should pass a larger cap and `truncate_mode=diff`, which keeps
#      the head of the file aligned to the previous `^diff --git`
#      boundary so the model never sees a half-hunk.
#   2. **Total-prompt budget** — across ALL `_embed_input_file`
#      invocations in the current shell process the cumulative bytes
#      stay under `_PROMPT_BUDGET_TOTAL_BYTES` (default 800000 ≈
#      200k tokens at ~4 bytes/token).  Sized to fit comfortably
#      within the gpt-5.5 (capacity-fallback floor) context window (272k tokens) once
#      the static prefix (system instructions / agents / pipeline
#      doc, ~10k tokens) and the response budget (~30k tokens) are
#      subtracted.  After the budget is exhausted later files are
#      replaced with an explicit "(omitted — budget exhausted)"
#      marker so the model knows context is incomplete.
#
# The budget tracker uses a state file under $TMPDIR so it survives
# subshell invocations from $(...) command-substitution inside a
# heredoc.  Callers should run `_init_prompt_budget` before assembling
# the prompt and `_cleanup_prompt_budget` after the prompt is written.
# ---------------------------------------------------------------

# Default total prompt budget (bytes).  Set the env var BEFORE sourcing
# gh_helpers.sh to override.  See header comment above for the sizing
# rationale (200k tokens within the gpt-5.5 fallback's 272k context window after
# static prefix + response budget).
: "${_PROMPT_BUDGET_TOTAL_BYTES:=800000}"

# Resolve a stable per-process state-file path.  $$ in a subshell is
# the parent shell's PID, so all $(...) invocations under one parent
# share the same file.
_prompt_budget_state_file()
{
	printf '%s/_prompt_input_budget_state.%s\n' "${TMPDIR:-/tmp}" "$$"
}

# Initialise (or reset) the running-total state file.  Optionally
# override the per-process total cap with the first argument.
_init_prompt_budget()
{
	local _cap="${1:-${_PROMPT_BUDGET_TOTAL_BYTES}}"
	local _state
	_state="$(_prompt_budget_state_file)"
	export _PROMPT_BUDGET_TOTAL_BYTES="${_cap}"
	printf '0\n' > "${_state}" 2>/dev/null || true
}

# Remove the per-process state file.  Idempotent.
_cleanup_prompt_budget()
{
	local _state
	_state="$(_prompt_budget_state_file)"
	rm -f "${_state}" 2>/dev/null || true
}

# _embed_input_file <path> [byte_cap] [truncate_mode]
#
#   byte_cap        — defaults to 100000.  Caller can pass a larger
#                     value for the few files that genuinely need it
#                     (diffs, reviewer bundles, comment dumps).
#   truncate_mode   — `head` (default) or `diff`.  `diff` mode keeps
#                     the file truncated at the last whole `^diff --git`
#                     boundary that fits, so the model never sees a
#                     half-hunk that would mis-scope its findings.
#
# Output is plain bytes safe to drop into a heredoc; no shell expansion
# is performed on the file content.  When the per-file cap or the
# global budget is hit, an explicit `[... TRUNCATED ...]` / `(omitted —
# budget exhausted)` marker is emitted so the model knows the section
# is incomplete instead of silently believing it saw everything.
_embed_input_file()
{
	local _path="${1:-}"
	local _cap="${2:-100000}"
	local _mode="${3:-head}"
	if [ -z "${_path}" ] || [ ! -e "${_path}" ]; then
		printf '(missing)\n'
		return 0
	fi
	if [ ! -s "${_path}" ]; then
		printf '(empty)\n'
		return 0
	fi

	# Honour the running total budget.  If we have no remaining
	# budget at all, emit the omitted-marker and return.
	local _state _used _budget_remaining _effective_cap
	_state="$(_prompt_budget_state_file)"
	_used=0
	if [ -f "${_state}" ]; then
		_used="$(cat "${_state}" 2>/dev/null)"
		[[ "${_used}" =~ ^[0-9]+$ ]] || _used=0
	fi
	_budget_remaining=$(( _PROMPT_BUDGET_TOTAL_BYTES - _used ))
	if [ "${_budget_remaining}" -le 0 ]; then
		printf '(omitted — total prompt input budget %d bytes exhausted; %d bytes already inlined)\n' \
			"${_PROMPT_BUDGET_TOTAL_BYTES}" "${_used}"
		return 0
	fi

	_effective_cap="${_cap}"
	if [ "${_budget_remaining}" -lt "${_effective_cap}" ]; then
		_effective_cap="${_budget_remaining}"
	fi

	local _size
	_size="$(wc -c < "${_path}" 2>/dev/null | tr -d '[:space:]')"
	[[ "${_size}" =~ ^[0-9]+$ ]] || _size=0

	local _emit_bytes
	if [ "${_size}" -le "${_effective_cap}" ]; then
		# Whole file fits; emit verbatim.  No need for a trailing newline:
		# command substitution `$(...)` strips trailing newlines and the
		# enclosing heredoc places its own \n after the substitution, so
		# the closing fence always lands on its own line.
		cat "${_path}"
		_emit_bytes="${_size}"
	else
		# File would overflow the effective cap.  In `diff` mode, walk
		# back from the cap to the last `^diff --git` boundary so the
		# model never sees a half-hunk; in `head` mode just take the
		# first _effective_cap bytes.  Either way emit an explicit
		# truncation marker so the model knows the section is partial.
		local _head_bytes="${_effective_cap}"
		if [ "${_mode}" = "diff" ]; then
			# Find the last `^diff --git` whose start-of-line lies <= cap.
			# `grep -b` outputs `BYTE_OFFSET:LINE_CONTENT` (no line numbers).
			# awk picks the largest byte offset still inside the cap; we
			# skip offset 0 because truncating before the first hunk would
			# emit nothing (head mode handles that case correctly).
			local _boundary
			_boundary="$(grep -bE '^diff --git ' "${_path}" 2>/dev/null \
				| awk -F: -v cap="${_effective_cap}" \
					'($1+0) > 0 && ($1+0) <= cap { last=$1+0 } END { print last+0 }')"
			if [ -n "${_boundary}" ] && [ "${_boundary}" -gt 0 ]; then
				_head_bytes="${_boundary}"
			fi
		fi
		head -c "${_head_bytes}" "${_path}"
		printf '\n[... TRUNCATED — file is %s bytes; first %s bytes shown above (mode=%s, per-file cap=%s, budget remaining was %s) ...]\n' \
			"${_size}" "${_head_bytes}" "${_mode}" "${_cap}" "${_budget_remaining}"
		_emit_bytes="${_head_bytes}"
	fi

	# Update running total.  Best-effort: if the state file write
	# fails we still emitted the content (don't poison the prompt).
	if [ -n "${_emit_bytes}" ] && [ "${_emit_bytes}" -gt 0 ] 2>/dev/null; then
		printf '%s\n' "$(( _used + _emit_bytes ))" > "${_state}" 2>/dev/null || true
	fi
}

# ---------------------------------------------------------------
# sanitize_codex_prompt_file <path>
#
# Rewrite <path> in place with any invalid UTF-8 byte sequences
# stripped, so the Codex CLI's strict UTF-8 stdin reader never
# rejects the prompt on the first invalid byte. This is the
# last-line-of-defence sanitisation for prompt files that get piped
# to `codex … exec … < "${path}"`.
#
# Background: Codex CLI reads its prompt from stdin as a UTF-8
# string and aborts on the first invalid byte with
#   `Failed to read prompt from stdin: input is not valid UTF-8
#    (invalid byte at offset N). Convert it to UTF-8 and retry`
# Any upstream byte-based truncation that lands mid-codepoint
# (e.g. mawk's byte-oriented `substr` on a 3-byte em-dash) — or any
# other corruption that leaks invalid bytes into an embedded input
# file — deterministically kills the editor, and the retry loop is
# impotent because the same bad bytes survive into the regenerated
# prompt. This helper makes that failure mode impossible at the
# stdin boundary.
#
# Behaviour notes:
#   • Best-effort. No-op if <path> is missing/empty or if the temp
#     file write fails.
#   • Prefers `iconv -f UTF-8 -t UTF-8//IGNORE` when available.
#     GNU iconv exits non-zero whenever it discards bytes even though
#     the rewritten output is correct, so we accept any non-empty
#     output regardless of exit status.
#   • Falls back to `python3` UTF-8 decode/encode with
#     `errors="ignore"` when iconv is missing, unsupported (musl),
#     or discards the entire file. That preserves the documented
#     "last line of defence" behaviour even for all-invalid inputs.
#   • Idempotent. Running on already-valid UTF-8 leaves the file
#     byte-identical (modulo the mv/rename, which the caller's
#     downstream `wc -c` / `sha256sum` instrumentation will reflect).
#   • Silent on success. The caller's existing `wc -c` / `sha256sum`
#     echo lines around the codex pipe will surface any byte-count
#     delta after sanitisation, so we don't add log noise here.
# ---------------------------------------------------------------
sanitize_codex_prompt_file()
{
	local _path="${1:-}"
	if [ -z "${_path}" ] || [ ! -f "${_path}" ]; then
		return 0
	fi
	local _tmp
	_tmp="$(mktemp "${_path}.utf8XXXXXX" 2>/dev/null)" || return 0
	if command -v iconv >/dev/null 2>&1; then
		# //IGNORE discards invalid sequences. iconv exits non-zero
		# whenever it skips any, but the output IS correct, so don't
		# gate the rewrite on $? — gate on whether output was produced.
		iconv -f UTF-8 -t UTF-8//IGNORE < "${_path}" > "${_tmp}" 2>/dev/null || true
		if [ -s "${_tmp}" ] || [ ! -s "${_path}" ]; then
			mv "${_tmp}" "${_path}" 2>/dev/null || rm -f "${_tmp}"
			return 0
		fi
		: > "${_tmp}" 2>/dev/null || { rm -f "${_tmp}"; return 0; }
	fi
	if command -v python3 >/dev/null 2>&1; then
		if python3 - "${_path}" "${_tmp}" <<'PY' 2>/dev/null
from pathlib import Path
import sys

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
dst.write_bytes(src.read_bytes().decode("utf-8", "ignore").encode("utf-8"))
PY
		then
			mv "${_tmp}" "${_path}" 2>/dev/null || rm -f "${_tmp}"
			return 0
		fi
	fi
	rm -f "${_tmp}"
}

# ---------------------------------------------------------------
# extract_repo_scoped_issue_refs_from_text <owner/repo> <text>
#
# Print deduplicated issue numbers referenced by strict current-repo
# closing keywords or full repo-scoped issue URLs/paths.
#
# Accepted:
#   Fixes #12
#   closes #34
#   owner/repo/issues/56
#   https://github.com/owner/repo/issues/78
#
# Rejected:
#   issue #12
#   issues/12
#   Closes: #12
#
# Fail-open:
#   empty text or malformed repository input emits no matches
# ---------------------------------------------------------------
extract_repo_scoped_issue_refs_from_text()
{
	local _repository="${1:-}"
	local _text="${2:-}"
	local _repository_escaped

	if [ -z "${_repository}" ] || [ -z "${_text}" ] || ! [[ "${_repository}" =~ ^[^/]+/[^/]+$ ]]; then
		return 0
	fi

	_repository_escaped="$(printf '%s' "${_repository}" | sed 's/[][\\.^$*+?(){}|]/\\&/g')"
	printf '%s\n' "${_text}" \
		| grep -oiE "((^|[^[:alnum:]_])github\\.com/${_repository_escaped}/issues/[0-9]+([^[:alnum:]_]|$)|(^|[^[:alnum:]_])${_repository_escaped}/issues/[0-9]+([^[:alnum:]_]|$)|(^|[^[:alnum:]_/-])(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)[[:space:]]+#[[:space:]]*[0-9]+([^[:alnum:]_]|$))" \
		| sed -nE 's/.*[^0-9]([0-9]+)[^0-9]*$/\1/p' \
		| sort -un || true
}
