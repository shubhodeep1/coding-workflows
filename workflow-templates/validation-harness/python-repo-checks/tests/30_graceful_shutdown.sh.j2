#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../_lib/tap_helpers.sh"

COMPOSE_FILE="${COMPOSE_FILE:-validation/docker-compose.test.yml}"
APP_SERVICE="${APP_SERVICE:-app}"
SHUTDOWN_TIMEOUT_SECONDS="${SHUTDOWN_TIMEOUT_SECONDS:-20}"
SHUTDOWN_POLL_SECONDS="${SHUTDOWN_POLL_SECONDS:-1}"
GRACEFUL_SHUTDOWN_LOG_TAIL_LINES="${GRACEFUL_SHUTDOWN_LOG_TAIL_LINES:-40}"

echo "1..1"

set +e
probe_pid="$(docker compose -f "${COMPOSE_FILE}" exec -T "${APP_SERVICE}" /bin/sh -c 'sleep 300 >/tmp/repo-check-sleep.log 2>&1 & echo $!')"
spawn_rc=$?
set -e

if [ "${spawn_rc}" -ne 0 ] || ! printf '%s' "${probe_pid}" | grep -Eq '^[0-9]+$'; then
	tap_not_ok 1 "graceful shutdown helper can terminate in-container process"
	echo "# unable to spawn probe process in app container"
	exit 1
fi

set +e
python3 "${SCRIPT_DIR}/_lib/graceful_shutdown.py" \
	--pid "${probe_pid}" \
	--compose-file "${COMPOSE_FILE}" \
	--app-service "${APP_SERVICE}" \
	--timeout-seconds "${SHUTDOWN_TIMEOUT_SECONDS}" \
	--poll-seconds "${SHUTDOWN_POLL_SECONDS}" \
	--log-tail-lines "${GRACEFUL_SHUTDOWN_LOG_TAIL_LINES}"
shutdown_rc=$?
set -e

if [ "${shutdown_rc}" -ne 0 ]; then
	tap_not_ok 1 "graceful shutdown helper can terminate in-container process"
	echo "# graceful shutdown probe failed for pid=${probe_pid}"
	exit 1
fi

tap_ok 1 "graceful shutdown helper can terminate in-container process"
