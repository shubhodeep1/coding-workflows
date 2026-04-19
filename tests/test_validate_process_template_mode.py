#!/usr/bin/env python3
"""Contract tests for template-mode routing in scripts/validate_process.sh."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS_PATH = REPO_ROOT / "scripts" / "validate_process.sh"
SELF_HEAL_SCRIPT_PATH = REPO_ROOT / "scripts" / "self_heal_validation.sh"
SELF_HEAL_PROMPT_PATH = REPO_ROOT / "prompts" / "mode-validate-self-heal.txt"


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
		printf '%s\\n' "${renderer_summary}" >> "${GENERATE_LOG_FILE}"
		return 14
	fi

	if [ -n "${renderer_summary}" ]; then
		printf '%s\\n' "${renderer_summary}" >> "${GENERATE_LOG_FILE}"
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
		post_tracking_comment "## ⚠️ Runtime validation harness generation failed\\n\\n${local_failure_summary}\\n\\nTemplate mode is enabled and does not fall back to freehand generation."
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


def test_template_render_recovery_contract_present() -> None:
	text = _validate_process_text()
	assert "attempt_template_render_preflight_recovery()" in text
	assert 'if [ "${HARNESS_MODE}" != "template_generate" ]; then' in text
	assert 'Template ${reason} recovery attempt ${render_attempt}/${render_budget}: deterministic re-render + pre-flight rerun.' in text
	assert 'if attempt_template_render_preflight_recovery "preflight"; then' in text
	assert 'attempt_self_heal_and_reexec "render"' in text


def test_template_render_recovery_success_paths_continue() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-template-recovery-success-") as td:
		tmp_path = Path(td)
		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		marker_file = runtime_dir / "success.marker"

		harness_script = """#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="${RUNTIME_DIR:?RUNTIME_DIR is required}"
PRE_FLIGHT_LOG_FILE="${RUNTIME_DIR}/validation_preflight.log"
HARNESS_MODE="template_generate"
MAX_SELF_HEAL_ATTEMPTS=2
SELF_HEAL_ATTEMPT=0
PRE_FLIGHT_CALLS=0
RENDER_CALLS=0

run_template_validation_harness_renderer() {
	RENDER_CALLS=$((RENDER_CALLS + 1))
	return 0
}

is_validation_harness_runnable() {
	return 0
}

run_preflight_checks() {
	PRE_FLIGHT_CALLS=$((PRE_FLIGHT_CALLS + 1))
	if [ "${PRE_FLIGHT_CALLS}" -eq 1 ]; then
		echo "synthetic preflight failure" >> "${PRE_FLIGHT_LOG_FILE}"
		return 1
	fi
	return 0
}

attempt_template_render_preflight_recovery() {
	local reason="${1:-preflight}"
	local render_attempt=1
	local render_budget=1
	local renderer_exit=0

	if [ "${HARNESS_MODE}" != "template_generate" ]; then
		return 1
	fi

	if [ "${MAX_SELF_HEAL_ATTEMPTS:-0}" -gt "${SELF_HEAL_ATTEMPT:-0}" ]; then
		render_budget="$((MAX_SELF_HEAL_ATTEMPTS - SELF_HEAL_ATTEMPT))"
	fi
	if [ "${render_budget}" -lt 1 ]; then
		render_budget=1
	fi

	while [ "${render_attempt}" -le "${render_budget}" ]; do
		echo "Template ${reason} recovery attempt ${render_attempt}/${render_budget}: deterministic re-render + pre-flight rerun."

		if run_template_validation_harness_renderer; then
			renderer_exit=0
		else
			renderer_exit=$?
			echo "::warning::Template ${reason} recovery renderer failed with exit ${renderer_exit}; continuing with existing hard-fail path." >&2
			return 1
		fi

		if ! is_validation_harness_runnable; then
			echo "::warning::Template ${reason} recovery produced non-runnable validation assets; continuing with existing hard-fail path." >&2
			return 1
		fi

		if run_preflight_checks; then
			echo "Template ${reason} recovery succeeded on attempt ${render_attempt}/${render_budget}."
			return 0
		fi

		render_attempt=$((render_attempt + 1))
	done

	echo "Template ${reason} recovery exhausted deterministic attempts (${render_budget}); preserving existing hard-fail behavior."
	return 1
}

if ! run_preflight_checks; then
	if attempt_template_render_preflight_recovery "preflight"; then
		:
	else
		echo "unexpected hard fail path" >&2
		exit 40
	fi
fi

if [ "${PRE_FLIGHT_CALLS}" -ne 2 ]; then
	echo "expected two preflight attempts, got ${PRE_FLIGHT_CALLS}" >&2
	exit 41
fi
if [ "${RENDER_CALLS}" -ne 1 ]; then
	echo "expected one renderer attempt, got ${RENDER_CALLS}" >&2
	exit 42
fi
printf 'ok\n' > "${RUNTIME_DIR}/success.marker"
"""

		script_path = tmp_path / "template_render_recovery_success.sh"
		script_path.write_text(harness_script, encoding="utf-8")
		script_path.chmod(0o755)

		env = os.environ.copy()
		env["RUNTIME_DIR"] = str(runtime_dir)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(tmp_path),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert marker_file.exists()


def test_template_render_recovery_fail_open_preserves_hard_fail_path() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-template-recovery-fail-open-") as td:
		tmp_path = Path(td)
		status_file = tmp_path / "status.json"
		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)

		harness_script = """#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="${RUNTIME_DIR:?RUNTIME_DIR is required}"
PRE_FLIGHT_LOG_FILE="${RUNTIME_DIR}/validation_preflight.log"
DIAGNOSE_RESULT_FILE="${RUNTIME_DIR}/diagnose_result.json"
STATUS_FILE="${STATUS_FILE:?STATUS_FILE is required}"
HARNESS_MODE="template_generate"
MAX_SELF_HEAL_ATTEMPTS=2
SELF_HEAL_ATTEMPT=0
PRE_FLIGHT_CALLS=0
ATTEMPT_SELF_HEAL_CALLS=0

run_template_validation_harness_renderer() {
	return 11
}

is_validation_harness_runnable() {
	return 0
}

run_preflight_checks() {
	PRE_FLIGHT_CALLS=$((PRE_FLIGHT_CALLS + 1))
	echo "synthetic preflight failure" >> "${PRE_FLIGHT_LOG_FILE}"
	return 1
}

attempt_template_render_preflight_recovery() {
	local reason="${1:-preflight}"
	local render_attempt=1
	local render_budget=1
	local renderer_exit=0

	if [ "${HARNESS_MODE}" != "template_generate" ]; then
		return 1
	fi

	if [ "${MAX_SELF_HEAL_ATTEMPTS:-0}" -gt "${SELF_HEAL_ATTEMPT:-0}" ]; then
		render_budget="$((MAX_SELF_HEAL_ATTEMPTS - SELF_HEAL_ATTEMPT))"
	fi
	if [ "${render_budget}" -lt 1 ]; then
		render_budget=1
	fi

	while [ "${render_attempt}" -le "${render_budget}" ]; do
		echo "Template ${reason} recovery attempt ${render_attempt}/${render_budget}: deterministic re-render + pre-flight rerun."

		if run_template_validation_harness_renderer; then
			renderer_exit=0
		else
			renderer_exit=$?
			echo "::warning::Template ${reason} recovery renderer failed with exit ${renderer_exit}; continuing with existing hard-fail path." >&2
			return 1
		fi

		if ! is_validation_harness_runnable; then
			echo "::warning::Template ${reason} recovery produced non-runnable validation assets; continuing with existing hard-fail path." >&2
			return 1
		fi

		if run_preflight_checks; then
			echo "Template ${reason} recovery succeeded on attempt ${render_attempt}/${render_budget}."
			return 0
		fi

		render_attempt=$((render_attempt + 1))
	done

	echo "Template ${reason} recovery exhausted deterministic attempts (${render_budget}); preserving existing hard-fail behavior."
	return 1
}

attempt_self_heal_and_reexec() {
	local phase="${1:-unknown}"
	ATTEMPT_SELF_HEAL_CALLS=$((ATTEMPT_SELF_HEAL_CALLS + 1))
	printf '%s\n' "${phase}" > "${RUNTIME_DIR}/self_heal_phase.txt"
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
}

post_tracking_comment() { :; }
set_tracking_phase_label() { :; }
tg_notify() { :; }

if ! run_preflight_checks; then
	if attempt_template_render_preflight_recovery "preflight"; then
		:
	else
		failure_summary="Validation pre-flight checks failed. See validation_preflight.log artifact."
		jq -n \
			--arg diagnosis "Pre-flight validation failed before test execution." \
			--arg harness_fixes "$(tail -n 120 "${PRE_FLIGHT_LOG_FILE}" 2>/dev/null || true)" \
			'{status: "harness_error", diagnosis: $diagnosis, fix_issues: [], harness_fixes: $harness_fixes}' > "${DIAGNOSE_RESULT_FILE}"

		if [ "${HARNESS_MODE}" = "template_generate" ]; then
			attempt_self_heal_and_reexec "render"
		else
			attempt_self_heal_and_reexec "preflight"
		fi
		write_result_files "fail" "Validation failed due to harness pre-flight error" "${failure_summary}" "harness_error"
		exit 0
	fi
fi

exit 99
"""

		script_path = tmp_path / "template_render_recovery_fail_open.sh"
		script_path.write_text(harness_script, encoding="utf-8")
		script_path.chmod(0o755)

		env = os.environ.copy()
		env["RUNTIME_DIR"] = str(runtime_dir)
		env["STATUS_FILE"] = str(status_file)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(tmp_path),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert status_file.exists()
		status_payload = status_file.read_text(encoding="utf-8")
		assert '"raw_status": "harness_error"' in status_payload
		phase_payload = (runtime_dir / "self_heal_phase.txt").read_text(encoding="utf-8").strip()
		assert phase_payload == "render"


def test_self_heal_allows_render_phase_and_guardrails_remain() -> None:
	validate_text = _validate_process_text()
	self_heal_text = SELF_HEAL_SCRIPT_PATH.read_text(encoding="utf-8")
	prompt_text = SELF_HEAL_PROMPT_PATH.read_text(encoding="utf-8")

	assert 'attempt_self_heal_and_reexec "render"' in validate_text
	assert '("generate"|"preflight"|"render"|"canary"|"diagnose"|"runtime"|"discover")' in self_heal_text
	assert 'failing_phase: ${SELF_HEAL_FAILURE_PHASE}' in self_heal_text
	assert 'ALLOWED_TARGETS=(' in self_heal_text
	for target in (
		'"mode-validate-discover.txt"',
		'"mode-validate-generate.txt"',
		'"mode-validate-fix-harness.txt"',
		'"mode-validate-diagnose.txt"',
	):
		assert target in self_heal_text
	assert 'self-heal: refusing — patch contains deletion lines; self-heal patches must be additive-only' in self_heal_text
	assert 'self-heal: refusing — patch includes non-additive file operations (rename/copy/delete/new)' in self_heal_text
	assert 'You may ONLY edit these four files:' in prompt_text
	assert 'Deterministic template renderer recovery is handled in `scripts/validate_process.sh` outside this prompt.' in prompt_text
	assert '`failing_phase` from runtime metadata: `render` means deterministic re-render + re-lint has already been attempted' in prompt_text


def main() -> int:
	test_template_mode_selection_contract_present()
	test_template_mode_missing_manifest_returns_harness_error()
	test_template_render_recovery_contract_present()
	test_template_render_recovery_success_paths_continue()
	test_template_render_recovery_fail_open_preserves_hard_fail_path()
	test_self_heal_allows_render_phase_and_guardrails_remain()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
