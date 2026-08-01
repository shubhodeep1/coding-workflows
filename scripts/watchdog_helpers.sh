#!/usr/bin/env bash

if [ "${_WATCHDOG_HELPERS_LOADED:-}" = "true" ]; then
	return 0 2>/dev/null || true
fi
_WATCHDOG_HELPERS_LOADED="true"

CODEX_RUN_BUDGET_DEFAULT_MINUTES="210"

normalize_review_soft_deadline_minutes()
{
	local raw_value="${1:-${REVIEW_SOFT_DEADLINE_MINUTES:-${CODEX_RUN_BUDGET_DEFAULT_MINUTES}}}"

	case "${raw_value}" in
		''|*[!0-9]*|0|0[0-9]*)
			echo "::warning::Invalid REVIEW_SOFT_DEADLINE_MINUTES='${raw_value}'; defaulting to ${CODEX_RUN_BUDGET_DEFAULT_MINUTES}." >&2
			printf '%s\n' "${CODEX_RUN_BUDGET_DEFAULT_MINUTES}"
			return 0
			;;
	esac

	printf '%s\n' "${raw_value}"
}

codex_run_budget_initialize()
{
	local job_start_epoch_raw="${1:-${JOB_START_EPOCH:-${CODEX_RUN_BUDGET_START_EPOCH:-}}}"
	local soft_deadline_minutes=""
	local start_epoch=""
	local total_secs=""
	local soft_deadline_epoch=""

	soft_deadline_minutes="$(normalize_review_soft_deadline_minutes "${2:-${REVIEW_SOFT_DEADLINE_MINUTES:-}}")"

	case "${job_start_epoch_raw}" in
		''|*[!0-9]*)
			start_epoch="$(date +%s)"
			;;
		*)
			start_epoch="${job_start_epoch_raw}"
			;;
	esac

	total_secs="$(( soft_deadline_minutes * 60 ))"
	soft_deadline_epoch="$(( start_epoch + total_secs ))"

	printf 'JOB_START_EPOCH=%s\n' "${start_epoch}"
	printf 'CODEX_RUN_BUDGET_START_EPOCH=%s\n' "${start_epoch}"
	printf 'CODEX_RUN_BUDGET_SOFT_DEADLINE_EPOCH=%s\n' "${soft_deadline_epoch}"
	printf 'CODEX_RUN_BUDGET_TOTAL_SECS=%s\n' "${total_secs}"
	printf 'REVIEW_SOFT_DEADLINE_MINUTES=%s\n' "${soft_deadline_minutes}"
}

codex_run_budget_export()
{
	local line=""

	while IFS= read -r line; do
		case "${line}" in
			*=*) export "${line}" ;;
		esac
	done <<EOF
$(codex_run_budget_initialize "$@")
EOF
}

codex_run_budget_total_secs()
{
	local total_secs_raw="${CODEX_RUN_BUDGET_TOTAL_SECS:-}"

	case "${total_secs_raw}" in
		''|*[!0-9]*)
			printf '%s\n' "0"
			return 1
			;;
	esac

	printf '%s\n' "${total_secs_raw}"
}

codex_run_budget_start_epoch()
{
	local start_epoch_raw="${CODEX_RUN_BUDGET_START_EPOCH:-${JOB_START_EPOCH:-}}"

	case "${start_epoch_raw}" in
		''|*[!0-9]*)
			printf '%s\n' "0"
			return 1
			;;
	esac

	printf '%s\n' "${start_epoch_raw}"
}

codex_run_budget_soft_deadline_epoch()
{
	local soft_deadline_epoch_raw="${CODEX_RUN_BUDGET_SOFT_DEADLINE_EPOCH:-}"

	case "${soft_deadline_epoch_raw}" in
		''|*[!0-9]*)
			printf '%s\n' "0"
			return 1
			;;
	esac

	printf '%s\n' "${soft_deadline_epoch_raw}"
}

codex_run_budget_elapsed_secs()
{
	local now_epoch="${1:-$(date +%s)}"
	local start_epoch=""
	local elapsed_secs=""

	start_epoch="$(codex_run_budget_start_epoch)" || {
		printf '%s\n' "0"
		return 1
	}

	elapsed_secs="$(( now_epoch - start_epoch ))"
	if [ "${elapsed_secs}" -lt 0 ]; then
		elapsed_secs="0"
	fi
	printf '%s\n' "${elapsed_secs}"
}

codex_run_budget_remaining_secs()
{
	local now_epoch="${1:-$(date +%s)}"
	local soft_deadline_epoch=""
	local remaining_secs=""

	soft_deadline_epoch="$(codex_run_budget_soft_deadline_epoch)" || {
		printf '%s\n' "0"
		return 1
	}

	remaining_secs="$(( soft_deadline_epoch - now_epoch ))"
	if [ "${remaining_secs}" -lt 0 ]; then
		remaining_secs="0"
	fi
	printf '%s\n' "${remaining_secs}"
}

codex_run_budget_summary()
{
	local now_epoch="${1:-$(date +%s)}"
	local elapsed_secs=""
	local remaining_secs=""

	elapsed_secs="$(codex_run_budget_elapsed_secs "${now_epoch}")" || {
		return 1
	}
	remaining_secs="$(codex_run_budget_remaining_secs "${now_epoch}")" || {
		return 1
	}

	printf 'budget_elapsed_secs=%s budget_remaining_secs=%s\n' \
		"${elapsed_secs}" \
		"${remaining_secs}"
}

codex_run_budget_phase_may_start()
{
	local minimum_required_secs="${1:-1}"
	local remaining_secs=""

	case "${minimum_required_secs}" in
		''|*[!0-9]*)
			minimum_required_secs="1"
			;;
	esac

	remaining_secs="$(codex_run_budget_remaining_secs)" || return 0
	[ "${remaining_secs}" -ge "${minimum_required_secs}" ]
}

read_codex_stall_guard_state()
{
	local status_file="$1"
	local state=""

	[ -s "${status_file}" ] || return 1
	state="$(sed -n 's/^state=//p' "${status_file}" | head -n 1)"
	case "${state}" in
		observed|killed)
			printf '%s\n' "${state}"
			return 0
			;;
	esac

	return 1
}

codex_stall_guard_kill_detected()
{
	local _rc="${1:-0}"
	local stall_state="${2:-}"

	: "${_rc}"
	[ "${stall_state}" = "killed" ]
}

resolve_editor_network_probe_pid()
{
	local wrapper_pid="$1"
	local child_pid=""

	[ -n "${wrapper_pid}" ] || return 1
	child_pid="$(ps -o pid= --ppid "${wrapper_pid}" 2>/dev/null | awk 'NR==1 { print $1; exit }' || true)"
	if [ -n "${child_pid}" ]; then
		printf '%s\n' "${child_pid}"
	else
		printf '%s\n' "${wrapper_pid}"
	fi
}

_reap_editor_fifo_holders()
{
	local fifo="$1"
	local sig="${2:-TERM}"
	local link=""
	local target=""
	local pid=""

	while [ "${sig#-}" != "${sig}" ]; do
		sig="${sig#-}"
	done
	[ -n "${fifo}" ] && [ -e "${fifo}" ] || return 0
	if command -v fuser >/dev/null 2>&1; then
		fuser -k -"${sig}" "${fifo}" >/dev/null 2>&1 || true
	fi
	for link in /proc/[0-9]*/fd/*; do
		[ -e "${link}" ] || continue
		target="$(readlink -f "${link}" 2>/dev/null)" || continue
		[ "${target}" = "${fifo}" ] || continue
		pid="${link#/proc/}"
		pid="${pid%%/*}"
		case "${pid}" in
			''|*[!0-9]*) continue ;;
		esac
		[ "${pid}" = "$$" ] && continue
		kill -"${sig}" "${pid}" 2>/dev/null || true
	done
	return 0
}
