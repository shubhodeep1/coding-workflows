#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../_lib/tap_helpers.sh"

echo "1..1"

set +e
python3 "${SCRIPT_DIR}/_lib/import_audit.py"
audit_rc=$?
set -e

if [ "${audit_rc}" -ne 0 ]; then
	tap_not_ok 1 "import audit subprocess isolation"
	echo "# import audit failed"
	exit 1
fi

tap_ok 1 "import audit subprocess isolation"
