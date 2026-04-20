#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../_lib/tap_helpers.sh"

CANARY_TOOLS=(
	'curl'
	'jq'
	'python3'
)

echo "1..3"

test_id=1
for tool in "${CANARY_TOOLS[@]}"; do
	if ! command -v "${tool}" >/dev/null 2>&1; then
		tap_not_ok "${test_id}" "canary tool available: ${tool}"
		echo "# missing required canary tool in app container: ${tool}"
		exit 1
	fi
	tap_ok "${test_id}" "canary tool available: ${tool}"
	test_id=$((test_id + 1))
done
