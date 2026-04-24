#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../_lib/tap_helpers.sh"

CUSTOM_TEST_COMMAND='python3 scripts/render_validation_templates.py --help >/dev/null'

echo "1..1"

set +e
/bin/sh -c "${CUSTOM_TEST_COMMAND}"
custom_rc=$?
set -e

if [ "${custom_rc}" -ne 0 ]; then
	tap_not_ok 1 "custom validation command 01"
	echo "# custom test command failed: ${CUSTOM_TEST_COMMAND}"
	exit 1
fi

tap_ok 1 "custom validation command 01"
