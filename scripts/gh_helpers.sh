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
#   gh_api_retry         — classified `gh api` retry, exit 0/1/2/75 (see below)
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

# Shared PAT counters are only an estimate of job usage: other jobs may run
# concurrently. /rate_limit does not debit the primary REST allowance.
gh_pat_budget()
{
	local phase="$1" workflow="$2" job="$3" snapshot_file="$4" response payload header_resource header_remaining header_reset remaining reset used="unknown" previous_remaining previous_reset
	case "${phase}" in start|end) ;; *) return 1 ;; esac
	remaining="unknown"
	reset="unknown"
	if response="$(gh api -i rate_limit 2>/dev/null)"; then
		payload="${response}"
		if [[ "${response}" == *$'\r\n\r\n'* ]]; then
			payload="${response##*$'\r\n\r\n'}"
		elif [[ "${response}" == *$'\n\n'* ]]; then
			payload="${response##*$'\n\n'}"
		fi
		read -r remaining reset < <(printf '%s' "${payload}" | jq -r '
			if (.resources.core.remaining | type) == "number" and
			   (.resources.core.reset | type) == "number" and
			   .resources.core.remaining >= 0 and .resources.core.reset > 0 then
				[(.resources.core.remaining | floor), (.resources.core.reset | floor)] | @tsv
			else "unknown\tunknown" end' 2>/dev/null || printf 'unknown\tunknown')
		# Headers describe the authenticated request, not the overview.
		# Override core only when the response identifies the core resource.
		header_resource="$(printf '%s\n' "${response}" | tr -d '\r' | grep -im1 '^x-ratelimit-resource:' | cut -d: -f2 | tr -d '[:space:]' || true)"
		if [ "${header_resource}" = "core" ]; then
			header_remaining="$(printf '%s\n' "${response}" | tr -d '\r' | grep -im1 '^x-ratelimit-remaining:' | cut -d: -f2 | tr -d '[:space:]' || true)"
			header_reset="$(printf '%s\n' "${response}" | tr -d '\r' | grep -im1 '^x-ratelimit-reset:' | cut -d: -f2 | tr -d '[:space:]' || true)"
			if [[ "${header_remaining}" =~ ^[0-9]+$ && "${header_reset}" =~ ^[0-9]+$ ]]; then
				remaining="${header_remaining}"
				reset="${header_reset}"
			fi
		fi
	fi
	if [ "${phase}" = "start" ]; then
		if [ "${remaining}" != "unknown" ] && [ "${reset}" != "unknown" ]; then
			printf '%s %s\n' "${remaining}" "${reset}" > "${snapshot_file}" 2>/dev/null || true
		fi
	elif [ -r "${snapshot_file}" ]; then
		read -r previous_remaining previous_reset < "${snapshot_file}" || true
		if [[ "${previous_remaining:-}" =~ ^[0-9]+$ ]] && [[ "${previous_reset:-}" =~ ^[0-9]+$ ]] &&
		   [[ "${remaining}" =~ ^[0-9]+$ ]] && [ "${reset}" = "${previous_reset}" ] &&
		   [ "${previous_remaining}" -ge "${remaining}" ]; then
			used=$((previous_remaining - remaining))
		fi
	fi
	printf 'GH_PAT_BUDGET phase=%s workflow=%s job=%s remaining=%s reset=%s used_in_job=%s\n' \
		"${phase}" "${workflow}" "${job}" "${remaining}" "${reset}" "${used}"
	return 0
}

# Synchronized job-local PR state for the reviewer and editor watchdogs.
# With no job-local runner directory, retain the legacy live-read behavior.
# A cache entry is never used as merge authorization; merge paths still read
# the current head. A read failure remains an uncached conservative "open".
gh_review_pr_state()
{
	local repo="$1" number="$2" root="${RUNNER_TEMP:-}" cache_file now saved_repo saved_number saved_state expires state
	if [[ ! "${repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ ! "${number}" =~ ^[1-9][0-9]*$ ]]; then
		printf 'open\n'
		return 0
	fi
	if [ -z "${root}" ] || ! command -v flock >/dev/null 2>&1; then
		gh_retry gh api "repos/${repo}/pulls/${number}" --jq .state 2>/dev/null | grep -xE 'open|closed|merged' || echo open
		return 0
	fi
	cache_file="${root}/review_pr_state_${GITHUB_RUN_ID:-local}_${GITHUB_RUN_ATTEMPT:-1}_${repo//\//_}_${number}"
	(
		flock -x 9 || { echo open; exit 0; }
		now="$(date +%s)"
		if [ -r "${cache_file}" ]; then
			read -r saved_repo saved_number saved_state expires < "${cache_file}" || true
			if [ "${saved_repo:-}" = "${repo}" ] && [ "${saved_number:-}" = "${number}" ] &&
			   [[ "${expires:-}" =~ ^[0-9]+$ ]] && [ "${expires}" -gt "${now}" ] &&
			   [ "${saved_state:-}" = "open" ]; then
				printf '%s\n' "${saved_state}"
				exit 0
			fi
		fi
		state="$(gh_retry gh api "repos/${repo}/pulls/${number}" --jq .state 2>/dev/null)" || state=""
		if [[ "${state}" =~ ^(open|closed|merged)$ ]]; then
			if [ "${state}" = "open" ]; then
				printf '%s %s %s %s\n' "${repo}" "${number}" "${state}" "$((now + 120))" > "${cache_file}.tmp" &&
					mv -f "${cache_file}.tmp" "${cache_file}"
			fi
			printf '%s\n' "${state}"
		else
			printf 'open\n'
		fi
	) 9>"${cache_file}.lock"
}

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
	# [bucket] (core | graphql | search …, default core): wait for the reset
	# of the bucket that was limited, read from the /rate_limit body. The
	# response header describes only the core bucket, so it is the fallback.
	local _bucket="${1:-core}" _resp _reset_ts=""
	[[ "${_bucket}" =~ ^[a-z_]+$ ]] || _bucket=core
	_resp=$(gh api -i /rate_limit 2>/dev/null) || _resp=""
	if [ -n "${_resp}" ]; then
		_reset_ts=$(printf '%s\n' "${_resp}" | tr -d '\r' | awk 'f { print } /^$/ { f = 1 }' \
			| jq -r --arg b "${_bucket}" '.resources[$b].reset // empty' 2>/dev/null | head -1) || _reset_ts=""
		if [[ "${_reset_ts}" =~ ^[0-9]+$ ]]; then
			# Exact reset of the limited bucket for gh_rate_limit_breaker_active.
			_gh_rate_limit_trip_breaker "${_bucket}" "${_reset_ts}"
		else
			_reset_ts=""
		fi
		if [ -z "${_reset_ts}" ]; then
			_reset_ts=$(printf '%s\n' "${_resp}" \
				| grep -i '^x-ratelimit-reset:' | head -1 \
				| awk '{print $2}' | tr -d '\r') || true
		fi
	fi
	_sleep_until_reset "${_reset_ts}"
}

# _gh_rate_limit_wait_for <stderr text> <command…> — a secondary limit waits
# 60 s (gh's stderr carries no retry-after); a primary limit waits for the
# reset of the command's bucket.
_gh_rate_limit_wait_for()
{
	local _text="$1"; shift
	if printf '%s' "${_text}" | grep -qiE 'secondary rate|abuse detection'; then
		echo "::warning::  Secondary rate limit; waiting 60s" >&2
		sleep 60
	else
		_gh_rate_limit_wait "$(_gh_cmd_bucket "$@")"
	fi
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
	# [bucket] [reset_epoch] [low]: with arguments, also append a
	# "<bucket> <reset_epoch> [low]" line read by gh_rate_limit_breaker_active.
	# The bare file still means "tripped" for gh_rate_limit_breaker_tripped.
	touch "${_GH_RATE_LIMIT_BREAKER_FILE}" 2>/dev/null || true
	if [[ "${1:-}" =~ ^[a-z_]+$ ]] && [[ "${2:-}" =~ ^[0-9]+$ ]]; then
		printf '%s %s%s\n' "$1" "$2" "${3:+ $3}" >> "${_GH_RATE_LIMIT_BREAKER_FILE}" 2>/dev/null || true
	fi
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
# gh_api_retry — rate-limit-aware `gh api` wrapper (issue #5873 / #6634).
#
# Usage:
#   gh_api_retry [--idempotent] [--optional] <gh api args…>
#
# Each attempt runs `gh api -i "$@"` (without -i for --paginate, whose
# headers would interleave with page bodies), splits the status line and
# headers from the body, and writes ONLY the successful attempt's body
# (the --jq output when --jq is given) to stdout. A failed attempt's
# body never reaches stdout.
#
# Failure classes (_gh_api_classify; scripts/gh_api_retry.py is the
# Python twin and shares tests/fixtures/gh_api_retry/classify_cases.json):
#   primary    403/429 with x-ratelimit-remaining: 0 → wait until
#              x-ratelimit-reset of the bucket named by x-ratelimit-resource
#              (else the endpoint's bucket: core / graphql / search)
#   secondary  numeric retry-after, a "secondary rate limit" / "abuse
#              detection" message, or a bare 429 → wait retry-after (60 s
#              when absent)
#   transient  5xx, 408, or no status (network error) → exponential backoff
#              (base 2 s, 0-2 s jitter, capped at GH_RETRY_BACKOFF_CAP_SECS)
#   permanent  every other 4xx, a non-limit 403 → no retry
#
# Never sleeps after the last attempt. A rate-limit wait longer than
# GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS (default 600) gives up at once.
# A POST create (see _gh_api_args_unsafe_post) makes ONE attempt unless
# --idempotent (or GH_RETRY_IDEMPOTENT=true) marks it safe to repeat.
# --optional returns 75 without calling GitHub while the breaker for the
# endpoint's bucket is active (a recorded limit or low remaining budget).
#
# Return codes:
#   0  success
#   1  transient failure, attempts exhausted
#   2  permanent failure                       (GH_API_RETRY_RC_PERMANENT)
#   75 rate-limited and gave up / optional call skipped
#                                              (GH_API_RETRY_RC_RATE_LIMITED)
# When not called inside $(...), GH_API_RETRY_LAST_STATUS and
# GH_API_RETRY_LAST_KIND describe the last attempt.
#
# Log line (stderr): GH_API_RETRY outcome=<retry|gave_up|permanent|skipped>
#   kind=<…> bucket=<…> status=<NNN|none> attempt=<i>/<n> wait_secs=<s>
#   endpoint=<escaped>. Response headers, bodies and tokens are never logged.
#
# API budget (CLAUDE.md §15): no new calls in steady state; a retry repeats
# only a failed call, and a --paginate rate limit reads /rate_limit, which
# does not count against the primary allowance.
# ---------------------------------------------------------------
GH_API_RETRY_RC_PERMANENT=2
GH_API_RETRY_RC_RATE_LIMITED=75

# Flags of `gh api` that consume the following argument as their value.
_gh_api_flag_takes_value()
{
	case "$1" in
		-X|--method|-f|-F|--field|--raw-field|--input|-H|--header|-q|--jq|-t|--template|--hostname|-p|--preview|--cache) return 0 ;;
	esac
	return 1
}

# _gh_api_endpoint <gh api args…> — first positional argument.
_gh_api_endpoint()
{
	local _a _skip=0
	for _a in "$@"; do
		if [ "${_skip}" = "1" ]; then _skip=0; continue; fi
		if _gh_api_flag_takes_value "${_a}"; then _skip=1; continue; fi
		case "${_a}" in
			-*) continue ;;
		esac
		printf '%s\n' "${_a}"
		return 0
	done
	return 0
}

# _gh_api_bucket <gh api args…> — rate-limit resource of the endpoint.
_gh_api_bucket()
{
	local _ep
	_ep="$(_gh_api_endpoint "$@")"
	_ep="${_ep#/}"
	case "${_ep}" in
		graphql) printf 'graphql\n' ;;
		search/*) printf 'search\n' ;;
		*) printf 'core\n' ;;
	esac
}

# _gh_cmd_bucket <full command…> — bucket of a gh_retry-style command.
_gh_cmd_bucket()
{
	if [ "${1:-}" = "gh" ] && [ "${2:-}" = "api" ]; then
		_gh_api_bucket "${@:3}"
	elif [ "${1:-}" = "_safe_gh_jq" ]; then
		_gh_api_bucket "${@:2}"
	else
		printf 'core\n'
	fi
}

# _gh_api_args_unsafe_post <gh api args…>
# True (0) when the call is a non-idempotent POST create:
#   * explicit -X POST / -XPOST / --method POST / --method=POST (any case);
#   * no method and any -f/-F/--field/--raw-field/--input (gh infers POST).
# An explicit GET/PUT/PATCH/DELETE is safe. The graphql endpoint is a read
# (retryable) unless its document starts with "mutation": an inline query=,
# a query=@file, or the .query of an --input JSON file. A document that cannot
# be read (missing file, stdin "-", invalid JSON) counts as a mutation.
_gh_api_args_unsafe_post()
{
	local _a _next="" _method="" _fields=0 _mutation=0 _val _ep _input="" _has_input=0 _qf _q
	for _a in "$@"; do
		_val=""
		case "${_next}" in
			method) _method="${_a}"; _next=""; continue ;;
			field) _fields=1; _val="${_a}"; _next="" ;;
			input) _fields=1; _has_input=1; _input="${_a}"; _next=""; continue ;;
			skip) _next=""; continue ;;
		esac
		if [ -z "${_val}" ]; then
			case "${_a}" in
				-X|--method) _next=method; continue ;;
				--method=*) _method="${_a#--method=}"; continue ;;
				-X*) _method="${_a#-X}"; continue ;;
				-f|-F|--field|--raw-field) _next=field; continue ;;
				--field=*) _fields=1; _val="${_a#--field=}" ;;
				--raw-field=*) _fields=1; _val="${_a#--raw-field=}" ;;
				-f?*|-F?*) _fields=1; _val="${_a#-?}" ;;
				--input) _next=input; continue ;;
				--input=*) _fields=1; _has_input=1; _input="${_a#--input=}"; continue ;;
				*)
					if _gh_api_flag_takes_value "${_a}"; then _next=skip; fi
					continue
					;;
			esac
		fi
		case "${_val}" in
			query=@*)
				_qf="${_val#query=@}"
				if [ "${_qf}" = "-" ] || [ ! -r "${_qf}" ] \
					|| _gh_graphql_doc_is_mutation "$(head -c 65536 "${_qf}" 2>/dev/null)"; then
					_mutation=1
				fi
				;;
			query=*)
				if _gh_graphql_doc_is_mutation "${_val#query=}"; then
					_mutation=1
				fi
				;;
		esac
	done
	_method="$(printf '%s' "${_method}" | tr '[:lower:]' '[:upper:]')"
	if [ -n "${_method}" ] && [ "${_method}" != "POST" ]; then
		return 1
	fi
	if [ -z "${_method}" ] && [ "${_fields}" != "1" ]; then
		return 1
	fi
	_ep="$(_gh_api_endpoint "$@")"
	_ep="${_ep#/}"
	if [ "${_ep}" = "graphql" ]; then
		if [ "${_has_input}" = "1" ]; then
			if [ "${_input}" = "-" ] || [ ! -r "${_input}" ] \
				|| ! _q="$(jq -r 'if type == "object" then (.query // "") else error("not an object") end' "${_input}" 2>/dev/null)" \
				|| _gh_graphql_doc_is_mutation "${_q}"; then
				_mutation=1
			fi
		fi
		[ "${_mutation}" = "1" ]
		return $?
	fi
	return 0
}

# _gh_graphql_doc_is_mutation <document> — true when any operation keyword
# (at the start of the document or after a closing brace, ignoring whitespace
# and # comments) is "mutation". A document holding a query and a mutation,
# where operationName may select the mutation, therefore counts as a mutation.
# A field named "mutation" after a "}" also matches; that only costs a retry.
# Pure bash: no pipeline, so callers running under pipefail cannot see a
# SIGPIPE status.
_gh_graphql_doc_is_mutation()
{
	local _line _out="" _re='(^|\})[[:space:]]*[Mm][Uu][Tt][Aa][Tt][Ii][Oo][Nn]([^A-Za-z0-9_]|$)'
	while IFS= read -r _line || [ -n "${_line}" ]; do
		_out+="${_line%%#*} "
	done <<< "$1"
	[[ "${_out}" =~ ${_re} ]]
}

# _gh_cmd_is_unsafe_post <full command…> — gh_retry-style command check.
# Covers `gh api …`, `_safe_gh_jq …` and gh's own create subcommands
# (`gh issue|pr create|comment`, `gh workflow run`, `gh release create`).
# `gh issue edit --add-label` and other edits stay retryable.
_gh_cmd_is_unsafe_post()
{
	if [ "${1:-}" = "_safe_gh_jq" ]; then
		_gh_api_args_unsafe_post "${@:2}"
		return $?
	fi
	[ "${1:-}" = "gh" ] || return 1
	case "${2:-} ${3:-}" in
		"api "*) _gh_api_args_unsafe_post "${@:3}"; return $? ;;
		"issue create"|"issue comment"|"pr create"|"pr comment"|"workflow run"|"release create") return 0 ;;
	esac
	return 1
}

# _gh_api_header <headers_file> <name> — value of the first matching header.
_gh_api_header()
{
	[ -r "$1" ] || return 0
	grep -i "^$2:" "$1" 2>/dev/null | head -1 | sed 's/^[^:]*:[[:space:]]*//; s/[[:space:]]*$//' | tr -d '\r' || true
}

# _gh_api_classify <status> <headers_file> <stderr_file> <body_file>
# Prints "<kind> <wait_secs> <bucket>": kind is primary|secondary|transient|
# permanent; wait_secs is -1 when a primary limit carries no reset (the
# caller then reads /rate_limit) and 0 for permanent/transient; bucket is
# the x-ratelimit-resource header or "-". GH_API_RETRY_NOW overrides the
# clock (tests only).
_gh_api_classify()
{
	local _status="$1" _hdr="$2" _err="$3" _body="$4" _text _remaining _reset _retry_after _resource _now _wait
	_now="${GH_API_RETRY_NOW:-$(date +%s)}"
	_text="$(cat "${_err}" "${_body}" 2>/dev/null || true)"
	if ! [[ "${_status}" =~ ^[0-9]{3}$ ]]; then
		_status="$(grep -oE 'HTTP [0-9]{3}' "${_err}" 2>/dev/null | head -1 | grep -oE '[0-9]{3}' || true)"
	fi
	_remaining="$(_gh_api_header "${_hdr}" 'x-ratelimit-remaining')"
	_reset="$(_gh_api_header "${_hdr}" 'x-ratelimit-reset')"
	_retry_after="$(_gh_api_header "${_hdr}" 'retry-after')"
	_resource="$(_gh_api_header "${_hdr}" 'x-ratelimit-resource')"
	[[ "${_resource}" =~ ^[a-z_]+$ ]] || _resource="-"
	_wait=-1
	if [[ "${_reset}" =~ ^[0-9]+$ ]]; then
		_wait=$(( 10#${_reset} - _now + 1 ))
		[ "${_wait}" -lt 1 ] && _wait=1
	fi
	if [ -z "${_status}" ] || [ "${_status}" = "403" ] || [ "${_status}" = "429" ]; then
		if [ "${_remaining}" = "0" ] && [ -n "${_status}" ]; then
			printf 'primary %s %s\n' "${_wait}" "${_resource}"
			return 0
		fi
		if [[ "${_retry_after}" =~ ^[0-9]+$ ]] && [ -n "${_status}" ]; then
			printf 'secondary %s %s\n' "$(( 10#${_retry_after} ))" "${_resource}"
			return 0
		fi
		if printf '%s' "${_text}" | grep -qiE 'secondary rate|abuse detection'; then
			printf 'secondary 60 %s\n' "${_resource}"
			return 0
		fi
		if printf '%s' "${_text}" | grep -qiE 'rate limit'; then
			printf 'primary %s %s\n' "${_wait}" "${_resource}"
			return 0
		fi
		if [ "${_status}" = "429" ]; then
			printf 'secondary 60 %s\n' "${_resource}"
			return 0
		fi
		if [ "${_status}" = "403" ] || _is_gh_permanent_failure "${_text}"; then
			printf 'permanent 0 %s\n' "${_resource}"
			return 0
		fi
		printf 'transient 0 %s\n' "${_resource}"
		return 0
	fi
	case "${_status}" in
		408|5[0-9][0-9]) printf 'transient 0 %s\n' "${_resource}" ;;
		*) printf 'permanent 0 %s\n' "${_resource}" ;;
	esac
}

# _gh_rate_limit_reset_for_bucket <bucket> — reset epoch from /rate_limit.
_gh_rate_limit_reset_for_bucket()
{
	local _b="${1:-core}"
	[[ "${_b}" =~ ^[a-z_]+$ ]] || _b=core
	gh api rate_limit --jq ".resources.${_b}.reset // empty" 2>/dev/null | tr -d '[:space:]' || true
}

# gh_rate_limit_breaker_active [bucket] — true while a breaker line for the
# bucket (default core) has a reset epoch in the future. The file is not
# trusted for anything but skipping optional work; malformed lines are ignored.
gh_rate_limit_breaker_active()
{
	local _b="${1:-core}"
	[ -r "${_GH_RATE_LIMIT_BREAKER_FILE}" ] || return 1
	awk -v b="${_b}" -v now="$(date +%s)" '
		$1 == b && $2 ~ /^[0-9]+$/ && ($2 + 0) > (now + 0) { found = 1 }
		END { exit(found ? 0 : 1) }
	' "${_GH_RATE_LIMIT_BREAKER_FILE}" 2>/dev/null
}

_gh_api_retry_log()
{
	# $1 outcome $2 kind $3 bucket $4 status $5 attempt $6 max $7 wait $8 endpoint
	echo "GH_API_RETRY outcome=$1 kind=$2 bucket=$3 status=${4:-none} attempt=$5/$6 wait_secs=$7 endpoint=$(_gh_actions_escape "$8")" >&2
}

gh_api_retry()
{
	local _idem=0 _opt=0
	while :; do
		case "${1:-}" in
			--idempotent) _idem=1; shift ;;
			--optional) _opt=1; shift ;;
			*) break ;;
		esac
	done
	[ "${GH_RETRY_IDEMPOTENT:-false}" = "true" ] && _idem=1
	local _max="${GH_RETRY_MAX_ATTEMPTS:-5}" _max_wait="${GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS:-600}"
	local _cap="${GH_RETRY_BACKOFF_CAP_SECS:-120}" _low="${GH_API_RETRY_LOW_BUDGET_REMAINING:-100}"
	[[ "${_max}" =~ ^[1-9][0-9]*$ ]] || _max=5
	[[ "${_max_wait}" =~ ^[0-9]+$ ]] || _max_wait=600
	[[ "${_cap}" =~ ^[0-9]+$ ]] || _cap=120
	[[ "${_low}" =~ ^[0-9]+$ ]] || _low=100
	local _bucket _endpoint _paginate=0 _a
	_bucket="$(_gh_api_bucket "$@")"
	_endpoint="$(_gh_api_endpoint "$@")"
	for _a in "$@"; do [ "${_a}" = "--paginate" ] && _paginate=1; done
	if [ "${_idem}" != "1" ] && _gh_api_args_unsafe_post "$@"; then
		_max=1
	fi
	GH_API_RETRY_LAST_STATUS=""
	GH_API_RETRY_LAST_KIND=""
	if [ "${_opt}" = "1" ] && gh_rate_limit_breaker_active "${_bucket}"; then
		GH_API_RETRY_LAST_KIND="skipped"
		_gh_api_retry_log skipped breaker "${_bucket}" "" 0 "${_max}" 0 "${_endpoint}"
		return 75
	fi
	local _dir
	if ! _dir=$(mktemp -d "${TMPDIR:-/tmp}/gh_api_retry.XXXXXX" 2>/dev/null); then
		echo "::error::gh_api_retry: mktemp failed; aborting without running" >&2
		return 1
	fi
	local _attempt=1 _rc _status _kind _wait _hb _lines _now _reset _remaining _class _eff_bucket _copy_rc
	while [ "${_attempt}" -le "${_max}" ]; do
		: > "${_dir}/headers"
		_rc=0
		if [ "${_paginate}" = "1" ]; then
			gh api "$@" > "${_dir}/body" 2> "${_dir}/err" || _rc=$?
		else
			gh api -i "$@" > "${_dir}/raw" 2> "${_dir}/err" || _rc=$?
			# Split at the FIRST blank line: status line + headers, then body.
			_lines="$(awk '{ sub(/\r$/, "") } $0 == "" { print NR; exit }' "${_dir}/raw" 2>/dev/null || true)"
			if [[ "${_lines}" =~ ^[0-9]+$ ]] && head -1 "${_dir}/raw" | grep -qE '^HTTP/[0-9.]+ [0-9]{3}'; then
				head -n "${_lines}" "${_dir}/raw" | tr -d '\r' > "${_dir}/headers"
				tail -n +"$(( _lines + 1 ))" "${_dir}/raw" > "${_dir}/body"
			else
				cp "${_dir}/raw" "${_dir}/body"
			fi
		fi
		_status="$(sed -n '1s/^HTTP\/[0-9.]* \([0-9][0-9][0-9]\).*/\1/p' "${_dir}/headers" 2>/dev/null || true)"
		GH_API_RETRY_LAST_STATUS="${_status}"
		if [ "${_rc}" -eq 0 ]; then
			GH_API_RETRY_LAST_KIND="ok"
			_copy_rc=0
			cat "${_dir}/body" || _copy_rc=$?
			_remaining="$(_gh_api_header "${_dir}/headers" 'x-ratelimit-remaining')"
			_reset="$(_gh_api_header "${_dir}/headers" 'x-ratelimit-reset')"
			_eff_bucket="$(_gh_api_header "${_dir}/headers" 'x-ratelimit-resource')"
			[[ "${_eff_bucket}" =~ ^[a-z_]+$ ]] || _eff_bucket="${_bucket}"
			if [[ "${_remaining}" =~ ^[0-9]+$ ]] && [[ "${_reset}" =~ ^[0-9]+$ ]] && [ "${_remaining}" -lt "${_low}" ]; then
				_gh_rate_limit_trip_breaker "${_eff_bucket}" "${_reset}" low
			fi
			rm -rf "${_dir}"
			return "${_copy_rc}"
		fi
		_class="$(_gh_api_classify "${_status}" "${_dir}/headers" "${_dir}/err" "${_dir}/body")"
		read -r _kind _wait _hb <<< "${_class}"
		_eff_bucket="${_bucket}"
		if [ -n "${_hb}" ] && [ "${_hb}" != "-" ]; then _eff_bucket="${_hb}"; fi
		GH_API_RETRY_LAST_KIND="${_kind}"
		if [ -z "${GH_API_RETRY_LAST_STATUS}" ]; then
			GH_API_RETRY_LAST_STATUS="$(grep -oE 'HTTP [0-9]{3}' "${_dir}/err" 2>/dev/null | head -1 | grep -oE '[0-9]{3}' || true)"
		fi
		case "${_kind}" in
			permanent)
				_gh_api_retry_log permanent permanent "${_eff_bucket}" "${GH_API_RETRY_LAST_STATUS}" "${_attempt}" "${_max}" 0 "${_endpoint}"
				cat "${_dir}/err" >&2 2>/dev/null || true
				rm -rf "${_dir}"
				return 2
				;;
			primary|secondary)
				_now="$(date +%s)"
				if ! [[ "${_wait}" =~ ^[0-9]+$ ]]; then
					_reset="$(_gh_rate_limit_reset_for_bucket "${_eff_bucket}")"
					if [[ "${_reset}" =~ ^[0-9]+$ ]]; then
						_wait=$(( _reset - _now + 1 ))
						[ "${_wait}" -lt 1 ] && _wait=1
					else
						_wait=60
					fi
				fi
				_gh_ratelimit_tg_alert
				_gh_rate_limit_trip_breaker "${_eff_bucket}" "$(( _now + _wait ))"
				if [ "${_attempt}" -ge "${_max}" ] || [ "${_wait}" -gt "${_max_wait}" ]; then
					_gh_api_retry_log gave_up "${_kind}" "${_eff_bucket}" "${GH_API_RETRY_LAST_STATUS}" "${_attempt}" "${_max}" "${_wait}" "${_endpoint}"
					rm -rf "${_dir}"
					return 75
				fi
				_gh_api_retry_log retry "${_kind}" "${_eff_bucket}" "${GH_API_RETRY_LAST_STATUS}" "${_attempt}" "${_max}" "${_wait}" "${_endpoint}"
				sleep "${_wait}"
				;;
			*)
				if [ "${_attempt}" -ge "${_max}" ]; then
					_gh_api_retry_log gave_up transient "${_eff_bucket}" "${GH_API_RETRY_LAST_STATUS}" "${_attempt}" "${_max}" 0 "${_endpoint}"
					if [ "${_max}" = "1" ] && [ "${_idem}" != "1" ]; then
						echo "::notice::gh_api_retry: single attempt for non-idempotent create: $(_gh_actions_escape "${_endpoint}")" >&2
					fi
					cat "${_dir}/err" >&2 2>/dev/null || true
					rm -rf "${_dir}"
					return 1
				fi
				_wait=$(( 2 * (2 ** (_attempt - 1)) + RANDOM % 3 ))
				[ "${_wait}" -gt "${_cap}" ] && _wait="${_cap}"
				_gh_api_retry_log retry transient "${_eff_bucket}" "${GH_API_RETRY_LAST_STATUS}" "${_attempt}" "${_max}" "${_wait}" "${_endpoint}"
				sleep "${_wait}"
				;;
		esac
		_attempt=$(( _attempt + 1 ))
	done
	rm -rf "${_dir}"
	return 1
}

# ---------------------------------------------------------------
# gh_retry — Execute a gh CLI command with automatic retry.
#
# Rate-limit errors  → wait until X-RateLimit-Reset, retry (up to max_attempts).
# Other failures     → exponential backoff 1 s, 2 s, 4 s, …
#
# Stdout contract: each attempt's stdout is buffered in a temp file
# and only the attempt that succeeds is copied to the caller's stdout.
# `gh api` prints the error response body to stdout when a call fails,
# so without the buffer a retry that succeeded after a rate limit
# handed the caller the error bodies followed by the real response
# (issue #5495: clarify.yml's `gh_retry gh api … > file` then read
# `null`, `null`, `5016`). A failed attempt's stdout is dropped; one
# warning line reports its size, never its content, because callers
# grep gh_retry's stderr to pick a branch. A failure that never
# succeeds writes nothing to stdout and returns 1. If the successful
# attempt's output cannot be delivered (the reader closed the pipe),
# the copy's non-zero status is returned and the command is not re-run.
#
# Usage:
#   gh_retry gh api repos/owner/repo/issues
#   gh_retry gh issue edit 42 --add-label bug
# ---------------------------------------------------------------
gh_retry()
{
	# Q2=A (issue #6634): a POST create makes one attempt unless a leading
	# --idempotent or GH_RETRY_IDEMPOTENT=true marks it safe to repeat.
	local _ghr_idem="${GH_RETRY_IDEMPOTENT:-false}"
	if [ "${1:-}" = "--idempotent" ]; then _ghr_idem=true; shift; fi
	local max_attempts="${GH_RETRY_MAX_ATTEMPTS:-5}"
	if [ "${_ghr_idem}" != "true" ] && _gh_cmd_is_unsafe_post "$@"; then
		max_attempts=1
	fi
	local attempt=1
	local stderr_file stdout_file
	if ! stderr_file=$(mktemp "${TMPDIR:-/tmp}/gh_retry_stderr.XXXXXX" 2>/dev/null); then
		echo "::error::gh_retry: failed to create stderr temp file (mktemp failed); aborting without running: $*" >&2
		return 1
	fi
	if ! stdout_file=$(mktemp "${TMPDIR:-/tmp}/gh_retry_stdout.XXXXXX" 2>/dev/null); then
		echo "::error::gh_retry: failed to create stdout temp file (mktemp failed); aborting without running: $*" >&2
		rm -f "${stderr_file}"
		return 1
	fi

	while [ "${attempt}" -le "${max_attempts}" ]; do
		if "$@" >"${stdout_file}" 2>"${stderr_file}"; then
			local _gh_retry_replay_rc=0
			cat "${stdout_file}" || _gh_retry_replay_rc=$?
			rm -f "${stderr_file}" "${stdout_file}"
			return "${_gh_retry_replay_rc}"
		fi

		if [ -s "${stdout_file}" ]; then
			local _gh_retry_stdout_bytes
			_gh_retry_stdout_bytes=$(wc -c < "${stdout_file}" 2>/dev/null | tr -d '[:space:]' || true)
			echo "::warning::  gh_retry: dropped ${_gh_retry_stdout_bytes:-?} bytes of stdout from failed attempt ${attempt}/${max_attempts}" >&2
		fi

		local stderr_content
		stderr_content=$(cat "${stderr_file}" 2>/dev/null || true)

		if _is_gh_permanent_failure "${stderr_content}"; then
			echo "::warning::gh command failed with non-retryable error (attempt ${attempt}/${max_attempts}); not retrying: $*" >&2
			if [ -n "${stderr_content}" ]; then
				echo "::warning::  stderr: $(_gh_actions_escape "${stderr_content}")" >&2
			fi
			rm -f "${stderr_file}" "${stdout_file}"
			return 1
		fi

		if _is_gh_rate_limit "${stderr_content}"; then
			echo "::warning::GitHub API rate limit hit (attempt ${attempt}/${max_attempts}), waiting for reset…" >&2
			_gh_ratelimit_tg_alert
			_gh_rate_limit_trip_breaker "$(_gh_cmd_bucket "$@")" "$(( $(date +%s) + 60 ))"
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				_gh_rate_limit_wait_for "${stderr_content}" "$@"
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

	if [ "${max_attempts}" = "1" ] && [ "${_ghr_idem}" != "true" ] && _gh_cmd_is_unsafe_post "$@"; then
		echo "::notice::gh_retry: single attempt for non-idempotent create: $*" >&2
	fi
	echo "::error::gh command failed after ${max_attempts} attempts: $*" >&2
	if [ -s "${stderr_file}" ]; then
		cat "${stderr_file}" >&2
	fi
	rm -f "${stderr_file}" "${stdout_file}"
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
	# [--idempotent] <outfile> <command…>. Each attempt is buffered; only a
	# successful attempt's output is written to <outfile>, and after a final
	# failure <outfile> is truncated, so a failed body never stays in it.
	local _ghr_idem="${GH_RETRY_IDEMPOTENT:-false}"
	if [ "${1:-}" = "--idempotent" ]; then _ghr_idem=true; shift; fi
	local outfile="$1"; shift
	local max_attempts="${GH_RETRY_MAX_ATTEMPTS:-5}"
	if [ "${_ghr_idem}" != "true" ] && _gh_cmd_is_unsafe_post "$@"; then
		max_attempts=1
	fi
	local attempt=1
	local stderr_file attempt_file
	if ! stderr_file=$(mktemp "${TMPDIR:-/tmp}/gh_retry_stderr.XXXXXX" 2>/dev/null); then
		echo "::error::gh_retry_to_file: failed to create stderr temp file (mktemp failed); aborting without running: $*" >&2
		return 1
	fi
	if ! attempt_file=$(mktemp "${TMPDIR:-/tmp}/gh_retry_stdout.XXXXXX" 2>/dev/null); then
		echo "::error::gh_retry_to_file: failed to create stdout temp file (mktemp failed); aborting without running: $*" >&2
		rm -f "${stderr_file}"
		return 1
	fi

	while [ "${attempt}" -le "${max_attempts}" ]; do
		if "$@" > "${attempt_file}" 2>"${stderr_file}"; then
			local _ghr_copy_rc=0
			cat "${attempt_file}" > "${outfile}" || _ghr_copy_rc=$?
			rm -f "${stderr_file}" "${attempt_file}"
			return "${_ghr_copy_rc}"
		fi

		local stderr_content
		stderr_content=$(cat "${stderr_file}" 2>/dev/null || true)

		if _is_gh_permanent_failure "${stderr_content}"; then
			echo "::warning::gh command failed with non-retryable error (attempt ${attempt}/${max_attempts}); not retrying: $*" >&2
			if [ -n "${stderr_content}" ]; then
				echo "::warning::  stderr: $(_gh_actions_escape "${stderr_content}")" >&2
			fi
			rm -f "${stderr_file}" "${attempt_file}"
			: > "${outfile}" 2>/dev/null || true
			return 1
		fi

		if _is_gh_rate_limit "${stderr_content}"; then
			echo "::warning::GitHub API rate limit hit (attempt ${attempt}/${max_attempts}), waiting for reset…" >&2
			_gh_ratelimit_tg_alert
			_gh_rate_limit_trip_breaker "$(_gh_cmd_bucket "$@")" "$(( $(date +%s) + 60 ))"
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				_gh_rate_limit_wait_for "${stderr_content}" "$@"
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
	rm -f "${stderr_file}" "${attempt_file}"
	: > "${outfile}" 2>/dev/null || true
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
	local _ghr_idem="${GH_RETRY_IDEMPOTENT:-false}"
	if [ "${1:-}" = "--idempotent" ]; then _ghr_idem=true; shift; fi
	local outfile="$1"; shift
	local max_attempts="${GH_RETRY_MAX_ATTEMPTS:-5}"
	if [ "${_ghr_idem}" != "true" ] && _gh_cmd_is_unsafe_post "$@"; then
		max_attempts=1
	fi
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
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				sleep "${wait_secs}"
			fi
		else
			local stderr_content
			stderr_content=$(cat "${stderr_file}" 2>/dev/null || true)

			if _is_gh_rate_limit "${stderr_content}"; then
				echo "::warning::GitHub API rate limit hit (attempt ${attempt}/${max_attempts}), waiting for reset…" >&2
				_gh_ratelimit_tg_alert
				_gh_rate_limit_trip_breaker "$(_gh_cmd_bucket "$@")" "$(( $(date +%s) + 60 ))"
				if [ "${attempt}" -lt "${max_attempts}" ]; then
					_gh_rate_limit_wait_for "${stderr_content}" "$@"
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
			_gh_rate_limit_trip_breaker "$(_gh_api_header "${header_file}" 'x-ratelimit-resource')" "${_reset_ts}"
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				_sleep_until_reset "${_reset_ts}"
			fi
		else
			local wait_secs=$(( 2 ** (attempt - 1) ))
			echo "::warning::GitHub API curl failed HTTP ${http_code} (attempt ${attempt}/${max_attempts}), retrying in ${wait_secs}s…" >&2
			if [ "${attempt}" -lt "${max_attempts}" ]; then
				sleep "${wait_secs}"
			fi
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
#   "comments": [{"author", "body", "created_at"}],
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
		| jq -c -s 'add // [] | [.[] | {author: .user.login, body: .body, created_at: .created_at}] | sort_by((.created_at // ""), (.author // ""), (.body // ""))' 2>/dev/null \
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
#   "comments": [{"author", "body", "created_at"}],
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
						author { login }
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
							author: (.author.login // null),
							body: (.body // ""),
							created_at: (.createdAt // null)
						}
					]
					| sort_by((.created_at // ""), (.author // ""), (.body // ""))
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
# _autofix_pr_named_review_runs — list the review wrapper runs that were
# dispatched for one PR.
#
# Motivation (issue #4898): the review dispatches now run from the default
# branch (review_autofix.yml's two retrigger steps, the sweep since #4618,
# the poller and merge train since #4701), so their head_branch and head_sha
# are the default branch's and no branch-scoped lookup can see them. The
# review wrappers name every workflow_dispatch run for its PR:
#   internal-review.yml (this repo):  "Internal: AI Review & Autofix [pr:<N>]"
#   ai-review.yml (consumer repos):   "AI Review [pr:<N>]"
# A workflow_dispatch run's name comes from the dispatched ref's workflow
# file, never from PR text, so an exact name match identifies the PR. This
# is the same match as _pr_named_review_dispatch_runs in
# scripts/orchestrate_poll_process.sh, which review_autofix.yml does not
# source; this one uses REST like its two callers below.
#
# Input:
#   $1 pr_number — must match ^[1-9][0-9]*$
#   $2 status    — optional REST status filter (e.g. "completed")
#   $3 since_epoch — optional trusted GitHub run creation time; narrows
#                    the budget listing to this head's push window
#
# Output (stdout): one JSON array of the matching runs:
#   [{id, status, conclusion, created_at, path}].
#
# Return: 0 on success (possibly []); 1 on an invalid PR number (no call),
#   an API error, an empty response, or unparseable JSON. Each caller
#   decides whether a failure fails open or closed.
#
# API calls: one repo metadata read (default branch) and paged GETs for
#   internal-review.yml and ai-review.yml, only after a branch lookup miss.
#   The branch-filtered list cannot contain these default-branch dispatches;
#   an unfiltered first page can omit them behind unrelated workflows. A
#   budget lookup adds a created>= filter from the head's trusted run time
#   so historical review runs cannot exhaust the page budget unnecessarily.
#   The peer lookup leaves time unfiltered so even an old active run counts.
#   A missing wrapper (HTTP 404) contributes no runs; any other read/parse or
#   pagination-cap failure returns 1, so callers retain their own fail-open
#   (peer) / fail-closed (budget) semantics. Input and output are arrays of
#   Actions run records; at most 10 pages per wrapper, no partial output.
# ---------------------------------------------------------------
# Per-shell memo of the default branch for _autofix_pr_named_review_runs
# (issue #6629: read it at most once per run). Both callers below run in the
# step shell, but they call _autofix_pr_named_review_runs inside $(...), so a
# variable set there would be lost. They call
# _autofix_review_default_branch_prime in their own shell first. A failed
# read is memoized too, so the second caller fails the same way without
# another call. The memo is never initialised from the environment.
# Input: none (GITHUB_REPOSITORY). Output: none; always returns 0.
# API calls: one `GET repos/<repo>` per shell and repository.
_AUTOFIX_REVIEW_DEFAULT_BRANCH=""
_AUTOFIX_REVIEW_DEFAULT_BRANCH_STATE=""
_AUTOFIX_REVIEW_DEFAULT_BRANCH_REPO=""
_autofix_review_default_branch_prime()
{
	local prime_value=""
	[ -n "${GITHUB_REPOSITORY:-}" ] || return 0
	if [ "${_AUTOFIX_REVIEW_DEFAULT_BRANCH_REPO:-}" = "${GITHUB_REPOSITORY}" ] \
		&& [ -n "${_AUTOFIX_REVIEW_DEFAULT_BRANCH_STATE:-}" ]; then
		return 0
	fi
	prime_value=$(gh_retry gh api -X GET "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' 2>/dev/null) || prime_value=""
	_AUTOFIX_REVIEW_DEFAULT_BRANCH_REPO="${GITHUB_REPOSITORY}"
	if [[ "${prime_value}" =~ ^[A-Za-z0-9._/-]+$ ]] && [ "${prime_value}" != "null" ]; then
		_AUTOFIX_REVIEW_DEFAULT_BRANCH="${prime_value}"
		_AUTOFIX_REVIEW_DEFAULT_BRANCH_STATE="ok"
	else
		_AUTOFIX_REVIEW_DEFAULT_BRANCH=""
		_AUTOFIX_REVIEW_DEFAULT_BRANCH_STATE="failed"
	fi
	return 0
}

_autofix_pr_named_review_runs()
{
	local pr_number="${1:-}"
	local status_filter="${2:-}"
	local review_since_epoch="${3:-}"

	if ! [[ "${pr_number}" =~ ^[1-9][0-9]*$ ]] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
		return 1
	fi
	local -a review_status_args=()
	if [ -n "${status_filter}" ]; then
		review_status_args=(-f "status=${status_filter}")
	fi
	local -a review_created_args=()
	if [ -n "${review_since_epoch}" ]; then
		[[ "${review_since_epoch}" =~ ^[0-9]+$ ]] || return 1
		local review_since_iso
		review_since_iso=$(jq -nr --argjson t "${review_since_epoch}" '$t | todate' 2>/dev/null) || return 1
		[[ "${review_since_iso}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || return 1
		review_created_args=(-f "created=>=${review_since_iso}")
	fi

	# Neither branch-filtered call can provide the repository's default
	# branch. Read it once here instead of trusting a caller-supplied ref.
	# The two callers prime a per-shell memo first (issue #6629: at most one
	# read per run); without a memo for this repository, read it here.
	local review_default_branch
	if [ "${_AUTOFIX_REVIEW_DEFAULT_BRANCH_REPO:-}" = "${GITHUB_REPOSITORY}" ] \
		&& [ "${_AUTOFIX_REVIEW_DEFAULT_BRANCH_STATE:-}" = "failed" ]; then
		return 1
	elif [ "${_AUTOFIX_REVIEW_DEFAULT_BRANCH_REPO:-}" = "${GITHUB_REPOSITORY}" ] \
		&& [ "${_AUTOFIX_REVIEW_DEFAULT_BRANCH_STATE:-}" = "ok" ]; then
		review_default_branch="${_AUTOFIX_REVIEW_DEFAULT_BRANCH:-}"
	else
		review_default_branch=$(gh_retry gh api -X GET "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' 2>/dev/null) || return 1
	fi
	[[ "${review_default_branch}" =~ ^[A-Za-z0-9._/-]+$ ]] || return 1
	# A response without .default_branch prints the literal "null", which
	# passes the regex; fail closed like the poller's resolver (issue #6629).
	[ "${review_default_branch}" != "null" ] || return 1
	local review_wrapper review_page review_response review_total review_count review_page_count
	local review_error_file review_all='[]' review_wrapper_runs
	review_error_file=$(mktemp "${TMPDIR:-/tmp}/autofix_pr_named_runs.XXXXXX") || return 1
	for review_wrapper in internal-review.yml ai-review.yml; do
		review_page=1
		review_wrapper_runs='[]'
		while :; do
			[ "${review_page}" -le 10 ] || { rm -f "${review_error_file}"; return 1; }
			# -X GET is required: -f parameters otherwise make gh api POST.
			if ! review_response=$(gh_retry gh api -X GET \
				-H "Accept: application/vnd.github+json" \
				"/repos/${GITHUB_REPOSITORY}/actions/workflows/${review_wrapper}/runs" \
				-f "event=workflow_dispatch" -f "per_page=100" -f "page=${review_page}" \
				"${review_status_args[@]}" "${review_created_args[@]}" 2>"${review_error_file}"); then
				if [ "${review_page}" -eq 1 ] && grep -q 'HTTP 404' "${review_error_file}"; then
					break
				fi
				rm -f "${review_error_file}"
				return 1
			fi
			if ! printf '%s' "${review_response}" | jq -es \
				'length == 1 and (.[0] | (.total_count | type == "number" and . >= 0 and . == floor) and (.workflow_runs | type == "array" and length <= 100 and all(.[]; type == "object" and (.id | type == "number"))))' >/dev/null 2>&1; then
				rm -f "${review_error_file}"
				return 1
			fi
			review_total=$(printf '%s' "${review_response}" | jq -r '.total_count')
			review_page_count=$(printf '%s' "${review_response}" | jq -r '.workflow_runs | length')
			# Append only after a complete page. Never let a partial listing
			# suppress a needed review or open an unbounded retry loop.
			review_wrapper_runs=$(printf '%s\n%s\n' "${review_wrapper_runs}" "${review_response}" \
				| jq -cs '.[0] + .[1].workflow_runs | unique_by(.id)') || { rm -f "${review_error_file}"; return 1; }
			review_count=$(printf '%s' "${review_wrapper_runs}" | jq -r 'length')
			if [ "${review_count}" -gt "${review_total}" ]; then
				rm -f "${review_error_file}"
				return 1
			fi
			if [ "${review_count}" -ge "${review_total}" ]; then
				break
			fi
			if [ "${review_page_count}" -lt 100 ]; then
				rm -f "${review_error_file}"
				return 1
			fi
			review_page=$(( review_page + 1 ))
		done
		review_all=$(printf '%s\n%s\n' "${review_all}" "${review_wrapper_runs}" | jq -cs '.[0] + .[1]') \
			|| { rm -f "${review_error_file}"; return 1; }
	done
	rm -f "${review_error_file}"
	printf '%s' "${review_all}" | jq -c --arg pr "${pr_number}" --arg branch "${review_default_branch}" --arg repo "${GITHUB_REPOSITORY}" '
		# The wrapper path, with an "@<ref>" suffix and a leading
		# "<this repo>/" prefix removed (same rule as the poller, issue #6629).
		def review_path:
			((.path // "") | if type == "string" then . else "" end | sub("@.*$"; "")) as $p
			| if ($p | ascii_downcase | startswith(($repo | ascii_downcase) + "/"))
				then $p[(($repo | length) + 1):]
				else $p
				end;
		[.[]
		 | select(type == "object")
		 # A null/empty head_branch is accepted (issue #6629, Q2): GitHub can
		 # report one on a real default-branch dispatch (#4928), while a
		 # branch-copy spoof always reports its own non-empty branch.
		 | select(.event == "workflow_dispatch" and (((.head_branch // "") == "") or .head_branch == $branch))
		 | select((review_path == ".github/workflows/internal-review.yml" and .display_title == ("Internal: AI Review & Autofix [pr:" + $pr + "]"))
		     or (review_path == ".github/workflows/ai-review.yml" and .display_title == ("AI Review [pr:" + $pr + "]")))
		 | {id, status, conclusion, created_at, path}]
	' 2>/dev/null
}

# ---------------------------------------------------------------
# autofix_retrigger_has_inflight_peer — detect an already-queued or
# already-running peer review/autofix run on the same PR head branch,
# so the caller can skip an otherwise-redundant workflow_dispatch.
#
# Motivation:
#   review_autofix.yml finishes a commit-and-push cycle and then
#   dispatches the next review run from the default branch (the
#   PR-named wrapper first, issue #4898) as a fallback in case the
#   merge-ref for the synchronize event is unbuildable.
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
#   $1 pr_number      — PR number: used in log lines, and must match
#                       ^[1-9][0-9]*$ for the PR-named lookup below
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
# PR-named runs (issue #4898):
#   Review runs dispatched from the default branch (both retrigger steps,
#   the sweep, the poller, the merge train) have the default branch as
#   head_branch, so the branch lookup cannot see them. When it finds no
#   peer and $1 is a valid PR number, the helper also counts queued or
#   running workflow_dispatch runs named for the PR
#   (_autofix_pr_named_review_runs), excluding the current run. The
#   AUTOFIX_PEER_CHECK line keeps its exact field set (a pinned log
#   contract); a PR-named peer shows up in peer_run / peer_path.
#
# API calls:
#   1 `gh api GET /repos/{repo}/actions/runs` call per invocation,
#   wrapped in gh_retry (rate-limit aware), plus 1 PR-named
#   `?event=workflow_dispatch` call only when the branch lookup found no
#   peer.  Results are not cached — the two retrigger blocks fire at
#   most twice per run and the set of in-flight runs is mutable between
#   those calls.
#
# CLAUDE.md §15 audit:
#   The prior retrigger path dispatched unconditionally, so there is
#   no existing gh call here to extend.  A single list-runs call
#   replaces the wasted dispatch on the collision path, so net API
#   cost is negative when a peer is found and neutral otherwise.  The
#   branch-filtered call cannot return default-branch runs, and widening
#   it to an unfiltered page would drop older same-branch runs, so the
#   PR-named lookup is a second call on the no-peer path only.
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
	if ! printf '%s' "${response}" | jq -es 'length == 1 and (.[0].workflow_runs | type == "array")' >/dev/null 2>&1; then
		echo "AUTOFIX_PEER_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=jq_error" >&2
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
			| select(.status == "queued" or .status == "pending" or .status == "in_progress")
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
	if ! [ "${peer_count:-0}" -gt 0 ] 2>/dev/null && [[ "${pr_number}" =~ ^[1-9][0-9]*$ ]]; then
		# Default-branch dispatch runs are invisible to the branch lookup
		# above (issue #4898); look for one named for this PR. Fail open.
		local pr_named_runs pr_named_info
		declare -F _autofix_review_default_branch_prime >/dev/null && _autofix_review_default_branch_prime
		if pr_named_runs=$(_autofix_pr_named_review_runs "${pr_number}") \
			&& pr_named_info=$(printf '%s' "${pr_named_runs}" | jq -r --arg current "${current_run_id}" '
				[
					.[]
					| select(.status == "queued" or .status == "in_progress" or .status == "pending" or .status == "waiting" or .status == "requested")
					| select((.id | tostring) != $current)
				]
				| {count: length, first_id: (.[0].id // "-"), first_path: (.[0].path // "-")}
				| "\(.count) \(.first_id) \(.first_path)"
			' 2>/dev/null); then
			peer_count=$(printf '%s' "${pr_named_info}" | awk '{print $1}')
			peer_run=$(printf '%s' "${pr_named_info}" | awk '{print $2}')
			peer_path=$(printf '%s' "${pr_named_info}" | awk '{print $3}')
		else
			echo "AUTOFIX_PEER_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=pr_named_api_error" >&2
		fi
	fi

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
#   $1 pr_number      — PR number: used in log lines, and must match
#                       ^[1-9][0-9]*$ when the PR-named lookup below runs
#   $2 head_branch    — PR head branch name (required for filtering)
#   $3 current_run_id — github.run_id of the CURRENT run (excluded)
#   $4 head_sha       — the PR head commit this run reviewed (required)
#   $5 head_commit_epoch — optional, accepted and ignored: the head
#                       commit's committer time (`git log -1 --format=%ct`).
#                       The PR author sets it, so it is never used as the
#                       push-time bound (issue #5523); it stays in the
#                       signature so existing callers keep working
#   $6 event_name     — optional: the current run's event
#                       (GITHUB_EVENT_NAME, the caller's event inside a
#                       reusable workflow); see "Unnamed dispatch runs"
#
# PR-named runs (issue #4898):
#   The retry is dispatched from the default branch, so its head_sha is
#   the default branch's and the branch lookup never counts it. When the
#   branch lookup counts nothing, the helper also counts completed,
#   non-cancelled workflow_dispatch runs named for the PR
#   (_autofix_pr_named_review_runs), excluding the current run, that were
#   created at or after the head's push-time bound: the earliest
#   created_at of any run in the branch page whose head_sha is this head
#   (the push's pull_request run, cancelled twins included, and any other
#   workflow the push triggered). GitHub records both fields, so neither
#   can be set by the PR author. The head commit's committer time is not
#   used (issue #5523): a backdated commit would pull earlier heads'
#   reviews into this head's count and suppress its retry. No run on the
#   head, an invalid PR number, or a failed call fails closed.
#
# Unnamed dispatch runs (issue #4898, conformance audit):
#   A retry dispatched from the default branch under a name the PR-named
#   match cannot see (a differently named caller wrapper, review_autofix.yml
#   itself, or an ai-review.yml that predates the "[pr:<N>]" run name) is
#   invisible to both lookups, so the next retry would count nothing and
#   dispatch again without bound once the head's pull_request twin was
#   concurrency-cancelled. When $6 is workflow_dispatch and the current
#   run is not among the PR-named runs, the budget is consumed
#   (reason=unnamed_dispatch_run, fail closed): such a run never
#   dispatches an automated retry. The PR-named page is therefore fetched
#   without a status filter (still one call) and filtered to completed
#   runs locally, so the in-progress current run is in it. An empty $6
#   (a caller that predates it) skips this check; the budget line then
#   shows event=-, so a caller that drops the event is visible in the log.
#
# Output (stdout):
#   AUTOFIX_CHANGES_LOST_BUDGET pr=<n> branch=<b> head_sha=<sha> \
#     current_run=<r> prior_completed=<n> pr_named_completed=<n|-> \
#     event=<$6|->
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
#   1 `gh api GET /repos/{repo}/actions/runs` call per invocation,
#   wrapped in gh_retry (§15: same single branch-scoped list the peer
#   helper issues; the two probes run back-to-back in one step at most
#   once per review run), plus 1 PR-named `?event=workflow_dispatch`
#   call only when the branch lookup counted nothing.
#   The branch-filtered call cannot return default-branch runs, so no
#   existing call could carry the PR-named match.
# ---------------------------------------------------------------
autofix_changes_lost_head_retry_consumed()
{
	local pr_number="${1:-}"
	local head_branch="${2:-}"
	local current_run_id="${3:-}"
	local head_sha="${4:-}"
	# Accepted for caller compatibility and not used (issue #5523).
	local head_commit_epoch="${5:-}"
	local run_event_name="${6:-}"

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
	if ! printf '%s' "${response}" | jq -es 'length == 1 and (.[0].workflow_runs | type == "array")' >/dev/null 2>&1; then
		echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=jq_error" >&2
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

	local pr_named_completed="-"
	if ! [ "${prior_completed:-0}" -gt 0 ] 2>/dev/null; then
		# The retry runs from the default branch (issue #4898), so the
		# branch lookup above never sees it; count the completed runs named
		# for this PR since the head was pushed. Fail closed throughout.
		if ! [[ "${pr_number}" =~ ^[1-9][0-9]*$ ]]; then
			echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=invalid_pr_number" >&2
			return 0
		fi
		# Push-time bound: GitHub-recorded run times on this head only,
		# never the author-set commit time (issue #5523).
		local push_bound
		if ! push_bound=$(printf '%s' "${response}" | jq -r \
			--arg head "${head_sha}" '
			[
				.workflow_runs[]?
				| select((.head_sha // "") == $head)
				| (.created_at // "")
				| (try fromdateiso8601 catch empty)
			]
			| if length == 0 then "" else (min | floor | tostring) end
		' 2>/dev/null) || [ -z "${push_bound}" ]; then
			echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=missing_head_time" >&2
			return 0
		fi
		local pr_named_runs
		declare -F _autofix_review_default_branch_prime >/dev/null && _autofix_review_default_branch_prime
		if ! pr_named_runs=$(_autofix_pr_named_review_runs "${pr_number}" "" "${push_bound}"); then
			echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=pr_named_api_error" >&2
			return 0
		fi
		if [ "${run_event_name}" = "workflow_dispatch" ] \
			&& ! printf '%s' "${pr_named_runs}" | jq -e --arg current "${current_run_id}" \
				'any(.[]; (.id | tostring) == $current)' >/dev/null 2>&1; then
			# This dispatch run is not named for the PR, so a retry it
			# dispatched could never count it (see "Unnamed dispatch runs").
			echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=unnamed_dispatch_run" >&2
			return 0
		fi
		if ! pr_named_completed=$(printf '%s' "${pr_named_runs}" | jq -r \
			--arg current "${current_run_id}" \
			--argjson bound "${push_bound}" '
			[
				.[]
				| select(.status == "completed")
				| select((.conclusion // "") != "cancelled")
				| select((.id | tostring) != $current)
				| select(((.created_at // "") | (try fromdateiso8601 catch -1)) >= $bound)
			]
			| length
		' 2>/dev/null) || ! [[ "${pr_named_completed}" =~ ^[0-9]+$ ]]; then
			echo "AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED pr=${pr_number:-?} branch=${head_branch} reason=pr_named_jq_error" >&2
			return 0
		fi
		prior_completed=$(( ${prior_completed:-0} + pr_named_completed ))
	fi

	echo "AUTOFIX_CHANGES_LOST_BUDGET pr=${pr_number:-?} branch=${head_branch} head_sha=${head_sha} current_run=${current_run_id} prior_completed=${prior_completed:-0} pr_named_completed=${pr_named_completed} event=${run_event_name:--}"

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
#      within a 272k-token context window (the former gpt-5.5
#      capacity-fallback floor) once the static prefix (system
#      instructions / agents / pipeline doc, ~10k tokens) and the
#      response budget (~30k tokens) are subtracted.  The current
#      primary (gpt-6-sol) and fallback (gpt-5.6-sol) both have 1.05M
#      windows, so this is headroom.  After the budget is exhausted
#      later files are replaced with an explicit "(omitted — budget exhausted)"
#      marker so the model knows context is incomplete.
#
# The budget tracker uses a state file under $TMPDIR so it survives
# subshell invocations from $(...) command-substitution inside a
# heredoc.  Callers should run `_init_prompt_budget` before assembling
# the prompt and `_cleanup_prompt_budget` after the prompt is written.
# ---------------------------------------------------------------

# Default total prompt budget (bytes).  Set the env var BEFORE sourcing
# gh_helpers.sh to override.  See header comment above for the sizing
# rationale (200k tokens within a 272k context window after static prefix
# + response budget; the gpt-6-sol / gpt-5.6-sol defaults have 1.05M).
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
		if PYTHONSAFEPATH=1 python3 - "${_path}" "${_tmp}" <<'PY' 2>/dev/null
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
#   https://github.com/owner/repo/issues/78#issuecomment-1
#     (any URL/path followed by a `#fragment` points at a comment or
#     event on the issue, not at the issue as the PR's subject;
#     #5776 / PR #5649 relabelled an issue `ai:merged` from such a link)
#   https://github.com/owner/repo/issues/78?view=plain#issuecomment-1
#     (a `?query` after the number is not a boundary either, so a query
#     string cannot hide a following `#fragment`; a bare URL with only a
#     query string is conservatively not counted as a link)
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
		| grep -oiE "((^|[^[:alnum:]_])github\\.com/${_repository_escaped}/issues/[0-9]+([^[:alnum:]_#?]|$)|(^|[^[:alnum:]_])${_repository_escaped}/issues/[0-9]+([^[:alnum:]_#?]|$)|(^|[^[:alnum:]_/-])(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)[[:space:]]+#[[:space:]]*[0-9]+([^[:alnum:]_]|$))" \
		| sed -nE 's/.*[^0-9]([0-9]+)[^0-9]*$/\1/p' \
		| sort -un || true
}
