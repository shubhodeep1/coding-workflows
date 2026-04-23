#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/logs}"
COMPOSE_FILE="${COMPOSE_FILE:-validation/docker-compose.test.yml}"
APP_SERVICE="${APP_SERVICE:-app}"
mkdir -p "${LOG_DIR}"
TEST_LOG="${LOG_DIR}/repo_local_validation.log"

# shellcheck source=/dev/null
. "${ROOT_DIR}/_lib/graceful_shutdown.sh"

echo "1..1"

set +e
docker compose -f "${COMPOSE_FILE}" exec -T "${APP_SERVICE}" /bin/sh -c "PYTHONDONTWRITEBYTECODE=1 python3 tests/test_validation_selftest_runner.py" >"${TEST_LOG}" 2>&1 &
TEST_PID=$!

(
	sleep "300"
	graceful_shutdown "${TEST_PID}" "3" "${TEST_LOG}"
) &
WATCHDOG_PID=$!

wait "${TEST_PID}"
TEST_STATUS=$?

kill "${WATCHDOG_PID}" 2>/dev/null || true
wait "${WATCHDOG_PID}" 2>/dev/null || true
set -e

if [ "${TEST_STATUS}" -ne 0 ]; then
	echo "not ok 1 - validation python test suite"
	echo "# command failed: PYTHONDONTWRITEBYTECODE=1 python3 tests/test_validation_selftest_runner.py"
	echo "# log tail (${TAIL_LINES:-40} lines):"
	tail -n "${TAIL_LINES:-40}" "${TEST_LOG}" 2>/dev/null || true
	exit 1
fi

echo "ok 1 - validation python test suite"
