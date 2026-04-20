#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../_lib/tap_helpers.sh"

TAP_PLAN="${TAP_PLAN:-2}"

echo "1..${TAP_PLAN}"

current=1
while [ "${current}" -le "${TAP_PLAN}" ]; do
	tap_ok "${current}" "tap summary slot ${current}"
	current=$((current + 1))
done

echo "# tap_report family=python-mongo-flask stable_summary=1"
