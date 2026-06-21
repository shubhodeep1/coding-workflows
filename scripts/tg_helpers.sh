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
# ALERT_MSG_LEVEL env var controls minimum level sent (default: DEBUG).
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
	local threshold="${ALERT_MSG_LEVEL:-DEBUG}"
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
	curl -s -X POST "https://api.telegram.org/bot${TG_BOT_SECRET}/deleteMessage" \
		-d "chat_id=${chat_id}" \
		-d "message_id=${msg_id}" >/dev/null 2>&1 || true
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

	# Look for an existing tracking comment (scan last 30 comments)
	local comments_json
	comments_json=$(curl_gh_api -s \
		-H "Authorization: token ${GH_TOKEN}" \
		-H "Accept: application/vnd.github.v3+json" \
		"${api_base}/${issue_num}/comments?per_page=30&direction=desc") || {
		echo "::warning::Failed to fetch existing tracking comments for issue #${issue_num}; creating a new tracking comment" >&2
		# Fallback: create a new tracking comment
		curl -s -X POST \
			-H "Authorization: token ${GH_TOKEN}" \
			-H "Accept: application/vnd.github.v3+json" \
			"${api_base}/${issue_num}/comments" \
			-d "$(jq -n --arg b "<!-- tg_cleanup:${msg_id} -->" '{body: $b}')" >/dev/null 2>&1 || true
		return 0
	}

	local tracking_id tracking_body
	tracking_id=$(printf '%s' "${comments_json}" | jq -r \
		'[.[] | select(.body | test("<!-- tg_cleanup:"))] | first | .id // empty' 2>/dev/null)
	tracking_body=$(printf '%s' "${comments_json}" | jq -r \
		'[.[] | select(.body | test("<!-- tg_cleanup:"))] | first | .body // empty' 2>/dev/null)

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
	if [ -z "${GH_TOKEN:-}" ] || [ -z "${GITHUB_REPOSITORY:-}" ]; then
		return 0
	fi

	local api_base="https://api.github.com/repos/${GITHUB_REPOSITORY}/issues"
	local marker="<!-- tg_phase:${phase}:"

	local comments_json
	comments_json=$(curl_gh_api -s \
		-H "Authorization: token ${GH_TOKEN}" \
		-H "Accept: application/vnd.github.v3+json" \
		"${api_base}/${issue_num}/comments?per_page=30&direction=desc") || {
		echo "::warning::Failed to fetch existing phase tracking comments for issue #${issue_num} phase=${phase}; creating a new tracking comment" >&2
		curl -s -X POST \
			-H "Authorization: token ${GH_TOKEN}" \
			-H "Accept: application/vnd.github.v3+json" \
			"${api_base}/${issue_num}/comments" \
			-d "$(jq -n --arg b "<!-- tg_phase:${phase}:${msg_id} -->" '{body: $b}')" >/dev/null 2>&1 || true
		return 0
	}

	local tracking_id tracking_body
	tracking_id=$(printf '%s' "${comments_json}" | jq -r \
		--arg m "${marker}" \
		'[.[] | select(.body | contains($m))] | first | .id // empty' 2>/dev/null)
	tracking_body=$(printf '%s' "${comments_json}" | jq -r \
		--arg m "${marker}" \
		'[.[] | select(.body | contains($m))] | first | .body // empty' 2>/dev/null)

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

	local chat_id
	chat_id="$(_tg_chat_id)"
	local api_base="https://api.github.com/repos/${GITHUB_REPOSITORY}/issues"
	local marker="<!-- tg_phase:${phase}:"
	local page=1

	while true; do
		local comments_json
		comments_json=$(curl_gh_api -s \
			-H "Authorization: token ${GH_TOKEN}" \
			-H "Accept: application/vnd.github.v3+json" \
			"${api_base}/${issue_num}/comments?per_page=100&page=${page}") || {
			echo "::warning::Failed to fetch issue comments for phase cleanup (issue #${issue_num}, phase ${phase}, page ${page}); aborting cleanup" >&2
			break
		}

		local count
		count=$(printf '%s' "${comments_json}" | jq 'length' 2>/dev/null) || break
		[ "${count:-0}" -gt 0 ] || break

		local tracking_entries
		tracking_entries=$(printf '%s' "${comments_json}" | jq -r \
			--arg m "${marker}" \
			'.[] | select(.body | contains($m)) | "\(.id)\t\(.body)"' 2>/dev/null) || true

		if [ -n "${tracking_entries}" ]; then
			while IFS=$'\t' read -r comment_id comment_body; do
				[ -n "${comment_id}" ] || continue
				local id_list
				id_list=$(printf '%s' "${comment_body}" | \
					sed -n "s/.*<!-- tg_phase:${phase}:\([0-9,]*\) -->.*/\1/p")
				if [ -n "${id_list}" ] && [ -n "${chat_id}" ] && [ -n "${TG_BOT_SECRET:-}" ]; then
					local old_ifs="${IFS}"
					IFS=','
					for tg_id in ${id_list}; do
						[ -n "${tg_id}" ] || continue
						tg_delete_msg "${tg_id}"
					done
					IFS="${old_ifs}"
				fi
				curl -s -X DELETE \
					-H "Authorization: token ${GH_TOKEN}" \
					-H "Accept: application/vnd.github.v3+json" \
					"https://api.github.com/repos/${GITHUB_REPOSITORY}/issues/comments/${comment_id}" \
					>/dev/null 2>&1 || true
			done <<< "${tracking_entries}"
		fi

		[ "${count}" -lt 100 ] && break
		page=$((page + 1))
	done
}

# ---------------------------------------------------------------
# tg_cleanup_msgs — Delete all tracked TG messages for an issue.
# Reads tracking comments, deletes each TG message, then removes
# the tracking comments from GitHub.
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

	local api_base="https://api.github.com/repos/${GITHUB_REPOSITORY}/issues"
	local page=1

	while true; do
		local comments_json
		comments_json=$(curl_gh_api -s \
			-H "Authorization: token ${GH_TOKEN}" \
			-H "Accept: application/vnd.github.v3+json" \
			"${api_base}/${issue_num}/comments?per_page=100&page=${page}") || {
			echo "::warning::Failed to fetch issue comments for cleanup (issue #${issue_num}, page ${page}); aborting cleanup" >&2
			break
		}

		local count
		count=$(printf '%s' "${comments_json}" | jq 'length' 2>/dev/null) || break
		[ "${count:-0}" -gt 0 ] || break

		# Extract tracking comments (both general and phase-specific)
		local tracking_entries
		tracking_entries=$(printf '%s' "${comments_json}" | jq -r \
			'.[] | select(.body | test("<!-- tg_(cleanup|phase):")) | "\(.id)\t\(.body)"' 2>/dev/null) || true

		if [ -n "${tracking_entries}" ]; then
			while IFS=$'\t' read -r comment_id comment_body; do
				[ -n "${comment_id}" ] || continue
				# Extract comma-separated message IDs (general or phase tags)
				local id_list
				id_list=$(printf '%s' "${comment_body}" | \
					sed -n 's/.*<!-- tg_\(cleanup\|phase:[a-z_]*\):\([0-9,]*\) -->.*/\2/p')
				if [ -n "${id_list}" ] && [ -n "${chat_id}" ] && [ -n "${TG_BOT_SECRET:-}" ]; then
					local old_ifs="${IFS}"
					IFS=','
					for tg_id in ${id_list}; do
						[ -n "${tg_id}" ] || continue
						tg_delete_msg "${tg_id}"
					done
					IFS="${old_ifs}"
				fi
				# Remove the tracking comment from GitHub
				curl -s -X DELETE \
					-H "Authorization: token ${GH_TOKEN}" \
					-H "Accept: application/vnd.github.v3+json" \
					"https://api.github.com/repos/${GITHUB_REPOSITORY}/issues/comments/${comment_id}" \
					>/dev/null 2>&1 || true
			done <<< "${tracking_entries}"
		fi

		[ "${count}" -lt 100 ] && break
		page=$((page + 1))
	done
}
