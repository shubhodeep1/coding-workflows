#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# shellcheck source=/dev/null
source "${REPO_ROOT}/scripts/watchdog_helpers.sh"

assert_eq()
{
	local expected="$1"
	local actual="$2"
	local message="$3"
	if [ "${expected}" != "${actual}" ]; then
		echo "${message}: expected '${expected}', got '${actual}'" >&2
		exit 1
	fi
}

test_read_codex_stall_guard_state()
{
	local tmpdir status_file state
	tmpdir="$(mktemp -d)"
	trap 'rm -rf "${tmpdir}"' RETURN
	status_file="${tmpdir}/stall.status"
	printf 'state=observed\n' > "${status_file}"
	state="$(read_codex_stall_guard_state "${status_file}")"
	assert_eq 'observed' "${state}" 'observed state should parse'
	printf 'state=unknown\n' > "${status_file}"
	if read_codex_stall_guard_state "${status_file}" >/dev/null 2>&1; then
		echo 'unexpected success parsing unknown stall state' >&2
		exit 1
	fi
	trap - RETURN
	rm -rf "${tmpdir}"
}

test_codex_stall_guard_kill_detected()
{
	if ! codex_stall_guard_kill_detected 143 killed; then
		echo 'expected killed stall state to be detected' >&2
		exit 1
	fi
	if codex_stall_guard_kill_detected 0 observed; then
		echo 'unexpected kill detection for observed stall state' >&2
		exit 1
	fi
}

test_resolve_editor_network_probe_pid()
{
	local wrapper_pid probe_pid child_pid
	bash -c 'sleep 30 & wait' &
	wrapper_pid=$!
	trap 'kill "${wrapper_pid}" 2>/dev/null || true; wait "${wrapper_pid}" 2>/dev/null || true' RETURN
	sleep 1
	probe_pid="$(resolve_editor_network_probe_pid "${wrapper_pid}")"
	child_pid="$(ps -o pid= --ppid "${wrapper_pid}" 2>/dev/null | awk 'NR==1 { print $1; exit }')"
	assert_eq "${child_pid}" "${probe_pid}" 'network probe pid should prefer the first child pid'
	trap - RETURN
	kill "${wrapper_pid}" 2>/dev/null || true
	wait "${wrapper_pid}" 2>/dev/null || true
}

test_resolve_editor_network_probe_pid_falls_back_to_wrapper_pid()
{
	local wrapper_pid probe_pid
	bash -c 'exec sleep 30' &
	wrapper_pid=$!
	trap 'kill "${wrapper_pid}" 2>/dev/null || true; wait "${wrapper_pid}" 2>/dev/null || true' RETURN
	sleep 1
	probe_pid="$(resolve_editor_network_probe_pid "${wrapper_pid}")"
	assert_eq "${wrapper_pid}" "${probe_pid}" 'network probe pid should fall back to the wrapper pid when no child exists'
	trap - RETURN
	kill "${wrapper_pid}" 2>/dev/null || true
	wait "${wrapper_pid}" 2>/dev/null || true
}

test_reap_editor_fifo_holders_missing_path()
{
	_reap_editor_fifo_holders '/tmp/does-not-exist-watchdog-helper' TERM
}

test_read_codex_stall_guard_state
test_codex_stall_guard_kill_detected
test_resolve_editor_network_probe_pid
test_resolve_editor_network_probe_pid_falls_back_to_wrapper_pid
test_reap_editor_fifo_holders_missing_path
echo "test_watchdog_helpers.sh: PASS"
