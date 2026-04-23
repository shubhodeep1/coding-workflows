#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CHECK_COMMANDS=(
	"python3 tests/test_render_validation_templates.py"
	"python3 tests/test_validate_process_template_mode.py"
	"python3 tests/test_validate_workflow_validate_bootstrap.py"
)

if [ "$#" -gt 0 ]; then
	CHECK_COMMANDS=("$@")
fi

for check_cmd in "${CHECK_COMMANDS[@]}"; do
	echo "# repo-check start: ${check_cmd}"
	if ! (cd "${ROOT_DIR}" && /bin/sh -c "${check_cmd}"); then
		echo "# repo-check failed: ${check_cmd}" >&2
		exit 1
	fi
	echo "# repo-check ok: ${check_cmd}"
done
