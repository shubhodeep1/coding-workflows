#!/usr/bin/env bash

if [ "${_WATCHDOG_HELPERS_LOADED:-}" = "true" ]; then
	return 0 2>/dev/null || true
fi
_WATCHDOG_HELPERS_LOADED="true"

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
