#!/usr/bin/env python3
"""Contract tests for template-mode routing in scripts/validate_process.sh."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS_PATH = REPO_ROOT / "scripts" / "validate_process.sh"


def _validate_process_text() -> str:
	return VALIDATE_PROCESS_PATH.read_text(encoding="utf-8")


def test_template_mode_selection_contract_present() -> None:
	text = _validate_process_text()
	assert 'VALIDATION_USE_TEMPLATES="${VALIDATION_USE_TEMPLATES:-false}"' in text
	assert 'VALIDATION_USE_TEMPLATES_ENABLED="false"' in text
	assert "case \"$(printf '%s' \"${VALIDATION_USE_TEMPLATES}\" | tr '[:upper:]' '[:lower:]')\" in" in text
	assert 'if [ "${VALIDATION_USE_TEMPLATES_ENABLED}" = "true" ]; then' in text
	assert 'HARNESS_MODE="template_generate"' in text
	assert 'HARNESS_GENERATOR_MODE="templates"' in text
	assert 'PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED="false"' in text
	assert 'attempt_render_recovery_after_preflight_failure()' in text
	assert 'if attempt_render_recovery_after_preflight_failure; then' in text
	assert 'attempt_self_heal_and_reexec "render"' in text
	assert 'elif [ "${VALIDATION_CYCLE}" -gt 1 ] \\' in text
	assert 'HARNESS_GENERATOR_MODE="freehand"' in text


def test_template_mode_missing_manifest_returns_harness_error() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-template-mode-") as td:
		tmp_path = Path(td)
		status_file = tmp_path / "status.json"
		metadata_file = tmp_path / "metadata.json"

		harness_script = """#!/usr/bin/env bash
set -euo pipefail

run_template_validation_harness_renderer() {
	local manifest_path=".ai/validate.yml"
	local renderer_script="scripts/render_validation_templates.py"
	local schema_path="scripts/templates/slot_manifest.schema.json"
	local templates_root="workflow-templates/validation-harness"
	local renderer_summary=""

	HARNESS_GENERATOR_MODE="templates"

	if [ ! -f "${manifest_path}" ]; then
		return 10
	fi
	if [ ! -f "${renderer_script}" ]; then
		return 11
	fi
	if [ ! -f "${schema_path}" ]; then
		return 12
	fi
	if [ ! -d "${templates_root}" ]; then
		return 13
	fi

	if ! renderer_summary="$(python3 "${renderer_script}" \
		--manifest "${manifest_path}" \
		--schema "${schema_path}" \
		--templates-root "${templates_root}" \
		--output-root validation 2>&1)"; then
		printf '%s\n' "${renderer_summary}" >> "${GENERATE_LOG_FILE}"
		return 14
	fi

	if [ -n "${renderer_summary}" ]; then
		printf '%s\n' "${renderer_summary}" >> "${GENERATE_LOG_FILE}"
	fi

	if [ -d validation/tests ]; then
		find validation/tests -type f -name '*.sh' -exec chmod +x {} +
	fi

	return 0
}

write_result_files() {
	local status="$1"
	local summary="$2"
	local failure_summary="$3"
	local raw_status="${4:-${status}}"

	jq -n \
		--arg status "${status}" \
		--arg raw_status "${raw_status}" \
		--arg summary "${summary}" \
		--arg failure_summary "${failure_summary}" \
		'{status: $status, raw_status: $raw_status, summary: $summary, failure_summary: $failure_summary}' > "${STATUS_FILE}"

	jq -n \
		--arg harness_mode "${HARNESS_MODE}" \
		--arg harness_generator_mode "${HARNESS_GENERATOR_MODE}" \
		'{harness_mode: $harness_mode, harness_generator_mode: $harness_generator_mode}' > "${METADATA_FILE}"
}

post_tracking_comment() { :; }
set_tracking_phase_label() { :; }
tg_notify() { :; }

HARNESS_MODE="template_generate"
HARNESS_GENERATOR_MODE="templates"
GENERATE_LOG_FILE="${RUNTIME_DIR}/validate_generate.log"

		if run_template_validation_harness_renderer; then
			renderer_exit=0
		else
			renderer_exit=$?
		fi

case "${renderer_exit}" in
	0)
		exit 0
		;;
	10)
		local_failure_summary="Template mode requires ${PWD}/.ai/validate.yml but it is missing. Add manifest config or disable VALIDATION_USE_TEMPLATES."
		post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}\n\nTemplate mode is enabled and does not fall back to freehand generation."
		set_tracking_phase_label "ai:validation-failed"
		write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
		tg_notify "Validation harness generation failed" "ERROR"
		exit 1
		;;
	*)
		local_failure_summary="Template renderer failed while generating validation assets."
		write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
		exit 1
		;;
esac
"""

		script_path = tmp_path / "template_missing_manifest_harness.sh"
		script_path.write_text(harness_script, encoding="utf-8")
		script_path.chmod(0o755)

		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)

		env = os.environ.copy()
		env["RUNTIME_DIR"] = str(runtime_dir)
		env["STATUS_FILE"] = str(status_file)
		env["METADATA_FILE"] = str(metadata_file)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(tmp_path),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

		assert proc.returncode == 1, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert status_file.exists()
		assert metadata_file.exists()
		status_payload = status_file.read_text(encoding="utf-8")
		metadata_payload = metadata_file.read_text(encoding="utf-8")
		assert '"raw_status": "harness_error"' in status_payload
		assert '.ai/validate.yml but it is missing' in status_payload
		assert '"harness_mode": "template_generate"' in metadata_payload
		assert '"harness_generator_mode": "templates"' in metadata_payload


def test_render_recovery_lint_gate_contract_present() -> None:
	text = _validate_process_text()
	assert 'if [ "${PRE_FLIGHT_FAILURE_CLASS:-non_lint}" != "lint" ]; then' in text
	assert 'Render recovery: skipping deterministic rerender because pre-flight failure class=${PRE_FLIGHT_FAILURE_CLASS:-unknown}.' in text


def main() -> int:
	test_template_mode_selection_contract_present()
	test_template_mode_missing_manifest_returns_harness_error()
	test_render_recovery_lint_gate_contract_present()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
