#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../_lib/tap_helpers.sh"

echo "1..1"

set +e
if ! command -v python3 >/dev/null 2>&1; then
	echo "ok 1 - import audit subprocess isolation # SKIP python3 not available in image"
	exit 0
fi

if [ ! -f "${SCRIPT_DIR}/_lib/import_audit.py" ]; then
	tap_not_ok 1 "import audit subprocess isolation"
	echo "# import_audit.py missing"
	exit 1
fi

# Keep TAP output stable: helper-level per-module success lines are non-TAP diagnostics.
python3 "${SCRIPT_DIR}/_lib/import_audit.py" >/dev/null
audit_rc=$?
set -e

if [ "${audit_rc}" -ne 0 ]; then
	tap_not_ok 1 "import audit subprocess isolation"
	echo "# import audit failed"
	exit 1
fi

tap_ok 1 "import audit subprocess isolation"
