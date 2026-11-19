#!/usr/bin/env bash
set -euo pipefail

# Stop a background process and wait with bounded timeout.
graceful_shutdown()
{
	local pid="$1"
	local timeout_secs="${2:-15}"
	local log_file="${3:-}"
	local grace_elapsed=0

	if [ -z "${pid}" ] || ! kill -0 "${pid}" 2>/dev/null; then
		return 0
	fi

	kill -TERM "${pid}" 2>/dev/null || true
	while kill -0 "${pid}" 2>/dev/null; do
		if [ "${grace_elapsed}" -ge "${timeout_secs}" ]; then
			if [ -n "${log_file}" ] && [ -f "${log_file}" ]; then
				echo "# graceful_shutdown timeout after ${timeout_secs}s (tail ${TAIL_LINES:-40})"
				tail -n "${TAIL_LINES:-40}" "${log_file}" 2>/dev/null || true
			fi
			kill -KILL "${pid}" 2>/dev/null || true
			break
		fi
		sleep 1
		grace_elapsed=$((grace_elapsed + 1))
	done

	wait "${pid}" 2>/dev/null || true
}
