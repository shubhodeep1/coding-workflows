#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-validation/docker-compose.test.yml}"
APP_SERVICE="${APP_SERVICE:-app}"

echo "1..1"

set +e
docker compose -f "${COMPOSE_FILE}" exec -T "${APP_SERVICE}" /bin/sh -c 'python3 scripts/render_validation_templates.py --manifest .ai/validate.yml --schema scripts/templates/slot_manifest.schema.json --templates-root workflow-templates/validation-harness --output-root /tmp/validation-render-smoke' >/dev/null 2>&1
render_rc=$?
set -e

if [ "${render_rc}" -ne 0 ]; then
	echo "not ok 1 - render validation templates smoke"
	echo "# python3 scripts/render_validation_templates.py failed"
	exit 1
fi

echo "ok 1 - render validation templates smoke"
