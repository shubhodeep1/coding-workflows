#!/usr/bin/env bash
# heartbeat_exec.sh — Activity-based timeout wrapper for long-running processes.
#
# Monitors a heartbeat file and kills the watched process group if no
# output activity is detected for IDLE_TIMEOUT seconds.  The caller is
# responsible for updating the heartbeat file (e.g. via the
# heartbeat_tee helper function below).
#
# Usage:
#   source scripts/heartbeat_exec.sh   # import functions
#
# Functions provided:
#   heartbeat_start [--idle-timeout SECS] [--max-wall SECS]
#       Starts the background watchdog.  Sets HEARTBEAT_FILE env var.
#       Returns the watchdog PID in HEARTBEAT_WATCHDOG_PID.
#
#   heartbeat_stop
#       Stops the watchdog and cleans up.
#
#   heartbeat_tee FILE
#       Reads stdin, writes to FILE, copies to stdout, and touches
#       the heartbeat file on every line.  Use in process substitution:
#         cmd 2> >(heartbeat_tee logfile.txt >&2)
#
#   heartbeat_touch
#       Manually update the heartbeat timestamp (e.g. after a meaningful event).
#
# Defaults (override via env or flags):
#   HEARTBEAT_IDLE_TIMEOUT  — idle seconds before kill  (default: 900 = 15 min)
#   HEARTBEAT_MAX_WALL      — max wall-clock seconds    (default: 10800 = 3 h)

HEARTBEAT_IDLE_TIMEOUT="${HEARTBEAT_IDLE_TIMEOUT:-900}"
HEARTBEAT_MAX_WALL="${HEARTBEAT_MAX_WALL:-10800}"
HEARTBEAT_FILE=""
HEARTBEAT_WATCHDOG_PID=""
_HEARTBEAT_START_TIME=""

heartbeat_touch() {
  [ -n "${HEARTBEAT_FILE}" ] && date +%s > "${HEARTBEAT_FILE}"
}

heartbeat_tee() {
  # Usage: heartbeat_tee [OUTPUT_FILE]
  # Reads stdin line-by-line, writes to OUTPUT_FILE (if given) and stdout,
  # and updates the heartbeat timestamp on each line.
  local outfile="${1:-}"
  if [ -n "$outfile" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      heartbeat_touch
      printf '%s\n' "$line"
      printf '%s\n' "$line" >> "$outfile"
    done
  else
    while IFS= read -r line || [ -n "$line" ]; do
      heartbeat_touch
      printf '%s\n' "$line"
    done
  fi
}

heartbeat_start() {
  local idle_timeout="${HEARTBEAT_IDLE_TIMEOUT}"
  local max_wall="${HEARTBEAT_MAX_WALL}"

  while [ $# -gt 0 ]; do
    case "$1" in
      --idle-timeout) idle_timeout="$2"; shift 2 ;;
      --max-wall)     max_wall="$2";     shift 2 ;;
      *)              shift ;;
    esac
  done

  HEARTBEAT_FILE="$(mktemp /tmp/heartbeat.XXXXXX)"
  date +%s > "${HEARTBEAT_FILE}"
  _HEARTBEAT_START_TIME="$(date +%s)"

  # Get the caller's process group so we can kill the whole step on timeout
  local watch_pgid="$$"

  (
    while true; do
      sleep 10

      now="$(date +%s)"
      last_activity="$(cat "${HEARTBEAT_FILE}" 2>/dev/null || echo "$now")"

      idle_secs=$(( now - last_activity ))
      wall_secs=$(( now - _HEARTBEAT_START_TIME ))

      if [ "$idle_secs" -ge "$idle_timeout" ]; then
        echo "" >&2
        echo "::error::heartbeat_exec: no output for ${idle_secs}s (limit: ${idle_timeout}s) — terminating" >&2
        kill -TERM -"${watch_pgid}" 2>/dev/null || kill -TERM "${watch_pgid}" 2>/dev/null
        sleep 10
        kill -KILL -"${watch_pgid}" 2>/dev/null || kill -KILL "${watch_pgid}" 2>/dev/null
        exit 142
      fi

      if [ "$wall_secs" -ge "$max_wall" ]; then
        echo "" >&2
        echo "::error::heartbeat_exec: max wall time ${max_wall}s exceeded — terminating" >&2
        kill -TERM -"${watch_pgid}" 2>/dev/null || kill -TERM "${watch_pgid}" 2>/dev/null
        sleep 10
        kill -KILL -"${watch_pgid}" 2>/dev/null || kill -KILL "${watch_pgid}" 2>/dev/null
        exit 143
      fi
    done
  ) &
  HEARTBEAT_WATCHDOG_PID=$!

  echo "heartbeat_exec: watchdog started (idle=${idle_timeout}s, max_wall=${max_wall}s, pid=${HEARTBEAT_WATCHDOG_PID})"
}

heartbeat_stop() {
  if [ -n "${HEARTBEAT_WATCHDOG_PID}" ]; then
    kill "${HEARTBEAT_WATCHDOG_PID}" 2>/dev/null
    wait "${HEARTBEAT_WATCHDOG_PID}" 2>/dev/null
    HEARTBEAT_WATCHDOG_PID=""
  fi
  if [ -n "${HEARTBEAT_FILE}" ]; then
    rm -f "${HEARTBEAT_FILE}"
    HEARTBEAT_FILE=""
  fi
}
