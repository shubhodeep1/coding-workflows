#!/usr/bin/env bash
# tg_helpers.sh — Telegram message tracking & cleanup helpers.
#
# Provides tracked notification functions that store Telegram message IDs
# on GitHub issue comments, enabling batch deletion when an issue/PR
# reaches a terminal state.
#
# Required env vars (best-effort; functions degrade gracefully):
#   TG_BOT_SECRET        — Telegram bot token
#   TG_ADMIN_CHAT_ID     — Telegram chat ID (fallback: TG_CHAT_ID)
#   GH_TOKEN             — GitHub PAT for issue comment API
#   GITHUB_REPOSITORY    — owner/repo

# Guard against double-sourcing
if [ "${_TG_HELPERS_LOADED:-}" = "true" ]; then
	return 0 2>/dev/null || true
fi
_TG_HELPERS_LOADED="true"

# Source rate-limit helpers (provides curl_gh_api)
# shellcheck source=gh_helpers.sh
if [ -f "scripts/gh_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source scripts/gh_helpers.sh
fi
# Fallback: if curl_gh_api is not available, pass through to plain curl
if ! type curl_gh_api >/dev/null 2>&1; then
	curl_gh_api() { curl "$@"; }
fi

# Resolve chat ID from available env vars
_tg_chat_id()
{
	printf '%s' "${TG_ADMIN_CHAT_ID:-${TG_CHAT_ID:-}}"
}

# ---------------------------------------------------------------
# Alert level support
# Levels: DEBUG < WARNING < ERROR < CRITICAL < SILENT
# ALERT_MSG_LEVEL env var controls minimum level sent (default: WARNING).
# Level convention: DEBUG means the pipeline is already handling the
# event (a dispatch, retry, re-issue, recovery step or success ping);
# WARNING and above mean a human has to act. With the default WARNING
# threshold only events that need a person reach Telegram; set
# ALERT_MSG_LEVEL=DEBUG to receive the full stream again.
# New alerts default to CRITICAL until explicitly recategorised.
# Setting ALERT_MSG_LEVEL=SILENT suppresses all existing helper-based
# sends because no existing call site emits at SILENT — every caller
# below uses CRITICAL (default), ERROR, WARNING, or DEBUG, and all of
# those map to a number < 99. (Used by the test-and-mark-stable
# release gate to silence its smoke-test pipeline; only the gate's
# final pass/fail notification is emitted via raw curl, bypassing
# this filter. A future caller wishing to bypass SILENT could pass
# `SILENT` itself as the level — the threshold check would let it
# through — but no such call exists today.)
# ---------------------------------------------------------------

_tg_level_num()
{
	case "$(printf '%s' "${1:-}" | tr '[:lower:]' '[:upper:]')" in
		DEBUG)    printf '0' ;;
		WARNING)  printf '1' ;;
		ERROR)    printf '2' ;;
		CRITICAL) printf '3' ;;
		SILENT)   printf '99' ;;
		*)        printf '0' ;;
	esac
}

_tg_level_icon()
{
	case "$(printf '%s' "${1:-}" | tr '[:lower:]' '[:upper:]')" in
		DEBUG)    printf '🔍' ;;
		WARNING)  printf '⚠️' ;;
		ERROR)    printf '❌' ;;
		CRITICAL) printf '🚨' ;;
		SILENT)   printf '🔕' ;;
		*)        printf '🚨' ;;
	esac
}

# _tg_should_send — returns 0 (true) if the message level meets the threshold.
_tg_should_send()
{
	local msg_level="$1"
	local threshold="${ALERT_MSG_LEVEL:-WARNING}"
	local msg_num threshold_num
	msg_num=$(_tg_level_num "${msg_level}")
	threshold_num=$(_tg_level_num "${threshold}")
	[ "${msg_num}" -ge "${threshold_num}" ]
}

# _tg_prepend_level — prepend "ICON LEVEL: " to a message.
_tg_prepend_level()
{
	local level="$1" msg="$2"
	local icon level_upper
	icon=$(_tg_level_icon "${level}")
	level_upper=$(printf '%s' "${level}" | tr '[:lower:]' '[:upper:]')
	printf '%s %s: %s' "${icon}" "${level_upper}" "${msg}"
}

# ---------------------------------------------------------------
# tg_send_msg — Send a Telegram message, echo the message_id.
# Usage: msg_id=$(tg_send_msg "Hello world" "ERROR")
# Level defaults to CRITICAL so new/untagged alerts are always sent.
# ---------------------------------------------------------------
tg_send_msg()
{
	local msg="$1"
	local level="${2:-CRITICAL}"
	# Filter by ALERT_MSG_LEVEL threshold
	if ! _tg_should_send "${level}"; then
		return 0
	fi
	# Prepend level prefix
	msg="$(_tg_prepend_level "${level}" "${msg}")"
	local chat_id
	chat_id="$(_tg_chat_id)"
	if [ -z "${TG_BOT_SECRET:-}" ] || [ -z "${chat_id}" ]; then
		return 0
	fi
	local resp
	resp=$(curl -s -X POST "https://api.telegram.org/bot${TG_BOT_SECRET}/sendMessage" \
		-d "chat_id=${chat_id}" \
		-d "disable_web_page_preview=true" \
		--data-urlencode "text=${msg}" 2>/dev/null) || {
		echo "::warning::Telegram send failed" >&2
		return 0
	}
	local msg_id
	msg_id=$(printf '%s' "${resp}" | jq -r '.result.message_id // empty' 2>/dev/null)
	printf '%s' "${msg_id:-}"
}

# ---------------------------------------------------------------
# tg_send_report_msg — Send a scheduled report the operator asked for
# (e.g. the daily Claude pool usage message), echo the message_id.
# Unlike tg_send_msg it adds no level prefix and ignores the
# ALERT_MSG_LEVEL threshold, except that ALERT_MSG_LEVEL=SILENT still
# suppresses it. The caller supplies its own label (e.g. "📊 ").
# Usage: msg_id=$(tg_send_report_msg "📊 Daily report")
# ---------------------------------------------------------------
tg_send_report_msg()
{
	local msg="$1"
	if [ "$(printf '%s' "${ALERT_MSG_LEVEL:-}" | tr '[:lower:]' '[:upper:]')" = "SILENT" ]; then
		return 0
	fi
	local chat_id
	chat_id="$(_tg_chat_id)"
	if [ -z "${TG_BOT_SECRET:-}" ] || [ -z "${chat_id}" ]; then
		return 0
	fi
	local resp
	resp=$(curl -s --max-time 30 -X POST "https://api.telegram.org/bot${TG_BOT_SECRET}/sendMessage" \
		-d "chat_id=${chat_id}" \
		-d "disable_web_page_preview=true" \
		--data-urlencode "text=${msg}" 2>/dev/null) || {
		echo "::warning::Telegram send failed" >&2
		return 0
	}
	local msg_id
	if ! msg_id=$(printf '%s' "${resp}" | jq -r '.result.message_id // empty' 2>/dev/null); then
		echo "::warning::Telegram response could not be parsed" >&2
		msg_id=""
	elif [ -z "${msg_id}" ]; then
		# Telegram's own error text (never contains the bot token).
		echo "::warning::Telegram returned no message id: $(printf '%s' "${resp}" | jq -r '.description // "unknown"' 2>/dev/null | tr -cd '[:print:]' | cut -c1-200)" >&2
	fi
	printf '%s' "${msg_id:-}"
}

# ---------------------------------------------------------------
# tg_delete_msg — Delete a single Telegram message by ID.
# Usage: tg_delete_msg 12345
# ---------------------------------------------------------------
tg_delete_msg()
{
	local msg_id="$1"
	local chat_id
	chat_id="$(_tg_chat_id)"
	if [ -z "${TG_BOT_SECRET:-}" ] || [ -z "${chat_id}" ] || [ -z "${msg_id}" ]; then
		return 0
	fi
	# Exit status stays 0 for compatibility; the checked helper bounds the
	# call to 10 seconds (the former inline curl had no timeout).
	_tg_delete_msg_checked "${msg_id}" || true
	return 0
}

# ---------------------------------------------------------------
# _tg_delete_msg_checked — Delete one Telegram message and classify
# the outcome so cleanup can keep a tracking comment on transient
# failures (BUG-006).
# Returns 0 when no retry is needed: deleted (HTTP 2xx, .ok == true),
# already gone or permanent (HTTP 400, or any other .ok == false that
# is not 401/403/429/5xx, e.g. "message to delete not found").
# Returns 1 when the failure is transient: curl transport failure,
# HTTP 401 or 403 (bot token or chat permission problem, retried once
# the credential is fixed), HTTP 429 or 5xx, or a missing / unparsable
# response.
# Prints nothing; never logs the URL, token or response body.
# Usage: if _tg_delete_msg_checked 12345; then ...; fi
# ---------------------------------------------------------------
_tg_delete_msg_checked()
{
	# Callers run this inside IFS=',' loops; reset IFS for our own splitting.
	local IFS=$' \t\n'
	local msg_id="$1"
	local chat_id
	chat_id="$(_tg_chat_id)"
	if [ -z "${TG_BOT_SECRET:-}" ] || [ -z "${chat_id}" ] || [ -z "${msg_id}" ]; then
		return 0
	fi
	local resp="" rc=0 http_code="" body="" ok=""
	resp=$(curl -s --max-time 10 -w '\n%{http_code}' -X POST "https://api.telegram.org/bot${TG_BOT_SECRET}/deleteMessage" \
		-d "chat_id=${chat_id}" \
		-d "message_id=${msg_id}" 2>/dev/null) || rc=$?
	if [ "${rc}" -ne 0 ]; then
		return 1
	fi
	case "${resp}" in
		*$'\n'*)
			body="${resp%$'\n'*}"
			http_code="${resp##*$'\n'}"
			;;
		*)
			http_code="${resp}"
			;;
	esac
	if ! [[ "${http_code}" =~ ^[0-9][0-9][0-9]$ ]] || [ "${http_code}" = "000" ]; then
		return 1
	fi
	if [ "${http_code}" = "429" ] || [ "${http_code}" = "401" ] || [ "${http_code}" = "403" ] || [ "${http_code:0:1}" = "5" ]; then
		return 1
	fi
	if [ "${http_code}" = "400" ]; then
		return 0
	fi
	if ! ok=$(printf '%s' "${body}" | jq -r 'if type=="object" and (.ok|type)=="boolean" then (.ok|tostring) else "missing" end' 2>/dev/null); then
		return 1
	fi
	if [ "${ok}" = "true" ] && [ "${http_code:0:1}" = "2" ]; then
		return 0
	fi
	if [ "${ok}" = "false" ]; then
		return 0
	fi
	return 1
}

# ---------------------------------------------------------------
# Tracking-comment trust (#8598). Anyone who can comment on an issue can
# write text that looks like a tracking marker, so a tracking comment is
# only acted on (Telegram delete, comment DELETE or PATCH) when:
#   - its author is the identity behind GH_TOKEN (case-insensitive login),
#     or, only when that identity cannot be resolved (installation tokens
#     get 403 from /user), github-actions[bot] with user.type == "Bot"; and
#   - its whole body is exactly one marker. The patterns use \A and \z
#     because Oniguruma's ^ and $ also match at line breaks, which would let
#     a multi-line body smuggle a forged record past the check.
# ---------------------------------------------------------------
_TG_TRACKING_JQ_DEFS='
def tg_trusted($login):
	type == "object"
	and (.user | type) == "object"
	and (.user.login | type) == "string"
	and (.id | type) == "number"
	and (.body | type) == "string"
	and (if $login == "github-actions[bot]"
		then .user.login == "github-actions[bot]" and .user.type == "Bot"
		else (.user.login | ascii_downcase) == $login
		end);
def tg_marker_ids($re):
	.body | capture($re) | .ids;
'
_TG_TRACKING_ANY_RE='\A<!-- tg_(?:cleanup|phase:[a-z_]+):(?<ids>[0-9]+(?:,[0-9]+)*) -->\z'
_TG_TRACKING_CLEANUP_RE='\A<!-- tg_cleanup:(?<ids>[0-9]+(?:,[0-9]+)*) -->\z'

# _tg_tracking_phase_re PHASE — print the exact-body pattern for one phase.
# Callers validate PHASE against ^[a-z_]+$ first.
_tg_tracking_phase_re()
{
	printf '%s' '\A<!-- tg_phase:'"$1"':(?<ids>[0-9]+(?:,[0-9]+)*) -->\z'
}

# _tg_valid_phase PHASE — 0 when PHASE is safe to splice into a pattern.
_tg_valid_phase()
{
	[[ "${1:-}" =~ ^[a-z_]+$ ]]
}

# ---------------------------------------------------------------
# _tg_tracking_author_login — Resolve the login trusted to have written
# tracking comments and store it in _TG_TRACKING_AUTHOR_CURRENT (no output;
# call it in the current shell, not inside $(...), so the cache survives).
# A resolved login is lowercased and cached in _TG_TRACKING_AUTHOR_LOGIN
# for the rest of the process. On any failure (transport, 403 for an
# installation token, non-JSON, missing or malformed login) the result is
# "github-actions[bot]", the fallback is cached in
# _TG_TRACKING_AUTHOR_FALLBACK (later calls skip the lookup) and one
# ::warning:: is printed. Never logs the token or the response body.
# Usage: _tg_tracking_author_login; login="${_TG_TRACKING_AUTHOR_CURRENT}"
# ---------------------------------------------------------------
_tg_tracking_author_login()
{
	if [ -n "${_TG_TRACKING_AUTHOR_LOGIN:-}" ]; then
		_TG_TRACKING_AUTHOR_CURRENT="${_TG_TRACKING_AUTHOR_LOGIN}"
		return 0
	fi
	if [ "${_TG_TRACKING_AUTHOR_FALLBACK:-}" = "1" ]; then
		_TG_TRACKING_AUTHOR_CURRENT="github-actions[bot]"
		return 0
	fi
	local user_json="" login="" attempts="${GH_RETRY_MAX_ATTEMPTS:-5}"
	# A 403 from /user is permanent for installation tokens; keep the
	# fallback quick by allowing at most 2 attempts.
	[[ "${attempts}" =~ ^[0-9]+$ ]] || attempts=5
	[ "${attempts}" -le 2 ] || attempts=2
	# API audit (§14): the comment-page reads in this file do not return the
	# caller's identity and no cached login exists here, so this is one GET
	# per process after the first success.
	if user_json=$(GH_RETRY_MAX_ATTEMPTS="${attempts}" curl_gh_api -s --max-time 30 \
		-H "Authorization: token ${GH_TOKEN:-}" \
		-H "Accept: application/vnd.github.v3+json" \
		"https://api.github.com/user" 2>/dev/null); then
		login=$(printf '%s' "${user_json}" | jq -r 'if type == "object" and (.login | type) == "string" then .login else empty end' 2>/dev/null) || login=""
	fi
	if [[ "${login}" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}$ ]]; then
		_TG_TRACKING_AUTHOR_LOGIN="$(printf '%s' "${login}" | tr '[:upper:]' '[:lower:]')"
		_TG_TRACKING_AUTHOR_CURRENT="${_TG_TRACKING_AUTHOR_LOGIN}"
		return 0
	fi
	echo "::warning::Telegram tracking: could not resolve the GH_TOKEN identity; only github-actions[bot] tracking comments are trusted" >&2
	_TG_TRACKING_AUTHOR_FALLBACK=1
	_TG_TRACKING_AUTHOR_CURRENT="github-actions[bot]"
	return 0
}

# ---------------------------------------------------------------
# _tg_snapshot_tracking_entries — Read EVERY comments page first and
# print the matching tracking comments as "comment_id<TAB>msg_id_list"
# lines, so the cleanup functions never paginate while deleting (BUG-003:
# deleting page-1 comments shifted page-2 comments up and they were skipped).
# A comment matches only when AUTHOR_LOGIN (see _tg_tracking_author_login)
# wrote it and its whole body is one marker (#8598): with a phase,
# exactly <!-- tg_phase:PHASE:ids -->; without one, exactly
# <!-- tg_cleanup:ids --> or <!-- tg_phase:any:ids -->. Records carry only
# the numeric comment id and the captured digits-and-commas ID list, never
# the body, so no record can span lines.
# Returns 1 (after a ::warning::) when AUTHOR_LOGIN is empty, the phase is
# invalid, any page read fails, a page is not a JSON array, or more than
# 100 pages are seen; callers then delete nothing. API calls: one GET per
# 100 comments.
# Usage: snapshot=$(_tg_snapshot_tracking_entries 42 clarify pipeline-bot) || return 0
# ---------------------------------------------------------------
_tg_snapshot_tracking_entries()
{
	local issue_num="$1" phase="${2:-}" author_login="${3:-}"
	if [ -z "${author_login}" ]; then
		echo "::warning::No trusted tracking-comment author for cleanup (issue #${issue_num}); aborting cleanup" >&2
		return 1
	fi
	local marker_re="${_TG_TRACKING_ANY_RE}"
	if [ -n "${phase}" ]; then
		if ! _tg_valid_phase "${phase}"; then
			echo "::warning::Invalid Telegram tracking phase for cleanup (issue #${issue_num}); aborting cleanup" >&2
			return 1
		fi
		marker_re="$(_tg_tracking_phase_re "${phase}")"
	fi
	local api_base="https://api.github.com/repos/${GITHUB_REPOSITORY}/issues"
	local page=1 max_pages=100 entries="" ctx comments_json count page_entries
	while true; do
		if [ -n "${phase}" ]; then
			ctx="phase cleanup (issue #${issue_num}, phase ${phase}, page ${page})"
		else
			ctx="cleanup (issue #${issue_num}, page ${page})"
		fi
		if [ "${page}" -gt "${max_pages}" ]; then
			echo "::warning::Issue comments for ${ctx} exceed ${max_pages} pages; aborting cleanup" >&2
			return 1
		fi
		# Bound each attempt so a stalled read cannot hold cleanup until the
		# job timeout (curl_gh_api passes extra args through to curl).
		comments_json=$(curl_gh_api -s --max-time 30 \
			-H "Authorization: token ${GH_TOKEN}" \
			-H "Accept: application/vnd.github.v3+json" \
			"${api_base}/${issue_num}/comments?per_page=100&page=${page}") || {
			echo "::warning::Failed to fetch issue comments for ${ctx}; aborting cleanup" >&2
			return 1
		}
		count=$(printf '%s' "${comments_json}" | jq 'if type=="array" then length else error("not an array") end' 2>/dev/null) || count=""
		if ! [[ "${count}" =~ ^[0-9]+$ ]]; then
			echo "::warning::Unparsable issue comments page for ${ctx}; aborting cleanup" >&2
			return 1
		fi
		if [ "${count}" -eq 0 ]; then
			break
		fi
		page_entries=$(printf '%s' "${comments_json}" | jq -r \
			--arg login "${author_login}" --arg re "${marker_re}" \
			"${_TG_TRACKING_JQ_DEFS}"'.[] | select(tg_trusted($login)) | tg_marker_ids($re) as $ids | "\(.id)\t\($ids)"' 2>/dev/null) || page_entries="__tg_snapshot_failed__"
		if [ "${page_entries}" = "__tg_snapshot_failed__" ]; then
			echo "::warning::Unparsable issue comments page for ${ctx}; aborting cleanup" >&2
			return 1
		fi
		if [ -n "${page_entries}" ]; then
			entries+="${page_entries}"$'\n'
		fi
		if [ "${count}" -lt 100 ]; then
			break
		fi
		page=$((page + 1))
	done
	printf '%s' "${entries}"
	return 0
}

# ---------------------------------------------------------------
# _tg_newest_comments_json — Print the issue's newest page of comments
# (up to 100), oldest first within the page.
# The issue-comments endpoint has no `direction` parameter: the former
# `?per_page=30&direction=desc` returned the OLDEST 30 comments, so on a
# busy tracking issue the marker was never found and every message posted
# a new one (447 `tg_cleanup` comments on tele-funtoken-msg-scoring#3928).
# API calls: 2 (the issue read for its comment count, then the last page).
# The existing comments read could not be reused: it never returned the
# newest page. Fails (status 1) when either read fails; callers then
# create a new marker as before.
# Usage: _tg_newest_comments_json 42
# ---------------------------------------------------------------
_tg_newest_comments_json()
{
	local issue_num="$1" api_base="https://api.github.com/repos/${GITHUB_REPOSITORY}/issues" issue_json total last_page
	issue_json=$(curl_gh_api -s \
		-H "Authorization: token ${GH_TOKEN}" \
		-H "Accept: application/vnd.github.v3+json" \
		"${api_base}/${issue_num}") || return 1
	total=$(printf '%s' "${issue_json}" | jq -r '.comments // empty' 2>/dev/null) || return 1
	[[ "${total}" =~ ^[0-9]+$ ]] || return 1
	last_page=$(( total > 0 ? (total + 99) / 100 : 1 ))
	curl_gh_api -s \
		-H "Authorization: token ${GH_TOKEN}" \
		-H "Accept: application/vnd.github.v3+json" \
		"${api_base}/${issue_num}/comments?per_page=100&page=${last_page}"
}

# ---------------------------------------------------------------
# tg_store_msg_id — Store a message ID on a GitHub issue comment.
# Appends to an existing tracking comment if one exists, or
# creates a new one.  Comment body pattern:
#   <!-- tg_cleanup:id1,id2,id3 -->
# Usage: tg_store_msg_id 42 12345
# ---------------------------------------------------------------
tg_store_msg_id()
{
	local issue_num="$1" msg_id="$2"
	if [ -z "${msg_id}" ] || [ -z "${issue_num}" ] || [ "${issue_num}" = "0" ]; then
		return 0
	fi
	if [ -z "${GH_TOKEN:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
		return 0
	fi

	local api_base="https://api.github.com/repos/${GITHUB_REPOSITORY}/issues"

	# Look for an existing tracking comment among the newest 100 comments
	local comments_json
	comments_json=$(_tg_newest_comments_json "${issue_num}") || {
		echo "::warning::Failed to fetch existing tracking comments for issue #${issue_num}; creating a new tracking comment" >&2
		# Fallback: create a new tracking comment
		curl -s -X POST \
			-H "Authorization: token ${GH_TOKEN}" \
			-H "Accept: application/vnd.github.v3+json" \
			"${api_base}/${issue_num}/comments" \
			-d "$(jq -n --arg b "<!-- tg_cleanup:${msg_id} -->" '{body: $b}')" >/dev/null 2>&1 || true
		return 0
	}

	# Only the trusted author's exact marker is appended to (#8598); a
	# look-alike from anyone else is never PATCHed.
	_tg_tracking_author_login
	local author_login="${_TG_TRACKING_AUTHOR_CURRENT:-}"
	local tracking_id="" tracking_body=""
	if [ -n "${author_login}" ]; then
		tracking_id=$(printf '%s' "${comments_json}" | jq -r \
			--arg login "${author_login}" --arg re "${_TG_TRACKING_CLEANUP_RE}" \
			"${_TG_TRACKING_JQ_DEFS}"'[.[]? | select(tg_trusted($login) and ([tg_marker_ids($re)] | length > 0))] | last | .id // empty' 2>/dev/null) || tracking_id=""
		tracking_body=$(printf '%s' "${comments_json}" | jq -r \
			--arg login "${author_login}" --arg re "${_TG_TRACKING_CLEANUP_RE}" \
			"${_TG_TRACKING_JQ_DEFS}"'[.[]? | select(tg_trusted($login) and ([tg_marker_ids($re)] | length > 0))] | last | .body // empty' 2>/dev/null) || tracking_body=""
	fi
	[[ "${tracking_id}" =~ ^[0-9]+$ ]] || tracking_id=""

	if [ -n "${tracking_id}" ] && [ -n "${tracking_body}" ]; then
		# Append to existing tracking comment
		local new_body
		new_body=$(printf '%s' "${tracking_body}" | \
			sed "s/<!-- tg_cleanup:\([0-9,]*\) -->/<!-- tg_cleanup:\1,${msg_id} -->/")
		curl -s -X PATCH \
			-H "Authorization: token ${GH_TOKEN}" \
			-H "Accept: application/vnd.github.v3+json" \
			"https://api.github.com/repos/${GITHUB_REPOSITORY}/issues/comments/${tracking_id}" \
			-d "$(jq -n --arg b "${new_body}" '{body: $b}')" >/dev/null 2>&1 || true
	else
		# Create new tracking comment
		curl -s -X POST \
			-H "Authorization: token ${GH_TOKEN}" \
			-H "Accept: application/vnd.github.v3+json" \
			"${api_base}/${issue_num}/comments" \
			-d "$(jq -n --arg b "<!-- tg_cleanup:${msg_id} -->" '{body: $b}')" >/dev/null 2>&1 || true
	fi
}

# ---------------------------------------------------------------
# tg_send_tracked — Send a Telegram message and track its ID.
# Drop-in replacement for fire-and-forget tg_notify() calls.
# Usage: tg_send_tracked 42 "message text" "ERROR"
# ---------------------------------------------------------------
tg_send_tracked()
{
	local issue_num="$1" msg="$2" level="${3:-CRITICAL}"
	local msg_id
	msg_id=$(tg_send_msg "${msg}" "${level}")
	tg_store_msg_id "${issue_num}" "${msg_id}"
}

# ---------------------------------------------------------------
# tg_store_phase_msg_id — Store a message ID with a phase tag.
# Comment body pattern: <!-- tg_phase:PHASE:id1,id2 -->
# Usage: tg_store_phase_msg_id 42 clarify 12345
# ---------------------------------------------------------------
tg_store_phase_msg_id()
{
	local issue_num="$1" phase="$2" msg_id="$3"
	if [ -z "${msg_id}" ] || [ -z "${issue_num}" ] || [ "${issue_num}" = "0" ] || [ -z "${phase}" ]; then
		return 0
	fi
	if ! _tg_valid_phase "${phase}"; then
		echo "::warning::Invalid Telegram tracking phase for issue #${issue_num}; message ID not stored" >&2
		return 0
	fi
	if [ -z "${GH_TOKEN:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
		return 0
	fi

	local api_base="https://api.github.com/repos/${GITHUB_REPOSITORY}/issues"

	local comments_json
	comments_json=$(_tg_newest_comments_json "${issue_num}") || {
		echo "::warning::Failed to fetch existing phase tracking comments for issue #${issue_num} phase=${phase}; creating a new tracking comment" >&2
		curl -s -X POST \
			-H "Authorization: token ${GH_TOKEN}" \
			-H "Accept: application/vnd.github.v3+json" \
			"${api_base}/${issue_num}/comments" \
			-d "$(jq -n --arg b "<!-- tg_phase:${phase}:${msg_id} -->" '{body: $b}')" >/dev/null 2>&1 || true
		return 0
	}

	# Only the trusted author's exact marker is appended to (#8598).
	_tg_tracking_author_login
	local author_login="${_TG_TRACKING_AUTHOR_CURRENT:-}"
	local phase_re
	phase_re="$(_tg_tracking_phase_re "${phase}")"
	local tracking_id="" tracking_body=""
	if [ -n "${author_login}" ]; then
		tracking_id=$(printf '%s' "${comments_json}" | jq -r \
			--arg login "${author_login}" --arg re "${phase_re}" \
			"${_TG_TRACKING_JQ_DEFS}"'[.[]? | select(tg_trusted($login) and ([tg_marker_ids($re)] | length > 0))] | last | .id // empty' 2>/dev/null) || tracking_id=""
		tracking_body=$(printf '%s' "${comments_json}" | jq -r \
			--arg login "${author_login}" --arg re "${phase_re}" \
			"${_TG_TRACKING_JQ_DEFS}"'[.[]? | select(tg_trusted($login) and ([tg_marker_ids($re)] | length > 0))] | last | .body // empty' 2>/dev/null) || tracking_body=""
	fi
	[[ "${tracking_id}" =~ ^[0-9]+$ ]] || tracking_id=""

	if [ -n "${tracking_id}" ] && [ -n "${tracking_body}" ]; then
		local new_body
		new_body=$(printf '%s' "${tracking_body}" | \
			sed "s/<!-- tg_phase:${phase}:\([0-9,]*\) -->/<!-- tg_phase:${phase}:\1,${msg_id} -->/")
		curl -s -X PATCH \
			-H "Authorization: token ${GH_TOKEN}" \
			-H "Accept: application/vnd.github.v3+json" \
			"https://api.github.com/repos/${GITHUB_REPOSITORY}/issues/comments/${tracking_id}" \
			-d "$(jq -n --arg b "${new_body}" '{body: $b}')" >/dev/null 2>&1 || true
	else
		curl -s -X POST \
			-H "Authorization: token ${GH_TOKEN}" \
			-H "Accept: application/vnd.github.v3+json" \
			"${api_base}/${issue_num}/comments" \
			-d "$(jq -n --arg b "<!-- tg_phase:${phase}:${msg_id} -->" '{body: $b}')" >/dev/null 2>&1 || true
	fi
}

# ---------------------------------------------------------------
# tg_send_phase_tracked — Send a TG message and track with phase.
# Usage: tg_send_phase_tracked 42 clarify "message text" "WARNING"
# ---------------------------------------------------------------
tg_send_phase_tracked()
{
	local issue_num="$1" phase="$2" msg="$3" level="${4:-CRITICAL}"
	local msg_id
	msg_id=$(tg_send_msg "${msg}" "${level}")
	tg_store_phase_msg_id "${issue_num}" "${phase}" "${msg_id}"
}

# ---------------------------------------------------------------
# tg_send_phase_failure — Send a phase-failure Telegram message.
# Usage: tg_send_phase_failure clarify "message text" "ERROR" 42
# The phase argument is retained for call-site readability; the current
# failure path preserves the existing tracked-send behavior.
# ---------------------------------------------------------------
tg_send_phase_failure()
{
	local phase="$1" reason="$2" level="${3:-ERROR}" tracked_num="${4:-${ISSUE_NUMBER:-${PR_NUMBER:-0}}}"
	[ -n "${phase}" ] || return 0
	[ -n "${reason}" ] || return 0
	if [ -n "${tracked_num}" ] && [ "${tracked_num}" != "0" ]; then
		tg_send_tracked "${tracked_num}" "${reason}" "${level}"
	else
		tg_send_msg "${reason}" "${level}" >/dev/null
	fi
}

# ---------------------------------------------------------------
# tg_cleanup_phase_msgs — Delete TG messages for a specific phase.
# Usage: tg_cleanup_phase_msgs 42 clarify
# ---------------------------------------------------------------
tg_cleanup_phase_msgs()
{
	local issue_num="$1" phase="$2"
	if [ -z "${issue_num}" ] || [ "${issue_num}" = "0" ] || [ -z "${phase}" ]; then
		return 0
	fi
	if [ -z "${GH_TOKEN:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
		return 0
	fi
	if ! _tg_valid_phase "${phase}"; then
		echo "::warning::Invalid Telegram tracking phase for cleanup (issue #${issue_num}); nothing deleted" >&2
		return 0
	fi

	local chat_id
	chat_id="$(_tg_chat_id)"

	# Resolve the trusted author in this shell so the login cache survives.
	_tg_tracking_author_login
	local author_login="${_TG_TRACKING_AUTHOR_CURRENT:-}"

	# Snapshot every page before deleting anything (BUG-003). Records are
	# "comment_id<TAB>msg_id_list" from the trusted author's exact markers.
	local tracking_entries
	tracking_entries=$(_tg_snapshot_tracking_entries "${issue_num}" "${phase}" "${author_login}") || return 0

	local kept_comments=0 failed_ids_total=0 skipped_ids_total=0 consecutive=0
	local comment_id id_list
	while IFS=$'\t' read -r comment_id id_list; do
		# Fail closed: skip any record that is not exactly id<TAB>ids.
		[[ "${comment_id}" =~ ^[0-9]+$ ]] || continue
		[[ "${id_list}" =~ ^[0-9]+(,[0-9]+)*$ ]] || continue
		local failed=0 skipped=0
		if [ -n "${id_list}" ] && [ -n "${chat_id}" ] && [ -n "${TG_BOT_SECRET:-}" ]; then
			local old_ifs="${IFS}"
			IFS=','
			for tg_id in ${id_list}; do
				[ -n "${tg_id}" ] || continue
				# Breaker: after 3 consecutive transient failures stop calling
				# Telegram for this cleanup and keep the remaining comments.
				if [ "${consecutive}" -ge 3 ]; then
					skipped=$((skipped + 1))
				elif _tg_delete_msg_checked "${tg_id}"; then
					consecutive=0
				else
					failed=$((failed + 1))
					consecutive=$((consecutive + 1))
				fi
			done
			IFS="${old_ifs}"
		fi
		if [ "${failed}" -eq 0 ] && [ "${skipped}" -eq 0 ]; then
			curl -s --max-time 10 -X DELETE \
				-H "Authorization: token ${GH_TOKEN}" \
				-H "Accept: application/vnd.github.v3+json" \
				"https://api.github.com/repos/${GITHUB_REPOSITORY}/issues/comments/${comment_id}" \
				>/dev/null 2>&1 || true
		else
			# Keep the tracking comment so the next cleanup retries its IDs (BUG-006).
			kept_comments=$((kept_comments + 1))
			failed_ids_total=$((failed_ids_total + failed))
			skipped_ids_total=$((skipped_ids_total + skipped))
		fi
	done <<< "${tracking_entries}"

	local skipped_note=""
	if [ "${skipped_ids_total}" -gt 0 ]; then
		skipped_note=" (${skipped_ids_total} more not tried after 3 failures in a row)"
	fi
	if [ "${kept_comments}" -gt 0 ]; then
		echo "::warning::Telegram cleanup kept ${kept_comments} tracking comment(s) on issue #${issue_num} (phase ${phase}): ${failed_ids_total} message ID(s) hit a transient Telegram failure${skipped_note}; the next cleanup retries them" >&2
	fi
	return 0
}

# ---------------------------------------------------------------
# tg_cleanup_msgs — Delete all tracked TG messages for an issue.
# Reads tracking comments, deletes each TG message, then removes
# the tracking comments from GitHub. All pages are read before
# anything is deleted; a tracking comment is kept when any of its
# IDs hits a transient Telegram failure, so the next cleanup retries.
# Usage: tg_cleanup_msgs 42
# ---------------------------------------------------------------
tg_cleanup_msgs()
{
	local issue_num="$1"
	if [ -z "${issue_num}" ] || [ "${issue_num}" = "0" ]; then
		return 0
	fi
	if [ -z "${GH_TOKEN:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
		return 0
	fi

	local chat_id
	chat_id="$(_tg_chat_id)"

	# Snapshot every page before deleting anything (BUG-003). Matches both
	# general and phase-specific tracking comments written by the trusted
	# author (#8598); records are "comment_id<TAB>msg_id_list".
	local tracking_entries
	_tg_tracking_author_login
	local author_login="${_TG_TRACKING_AUTHOR_CURRENT:-}"
	tracking_entries=$(_tg_snapshot_tracking_entries "${issue_num}" "" "${author_login}") || return 0

	local kept_comments=0 failed_ids_total=0 skipped_ids_total=0 consecutive=0
	local comment_id id_list
	while IFS=$'\t' read -r comment_id id_list; do
		# Fail closed: skip any record that is not exactly id<TAB>ids.
		[[ "${comment_id}" =~ ^[0-9]+$ ]] || continue
		[[ "${id_list}" =~ ^[0-9]+(,[0-9]+)*$ ]] || continue
		local failed=0 skipped=0
		if [ -n "${id_list}" ] && [ -n "${chat_id}" ] && [ -n "${TG_BOT_SECRET:-}" ]; then
			local old_ifs="${IFS}"
			IFS=','
			for tg_id in ${id_list}; do
				[ -n "${tg_id}" ] || continue
				# Breaker: after 3 consecutive transient failures stop calling
				# Telegram for this cleanup and keep the remaining comments.
				if [ "${consecutive}" -ge 3 ]; then
					skipped=$((skipped + 1))
				elif _tg_delete_msg_checked "${tg_id}"; then
					consecutive=0
				else
					failed=$((failed + 1))
					consecutive=$((consecutive + 1))
				fi
			done
			IFS="${old_ifs}"
		fi
		if [ "${failed}" -eq 0 ] && [ "${skipped}" -eq 0 ]; then
			# Remove the tracking comment from GitHub
			curl -s --max-time 10 -X DELETE \
				-H "Authorization: token ${GH_TOKEN}" \
				-H "Accept: application/vnd.github.v3+json" \
				"https://api.github.com/repos/${GITHUB_REPOSITORY}/issues/comments/${comment_id}" \
				>/dev/null 2>&1 || true
		else
			# Keep the tracking comment so the next cleanup retries its IDs (BUG-006).
			kept_comments=$((kept_comments + 1))
			failed_ids_total=$((failed_ids_total + failed))
			skipped_ids_total=$((skipped_ids_total + skipped))
		fi
	done <<< "${tracking_entries}"

	local skipped_note=""
	if [ "${skipped_ids_total}" -gt 0 ]; then
		skipped_note=" (${skipped_ids_total} more not tried after 3 failures in a row)"
	fi
	if [ "${kept_comments}" -gt 0 ]; then
		echo "::warning::Telegram cleanup kept ${kept_comments} tracking comment(s) on issue #${issue_num}: ${failed_ids_total} message ID(s) hit a transient Telegram failure${skipped_note}; the next cleanup retries them" >&2
	fi
	return 0
}
