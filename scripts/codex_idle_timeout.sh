#!/usr/bin/env bash
# codex_idle_timeout.sh — Run a command and kill it if stderr is idle too long.
#
# Codex prints progress (tool calls, model responses) to stderr while working.
# When an upstream API call hangs, stderr goes silent. This wrapper monitors
# stderr activity and terminates the process only when it has been truly idle
# for the configured duration — a legitimately busy 60-minute run is fine as
# long as stderr keeps flowing.
#
# Usage:
#   codex_idle_timeout.sh <idle_seconds> <command> [args...]
#
# Exit codes:
#   Same as the wrapped command on normal exit.
#   124  — killed due to idle timeout (matches coreutils timeout convention).

set -uo pipefail

if [ $# -lt 2 ]; then
  echo "Usage: codex_idle_timeout.sh <idle_seconds> <command> [args...]" >&2
  exit 1
fi

IDLE_LIMIT="$1"; shift

STDERR_LOG="$(mktemp)"
trap 'rm -f "${STDERR_LOG}"' EXIT

# Run the command in the background.
# Stderr is tee'd to a temp file (for size monitoring) AND passed through to
# the real stderr so that GitHub Actions logs are preserved.
"$@" 2> >(tee "${STDERR_LOG}" >&2) &
CMD_PID=$!

POLL_INTERVAL=10
last_size=0
idle_secs=0

while kill -0 "${CMD_PID}" 2>/dev/null; do
  sleep "${POLL_INTERVAL}"
  cur_size="$(stat -c%s "${STDERR_LOG}" 2>/dev/null || echo 0)"
  if [ "${cur_size}" != "${last_size}" ]; then
    last_size="${cur_size}"
    idle_secs=0
  else
    idle_secs=$((idle_secs + POLL_INTERVAL))
  fi
  if [ "${idle_secs}" -ge "${IDLE_LIMIT}" ]; then
    echo "::warning::Process idle for ${IDLE_LIMIT}s with no stderr output — terminating (pid ${CMD_PID})." >&2
    kill -TERM "${CMD_PID}" 2>/dev/null
    # Give it 30s to clean up, then force-kill.
    for _ in $(seq 1 6); do
      sleep 5
      kill -0 "${CMD_PID}" 2>/dev/null || break
    done
    kill -9 "${CMD_PID}" 2>/dev/null || true
    wait "${CMD_PID}" 2>/dev/null || true
    exit 124
  fi
done

wait "${CMD_PID}"
