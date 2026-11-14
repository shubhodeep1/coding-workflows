#!/usr/bin/env bash
# alert_inbox_capture.sh — capture Telegram alerts into the alert inbox.
#
# Sourced by scripts/tg_helpers.sh (alert triage plan, Phase 1,
# docs/plans/alert-triage-auto-heal-plan.md). Defines functions only.
#
# The Telegram Bot API cannot read the bot's own messages back, so alerts are
# captured at the source: tg_send_msg asks alert_inbox_mode which routing mode
# is on and calls alert_inbox_capture, which sends one `repository_dispatch`
# (event type `alert-inbox`) to shubhodeep1/coding-workflows. There,
# .github/workflows/alert-inbox-intake.yml runs scripts/alert_inbox_store.sh,
# which validates the record and writes it to the branch `ai/alert-inbox`.
#
# The switches live in .github/ai/alert_inbox.json and are read only from the
# trusted support roots (the same order as support_roots() in
# scripts/claude_engine.py), never from the caller's checkout or $PWD. A
# missing, symlinked or invalid file, a non-boolean switch, or no jq means
# "off". Both switches ship as false, so this file is inert by default.
#
# Alert text is untrusted data: it only reaches jq as a --rawfile value and
# curl as a --data-binary @file body. It is never eval'd, interpolated into a
# command or placed in argv. The GH_TOKEN is passed to curl in a 0600 header
# file, never in argv, and is never logged.
#
# Log line (stderr, stable prefix ALERT_INBOX):
#   ALERT_INBOX capture outcome=sent|failed reason=<token>

# Guard against double-sourcing
if [ "${_ALERT_INBOX_CAPTURE_LOADED:-}" = "true" ]; then
	return 0 2>/dev/null || true
fi
_ALERT_INBOX_CAPTURE_LOADED="true"

_ALERT_INBOX_CAPTURE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd)" || _ALERT_INBOX_CAPTURE_DIR=""
_ALERT_INBOX_DISPATCH_REPO="shubhodeep1/coding-workflows"
_ALERT_INBOX_CONFIG_REL=".github/ai/alert_inbox.json"
_ALERT_INBOX_MESSAGE_MAX_BYTES=4000
_ALERT_INBOX_WORKFLOW_MAX_BYTES=200
_ALERT_INBOX_JOB_MAX_BYTES=100

# _alert_inbox_log OUTCOME REASON — one fixed-format line on stderr.
_alert_inbox_log()
{
	printf 'ALERT_INBOX capture outcome=%s reason=%s\n' "${1:-failed}" "${2:-unknown}" >&2
}

# ---------------------------------------------------------------
# _alert_inbox_config_path — print the trusted config path.
# Roots, in order: ${SUPPORT_ROOT_DIR}, ${GITHUB_WORKSPACE}/.codex-workflow-src,
# the parent of this script's directory. The first regular, non-symlink file
# wins; symlinks are skipped. Returns 1 (prints nothing) when none is found.
# ---------------------------------------------------------------
_alert_inbox_config_path()
{
	local root="" candidate=""
	local -a roots=()
	if [ -n "${SUPPORT_ROOT_DIR:-}" ]; then
		roots+=("${SUPPORT_ROOT_DIR}")
	fi
	if [ -n "${GITHUB_WORKSPACE:-}" ]; then
		roots+=("${GITHUB_WORKSPACE}/.codex-workflow-src")
	fi
	if [ -n "${_ALERT_INBOX_CAPTURE_DIR:-}" ]; then
		roots+=("$(dirname "${_ALERT_INBOX_CAPTURE_DIR}")")
	fi
	for root in "${roots[@]}"; do
		candidate="${root}/${_ALERT_INBOX_CONFIG_REL}"
		if [ -f "${candidate}" ] && [ ! -L "${candidate}" ]; then
			printf '%s' "${candidate}"
			return 0
		fi
	done
	return 1
}

# ---------------------------------------------------------------
# alert_inbox_mode — print exactly one of: off, capture, route.
#   capture: capture_enabled is JSON true;
#   route:   capture_enabled and route_warning_error_to_inbox are JSON true.
# Both switches must be JSON booleans; anything else is "off".
# ---------------------------------------------------------------
alert_inbox_mode()
{
	local cfg="" mode="off"
	if ! command -v jq >/dev/null 2>&1; then
		printf 'off'
		return 0
	fi
	cfg="$(_alert_inbox_config_path 2>/dev/null)" || cfg=""
	if [ -z "${cfg}" ]; then
		printf 'off'
		return 0
	fi
	mode="$(jq -r 'if type == "object"
		and (.capture_enabled | type) == "boolean"
		and (.route_warning_error_to_inbox | type) == "boolean"
		and .capture_enabled == true
		then (if .route_warning_error_to_inbox == true then "route" else "capture" end)
		else "off" end' "${cfg}" 2>/dev/null)" || mode="off"
	case "${mode}" in
		capture|route) ;;
		*) mode="off" ;;
	esac
	printf '%s' "${mode}"
}

# _alert_inbox_cap_file CAP IN OUT — copy IN to OUT capped at CAP bytes on a
# UTF-8 boundary (scripts/truncate_to_utf8_byte_cap.py). Returns 1 on failure.
_alert_inbox_cap_file()
{
	local cap="$1" in_file="$2" out_file="$3"
	local trunc="${_ALERT_INBOX_CAPTURE_DIR:-}/truncate_to_utf8_byte_cap.py"
	if [ -z "${_ALERT_INBOX_CAPTURE_DIR:-}" ] || [ ! -f "${trunc}" ]; then
		return 2
	fi
	if ! command -v python3 >/dev/null 2>&1; then
		return 1
	fi
	if ! python3 -I -B "${trunc}" "${cap}" <"${in_file}" >"${out_file}" 2>/dev/null; then
		return 1
	fi
	return 0
}

# ---------------------------------------------------------------
# alert_inbox_capture LEVEL RAW_MESSAGE SENT_TO_TELEGRAM(true|false)
# Builds one alert_inbox.v1 record and dispatches it. Never writes to stdout.
# Returns 0 only when the dispatch returned HTTP 204; any failure returns 1.
# Safe under a caller's `set -euo pipefail` when called in an `if` or `||`.
# ---------------------------------------------------------------
alert_inbox_capture()
{
	local IFS=$' \t\n'
	local level="${1:-}" raw_msg="${2:-}" sent="${3:-false}"
	local repo="${GITHUB_REPOSITORY:-}" run_id="${GITHUB_RUN_ID:-}"
	local api="${GITHUB_API_URL:-https://api.github.com}"
	local server="${GITHUB_SERVER_URL:-https://github.com}"
	local token="${GH_TOKEN:-}"
	if [ "${sent}" != "true" ]; then
		sent="false"
	fi
	level="$(printf '%s' "${level}" | tr '[:lower:]' '[:upper:]')"
	case "${level}" in
		WARNING|ERROR|CRITICAL) ;;
		*)
			_alert_inbox_log failed bad_level
			return 1
			;;
	esac
	if [ -z "${token}" ]; then
		_alert_inbox_log failed no_token
		return 1
	fi
	case "${token}" in
		*$'\n'*|*$'\r'*)
			_alert_inbox_log failed no_token
			return 1
			;;
	esac
	if ! [[ "${repo}" =~ ^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$ ]] || ! [[ "${run_id}" =~ ^[0-9]{1,20}$ ]]; then
		_alert_inbox_log failed no_run_context
		return 1
	fi
	case "${api}" in
		https://*) ;;
		*)
			_alert_inbox_log failed payload_build
			return 1
			;;
	esac
	if ! command -v jq >/dev/null 2>&1; then
		_alert_inbox_log failed payload_build
		return 1
	fi

	local tmp_dir=""
	tmp_dir="$(mktemp -d 2>/dev/null)" || tmp_dir=""
	if [ -z "${tmp_dir}" ] || [ ! -d "${tmp_dir}" ]; then
		_alert_inbox_log failed payload_build
		return 1
	fi
	chmod 700 "${tmp_dir}" 2>/dev/null || true

	local raw_file="${tmp_dir}/raw" msg_file="${tmp_dir}/message"
	local wf_raw="${tmp_dir}/wf_raw" wf_file="${tmp_dir}/workflow"
	local job_raw="${tmp_dir}/job_raw" job_file="${tmp_dir}/job"
	local body_file="${tmp_dir}/body.json" hdr_file="${tmp_dir}/headers"
	local cap_rc=0
	printf '%s' "${raw_msg}" >"${raw_file}" 2>/dev/null || cap_rc=1
	printf '%s' "${GITHUB_WORKFLOW:-}" >"${wf_raw}" 2>/dev/null || cap_rc=1
	printf '%s' "${GITHUB_JOB:-}" >"${job_raw}" 2>/dev/null || cap_rc=1
	if [ "${cap_rc}" -ne 0 ]; then
		rm -rf "${tmp_dir}" 2>/dev/null || true
		_alert_inbox_log failed payload_build
		return 1
	fi
	_alert_inbox_cap_file "${_ALERT_INBOX_MESSAGE_MAX_BYTES}" "${raw_file}" "${msg_file}" || cap_rc=$?
	if [ "${cap_rc}" -eq 0 ]; then
		_alert_inbox_cap_file "${_ALERT_INBOX_WORKFLOW_MAX_BYTES}" "${wf_raw}" "${wf_file}" || cap_rc=$?
	fi
	if [ "${cap_rc}" -eq 0 ]; then
		_alert_inbox_cap_file "${_ALERT_INBOX_JOB_MAX_BYTES}" "${job_raw}" "${job_file}" || cap_rc=$?
	fi
	if [ "${cap_rc}" -ne 0 ]; then
		rm -rf "${tmp_dir}" 2>/dev/null || true
		if [ "${cap_rc}" -eq 2 ]; then
			_alert_inbox_log failed truncate_unavailable
		else
			_alert_inbox_log failed payload_build
		fi
		return 1
	fi

	local captured_at="" record_id="" release_sha="" cfg="" support_root=""
	captured_at="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ 2>/dev/null)" || captured_at=""
	if ! [[ "${captured_at}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$ ]]; then
		rm -rf "${tmp_dir}" 2>/dev/null || true
		_alert_inbox_log failed payload_build
		return 1
	fi
	record_id="$({ printf '%s|%s|%s|' "$(date +%s%N 2>/dev/null)" "${run_id}" "${RANDOM}"; cat "${msg_file}"; } 2>/dev/null | sha256sum 2>/dev/null | cut -c1-32)" || record_id=""
	if ! [[ "${record_id}" =~ ^[0-9a-f]{32}$ ]]; then
		rm -rf "${tmp_dir}" 2>/dev/null || true
		_alert_inbox_log failed payload_build
		return 1
	fi
	cfg="$(_alert_inbox_config_path 2>/dev/null)" || cfg=""
	if [ -n "${cfg}" ] && command -v git >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
		support_root="${cfg%/"${_ALERT_INBOX_CONFIG_REL}"}"
		release_sha="$(timeout 5 git -C "${support_root}" rev-parse HEAD 2>/dev/null)" || release_sha=""
	fi
	if ! [[ "${release_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		release_sha=""
	fi

	if ! jq -n -c \
		--arg id "${record_id}" \
		--arg captured_at "${captured_at}" \
		--arg level "${level}" \
		--arg source_repo "${repo}" \
		--rawfile workflow "${wf_file}" \
		--arg run_id "${run_id}" \
		--arg run_url "${server}/${repo}/actions/runs/${run_id}" \
		--rawfile job "${job_file}" \
		--rawfile message "${msg_file}" \
		--arg release_sha "${release_sha}" \
		--argjson sent_to_telegram "${sent}" \
		'{event_type: "alert-inbox", client_payload: {schema: "alert_inbox.v1", record: {
			id: $id, captured_at: $captured_at, level: $level, source_repo: $source_repo,
			workflow: $workflow, run_id: $run_id, run_url: $run_url, job: $job,
			message: $message, release_sha: $release_sha, sent_to_telegram: $sent_to_telegram}}}' \
		>"${body_file}" 2>/dev/null; then
		rm -rf "${tmp_dir}" 2>/dev/null || true
		_alert_inbox_log failed payload_build
		return 1
	fi
	# Invalid UTF-8 in the alert becomes U+FFFD in jq and can grow past the cap;
	# the intake would reject it, so fail here (the alert still reaches Telegram).
	if ! jq -e --argjson cap "${_ALERT_INBOX_MESSAGE_MAX_BYTES}" '.client_payload.record.message | utf8bytelength <= $cap' "${body_file}" >/dev/null 2>&1; then
		rm -rf "${tmp_dir}" 2>/dev/null || true
		_alert_inbox_log failed payload_build
		return 1
	fi

	if ! ( umask 077 && printf 'Authorization: Bearer %s\nAccept: application/vnd.github+json\nContent-Type: application/json\nX-GitHub-Api-Version: 2022-11-28\n' "${token}" >"${hdr_file}" ) 2>/dev/null; then
		rm -rf "${tmp_dir}" 2>/dev/null || true
		_alert_inbox_log failed payload_build
		return 1
	fi
	chmod 600 "${hdr_file}" 2>/dev/null || true

	local http_code="" curl_rc=0
	http_code="$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' -X POST \
		-H "@${hdr_file}" \
		--data-binary "@${body_file}" \
		"${api}/repos/${_ALERT_INBOX_DISPATCH_REPO}/dispatches" 2>/dev/null)" || curl_rc=$?
	rm -rf "${tmp_dir}" 2>/dev/null || true
	if [ "${curl_rc}" -ne 0 ]; then
		_alert_inbox_log failed curl_error
		return 1
	fi
	if [ "${http_code}" != "204" ]; then
		[[ "${http_code}" =~ ^[0-9]{3}$ ]] || http_code="000"
		_alert_inbox_log failed "http_${http_code}"
		return 1
	fi
	_alert_inbox_log sent ok
	return 0
}
