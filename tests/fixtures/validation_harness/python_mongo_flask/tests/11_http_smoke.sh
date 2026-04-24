#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../_lib/tap_helpers.sh"

APP_PORT="${APP_PORT:-8000}"
HEALTH_PATH="${HEALTH_PATH:-/health}"
TEST_HOST_HEADER="${TEST_HOST_HEADER:-app.local.test}"
APP_URL="${APP_URL:-http://127.0.0.1:${APP_PORT}${HEALTH_PATH}}"
HTTP_SMOKE_TIMEOUT_SECONDS="${HTTP_SMOKE_TIMEOUT_SECONDS:-30}"

echo "1..1"

set +e
python3 "${SCRIPT_DIR}/_lib/http_smoke.py" \
	--url "${APP_URL}" \
	--host-header "${TEST_HOST_HEADER}" \
	--timeout-seconds "${HTTP_SMOKE_TIMEOUT_SECONDS}"
smoke_rc=$?
set -e

if [ "${smoke_rc}" -ne 0 ]; then
	tap_not_ok 1 "http smoke reachable with Flask host header override"
	echo "# http smoke failed for ${APP_URL} with Host=${TEST_HOST_HEADER}"
	exit 1
fi

tap_ok 1 "http smoke reachable with Flask host header override"
