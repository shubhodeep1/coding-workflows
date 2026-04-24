#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../_lib/tap_helpers.sh"

echo "1..1"
tap_ok 1 "python-mongo-flask family marker for demo-project-non-flask"
