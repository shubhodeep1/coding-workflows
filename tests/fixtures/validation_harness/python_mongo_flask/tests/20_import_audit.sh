#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../_lib/tap_helpers.sh"

COMPOSE_FILE="${COMPOSE_FILE:-validation/docker-compose.test.yml}"
APP_SERVICE="${APP_SERVICE:-app}"
CONTAINER_IMPORT_AUDIT="/workspace/validation/tests/_lib/import_audit.py"

echo "1..1"

set +e
audit_output="$(docker compose -f "${COMPOSE_FILE}" exec -T "${APP_SERVICE}" /bin/sh -c "python3 ${CONTAINER_IMPORT_AUDIT}" 2>&1)"
audit_rc=$?
set -e

if [ "${audit_rc}" -ne 0 ]; then
	tap_not_ok 1 "import audit subprocess isolation"
	if [ -n "${audit_output}" ]; then
		while IFS= read -r line; do
			echo "# ${line}"
		done <<EOF
${audit_output}
EOF
	fi
	echo "# import audit failed inside app container"
	exit 1
fi

tap_ok 1 "import audit subprocess isolation"
