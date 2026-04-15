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
# use exponential backoff (1 s, 2 s, 4 s, …).

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

	echo "::warning::  Rate limit resets in ${wait_secs}s (X-RateLimit-Reset: ${reset_epoch:-unknown})" >&2
	sleep "${wait_secs}"
}

# ---------------------------------------------------------------
# _parse_reset_header — extract X-RateLimit-Reset from a header
# dump file (produced by curl -D).
#
# Prints the epoch timestamp to stdout; empty string on failure.
# ---------------------------------------------------------------
_parse_reset_header()
{
	local header_file="$1"
	grep -i '^x-ratelimit-reset:' "${header_file}" 2>/dev/null \
		| head -1 | awk '{print $2}' | tr -d '\r'
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
	# so when the operator has configured ALERT_MSG_LEVEL=ERROR or
	# CRITICAL the rate-limit alert is suppressed (no send, no pin
	# update, cooldown window is not advanced).
	case "$(printf '%s' "${ALERT_MSG_LEVEL:-DEBUG}" | tr '[:lower:]' '[:upper:]')" in
		DEBUG|WARNING) : ;;
		ERROR|CRITICAL) return 0 ;;
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

		if _is_gh_rate_limit "${stderr_content}"; then
			echo "::warning::GitHub API rate limit hit (attempt ${attempt}/${max_attempts}), waiting for reset…" >&2
			_gh_ratelimit_tg_alert
			_gh_rate_limit_wait
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

		if _is_gh_rate_limit "${stderr_content}"; then
			echo "::warning::GitHub API rate limit hit (attempt ${attempt}/${max_attempts}), waiting for reset…" >&2
			_gh_ratelimit_tg_alert
			_gh_rate_limit_wait
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
				_gh_rate_limit_wait
			else
				local wait_secs=$(( 2 ** (attempt - 1) ))
				echo "::warning::gh command failed (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
				if [ -n "${stderr_content}" ]; then
					echo "::warning::  stderr: ${stderr_content}" >&2
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
