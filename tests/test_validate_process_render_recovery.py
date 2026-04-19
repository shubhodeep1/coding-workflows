#!/usr/bin/env python3
"""Contract tests for template-mode render recovery in validate_process.sh."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS_PATH = REPO_ROOT / "scripts" / "validate_process.sh"


def _validate_process_text() -> str:
	return VALIDATE_PROCESS_PATH.read_text(encoding="utf-8")


def test_render_recovery_contract_present() -> None:
	text = _validate_process_text()
	assert "run_preflight_render_recovery()" in text
	assert "VALIDATION_RENDER_RECOVERY phase=render status=start" in text
	assert "VALIDATION_RENDER_RECOVERY phase=render status=renderer_failed" in text
	assert "VALIDATION_RENDER_RECOVERY phase=render status=rerender_pass rerun=preflight" in text
	assert "VALIDATION_RENDER_RECOVERY phase=render status=relint_pass" in text
	assert "VALIDATION_RENDER_RECOVERY phase=render status=relint_failed" in text
	assert 'if [ "${VALIDATION_USE_TEMPLATES_ENABLED}" = "true" ] && run_preflight_render_recovery; then' in text
	assert 'attempt_self_heal_and_reexec "render"' in text


def test_template_preflight_failure_triggers_render_phase_self_heal() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-render-recovery-fail-") as td:
		tmp_path = Path(td)
		status_file = tmp_path / "status.json"
		phase_file = tmp_path / "phase.txt"

		harness_script = textwrap.dedent(
			"""#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="${RUNTIME_DIR}"
VALIDATION_USE_TEMPLATES_ENABLED="true"
PRE_FLIGHT_LOG_FILE="${RUNTIME_DIR}/validation_preflight.log"
GENERATE_LOG_FILE="${RUNTIME_DIR}/validate_generate.log"
DIAGNOSE_RESULT_FILE="${RUNTIME_DIR}/validation_diagnosis.json"
STATUS_FILE="${STATUS_FILE}"
PHASE_FILE="${PHASE_FILE}"
PRE_FLIGHT_CHECK_CALLS=0

run_template_validation_harness_renderer() {
	return 11
}

run_preflight_checks() {
	PRE_FLIGHT_CHECK_CALLS=$((PRE_FLIGHT_CHECK_CALLS + 1))
	echo "preflight call ${PRE_FLIGHT_CHECK_CALLS}" >> "${PRE_FLIGHT_LOG_FILE}"
	return 1
}

run_preflight_render_recovery() {
	local renderer_exit=0

	echo "VALIDATION_RENDER_RECOVERY phase=render status=start" >> "${GENERATE_LOG_FILE}"
	if run_template_validation_harness_renderer; then
		renderer_exit=0
	else
		renderer_exit=$?
	fi

	if [ "${renderer_exit}" -ne 0 ]; then
		echo "VALIDATION_RENDER_RECOVERY phase=render status=renderer_failed renderer_exit=${renderer_exit}" >> "${GENERATE_LOG_FILE}"
		return 1
	fi

	echo "VALIDATION_RENDER_RECOVERY phase=render status=rerender_pass rerun=preflight" >> "${GENERATE_LOG_FILE}"
	if run_preflight_checks; then
		echo "VALIDATION_RENDER_RECOVERY phase=render status=relint_pass" >> "${GENERATE_LOG_FILE}"
		return 0
	fi

	echo "VALIDATION_RENDER_RECOVERY phase=render status=relint_failed" >> "${GENERATE_LOG_FILE}"
	return 1
}

attempt_self_heal_and_reexec() {
	printf '%s\n' "$1" > "${PHASE_FILE}"
}

post_tracking_comment() { :; }
set_tracking_phase_label() { :; }
tg_notify() { :; }

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

if ! run_preflight_checks; then
	if [ "${VALIDATION_USE_TEMPLATES_ENABLED}" = "true" ] && run_preflight_render_recovery; then
		:
	else
		failure_summary="Validation pre-flight checks failed. See validation_preflight.log artifact."
		jq -n \
			--arg diagnosis "Pre-flight validation failed before test execution." \
			--arg harness_fixes "$(tail -n 120 "${PRE_FLIGHT_LOG_FILE}" 2>/dev/null || true)" \
			'{
				status: "harness_error",
				diagnosis: $diagnosis,
				fix_issues: [],
				harness_fixes: (if ($harness_fixes | length) > 0 then $harness_fixes else "Fix validation/docker-compose.test.yml, shell syntax, or build context/dockerfile paths." end)
			}' > "${DIAGNOSE_RESULT_FILE}"

		if [ "${VALIDATION_USE_TEMPLATES_ENABLED}" = "true" ]; then
			attempt_self_heal_and_reexec "render"
		else
			attempt_self_heal_and_reexec "preflight"
		fi
		write_result_files "fail" "Validation failed due to harness pre-flight error" "${failure_summary}" "harness_error"
		exit 0
	fi
fi

exit 0
"""
		)

		script_path = tmp_path / "render_recovery_failure_harness.sh"
		script_path.write_text(harness_script, encoding="utf-8")
		script_path.chmod(0o755)

		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)

		env = os.environ.copy()
		env["RUNTIME_DIR"] = str(runtime_dir)
		env["STATUS_FILE"] = str(status_file)
		env["PHASE_FILE"] = str(phase_file)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(tmp_path),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert phase_file.exists()
		assert phase_file.read_text(encoding="utf-8").strip() == "render"
		status_payload = status_file.read_text(encoding="utf-8")
		assert '"raw_status": "harness_error"' in status_payload
		generate_log = (runtime_dir / "validate_generate.log").read_text(encoding="utf-8")
		assert "VALIDATION_RENDER_RECOVERY phase=render status=renderer_failed renderer_exit=11" in generate_log


def test_template_preflight_failure_render_recovery_can_pass_without_self_heal() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-render-recovery-pass-") as td:
		tmp_path = Path(td)
		phase_file = tmp_path / "phase.txt"

		harness_script = textwrap.dedent(
			"""#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="${RUNTIME_DIR}"
VALIDATION_USE_TEMPLATES_ENABLED="true"
PRE_FLIGHT_LOG_FILE="${RUNTIME_DIR}/validation_preflight.log"
GENERATE_LOG_FILE="${RUNTIME_DIR}/validate_generate.log"
PRE_FLIGHT_CHECK_CALLS=0

run_template_validation_harness_renderer() {
	return 0
}

run_preflight_checks() {
	PRE_FLIGHT_CHECK_CALLS=$((PRE_FLIGHT_CHECK_CALLS + 1))
	echo "preflight call ${PRE_FLIGHT_CHECK_CALLS}" >> "${PRE_FLIGHT_LOG_FILE}"
	if [ "${PRE_FLIGHT_CHECK_CALLS}" -eq 1 ]; then
		return 1
	fi
	return 0
}

run_preflight_render_recovery() {
	local renderer_exit=0

	echo "VALIDATION_RENDER_RECOVERY phase=render status=start" >> "${GENERATE_LOG_FILE}"
	if run_template_validation_harness_renderer; then
		renderer_exit=0
	else
		renderer_exit=$?
	fi

	if [ "${renderer_exit}" -ne 0 ]; then
		echo "VALIDATION_RENDER_RECOVERY phase=render status=renderer_failed renderer_exit=${renderer_exit}" >> "${GENERATE_LOG_FILE}"
		return 1
	fi

	echo "VALIDATION_RENDER_RECOVERY phase=render status=rerender_pass rerun=preflight" >> "${GENERATE_LOG_FILE}"
	if run_preflight_checks; then
		echo "VALIDATION_RENDER_RECOVERY phase=render status=relint_pass" >> "${GENERATE_LOG_FILE}"
		return 0
	fi

	echo "VALIDATION_RENDER_RECOVERY phase=render status=relint_failed" >> "${GENERATE_LOG_FILE}"
	return 1
}

attempt_self_heal_and_reexec() {
	printf '%s\n' "$1" > "${PHASE_FILE}"
}

if ! run_preflight_checks; then
	if [ "${VALIDATION_USE_TEMPLATES_ENABLED}" = "true" ] && run_preflight_render_recovery; then
		:
	else
		if [ "${VALIDATION_USE_TEMPLATES_ENABLED}" = "true" ]; then
			attempt_self_heal_and_reexec "render"
		else
			attempt_self_heal_and_reexec "preflight"
		fi
		exit 1
	fi
fi

exit 0
"""
		)

		script_path = tmp_path / "render_recovery_pass_harness.sh"
		script_path.write_text(harness_script, encoding="utf-8")
		script_path.chmod(0o755)

		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)

		env = os.environ.copy()
		env["RUNTIME_DIR"] = str(runtime_dir)
		env["PHASE_FILE"] = str(phase_file)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(tmp_path),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert not phase_file.exists(), "render recovery success must not invoke self-heal"
		generate_log = (runtime_dir / "validate_generate.log").read_text(encoding="utf-8")
		assert "VALIDATION_RENDER_RECOVERY phase=render status=rerender_pass rerun=preflight" in generate_log
		assert "VALIDATION_RENDER_RECOVERY phase=render status=relint_pass" in generate_log

