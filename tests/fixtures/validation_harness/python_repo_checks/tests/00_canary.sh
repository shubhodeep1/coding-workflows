#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../_lib/tap_helpers.sh"

COMPOSE_FILE="${COMPOSE_FILE:-validation/docker-compose.test.yml}"
APP_SERVICE="${APP_SERVICE:-app}"
DEFAULT_CANARY_TOOLS=(
	'bash'
	'python3'
	'jq'
)

if [ "${CANARY_TOOLS+x}" = "x" ]; then
	read -r -a TOOL_LIST <<< "${CANARY_TOOLS}"
else
	TOOL_LIST=("${DEFAULT_CANARY_TOOLS[@]}")
fi

tool_count=0
for _tool in "${TOOL_LIST[@]}"; do
	tool_count=$((tool_count + 1))
done
echo "1..${tool_count}"

test_id=1
for tool in "${TOOL_LIST[@]}"; do
	if ! docker compose -f "${COMPOSE_FILE}" exec -T "${APP_SERVICE}" /bin/sh -c 'command -v "$1" >/dev/null 2>&1' sh "${tool}"; then
		tap_not_ok "${test_id}" "canary tool available: ${tool}"
		echo "# missing required canary tool: ${tool}"
		exit 1
	fi
	tap_ok "${test_id}" "canary tool available: ${tool}"
	test_id=$((test_id + 1))
done
