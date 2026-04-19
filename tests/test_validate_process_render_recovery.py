#!/usr/bin/env python3
"""Integration-style tests for validate preflight render recovery."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def _run_script(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	with tempfile.TemporaryDirectory(prefix="validate-render-recovery-") as td:
		tmp_path = Path(td)
		script_path = tmp_path / "render_recovery_harness.sh"
		script_path.write_text(script, encoding="utf-8")
		script_path.chmod(0o755)
		return subprocess.run(
			["bash", str(script_path)],
			cwd=str(tmp_path),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)


def test_render_recovery_success_continues_pipeline() -> None:
	script = """#!/usr/bin/env bash
set -euo pipefail

PRE_FLIGHT_LOG_FILE="${RUNTIME_DIR}/validation_preflight.log"
PRE_FLIGHT_STATUS="not_run"
PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED="false"
HARNESS_MODE="template_generate"
HARNESS_GENERATOR_MODE="templates"
SELF_HEAL_PHASE_LOG="${RUNTIME_DIR}/self_heal_phase.log"
RENDER_EXIT_SEQUENCE="0"
PRECHECK_SEQUENCE="fail,pass"

run_template_validation_harness_renderer() {
	local next="${RENDER_EXIT_SEQUENCE%%,*}"
	if [[ "${RENDER_EXIT_SEQUENCE}" == *,* ]]; then
		RENDER_EXIT_SEQUENCE="${RENDER_EXIT_SEQUENCE#*,}"
	fi
	[ -z "${next}" ] && next=0
	if [ "${next}" -eq 0 ]; then
		return 0
	fi
	return "${next}"
}

run_preflight_checks() {
	PRE_FLIGHT_STATUS="running"
	if [ "${PRE_FLIGHT_APPEND_LOG:-false}" != "true" ]; then
		: > "${PRE_FLIGHT_LOG_FILE}"
	fi
	local next="${PRECHECK_SEQUENCE%%,*}"
	if [[ "${PRECHECK_SEQUENCE}" == *,* ]]; then
		PRECHECK_SEQUENCE="${PRECHECK_SEQUENCE#*,}"
	fi
	case "${next}" in
		pass)
			PRE_FLIGHT_STATUS="pass"
			echo "preflight-pass" >> "${PRE_FLIGHT_LOG_FILE}"
			return 0
			;;
		*)
			PRE_FLIGHT_STATUS="fail"
			echo "preflight-fail" >> "${PRE_FLIGHT_LOG_FILE}"
			return 1
			;;
	esac
}

attempt_self_heal_and_reexec() {
	printf '%s\n' "$1" >> "${SELF_HEAL_PHASE_LOG}"
	return 0
}

attempt_render_recovery_after_preflight_failure()
{
	local renderer_exit=0

	if [ "${HARNESS_MODE}" != "template_generate" ] || [ "${HARNESS_GENERATOR_MODE}" != "templates" ]; then
		return 1
	fi
	if [ "${PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED:-false}" = "true" ]; then
		return 1
	fi

	PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED="true"
	{
		echo "Render recovery: deterministic template rerender triggered after pre-flight failure."
		echo "Render recovery: preserving initial pre-flight diagnostics and attempting rerender."
	} >> "${PRE_FLIGHT_LOG_FILE}"

	if run_template_validation_harness_renderer; then
		renderer_exit=0
	else
		renderer_exit=$?
	fi

	if [ "${renderer_exit}" -ne 0 ]; then
		echo "Render recovery: template rerender failed with exit=${renderer_exit}; fail-open to legacy pre-flight failure handling." >> "${PRE_FLIGHT_LOG_FILE}"
		return 2
	fi

	echo "Render recovery: rerender completed; re-running pre-flight checks." >> "${PRE_FLIGHT_LOG_FILE}"
	local PRE_FLIGHT_APPEND_LOG="true"
	if run_preflight_checks; then
		echo "Render recovery: pre-flight checks passed after deterministic rerender." >> "${PRE_FLIGHT_LOG_FILE}"
		return 0
	fi

	echo "Render recovery: pre-flight checks still failing after deterministic rerender." >> "${PRE_FLIGHT_LOG_FILE}"
	return 2
}

if ! run_preflight_checks; then
	render_recovery_exit=1
	if attempt_render_recovery_after_preflight_failure; then
		render_recovery_exit=0
	else
		render_recovery_exit=$?
	fi

	if [ "${render_recovery_exit}" -ne 0 ]; then
		if [ "${render_recovery_exit}" -eq 2 ]; then
			attempt_self_heal_and_reexec "render"
		else
			attempt_self_heal_and_reexec "preflight"
		fi
		echo "terminal-failure" > "${RUNTIME_DIR}/terminal.txt"
		exit 0
	fi
fi

echo "continued" > "${RUNTIME_DIR}/pipeline.txt"
"""

	with tempfile.TemporaryDirectory(prefix="validate-render-recovery-success-") as td:
		tmp_path = Path(td)
		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		env = os.environ.copy()
		env["RUNTIME_DIR"] = str(runtime_dir)
		proc = _run_script(script, env)
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert (runtime_dir / "pipeline.txt").read_text(encoding="utf-8").strip() == "continued"
		assert not (runtime_dir / "terminal.txt").exists()
		assert not (runtime_dir / "self_heal_phase.log").exists()
		log = (runtime_dir / "validation_preflight.log").read_text(encoding="utf-8")
		assert "preflight-fail" in log
		assert "Render recovery: deterministic template rerender triggered after pre-flight failure." in log
		assert "Render recovery: pre-flight checks passed after deterministic rerender." in log


def test_render_recovery_fail_open_uses_render_phase_self_heal() -> None:
	script = """#!/usr/bin/env bash
set -euo pipefail

PRE_FLIGHT_LOG_FILE="${RUNTIME_DIR}/validation_preflight.log"
PRE_FLIGHT_STATUS="not_run"
PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED="false"
HARNESS_MODE="template_generate"
HARNESS_GENERATOR_MODE="templates"
SELF_HEAL_PHASE_LOG="${RUNTIME_DIR}/self_heal_phase.log"
RENDER_EXIT_SEQUENCE="14"
PRECHECK_SEQUENCE="fail"

run_template_validation_harness_renderer() {
	local next="${RENDER_EXIT_SEQUENCE%%,*}"
	if [[ "${RENDER_EXIT_SEQUENCE}" == *,* ]]; then
		RENDER_EXIT_SEQUENCE="${RENDER_EXIT_SEQUENCE#*,}"
	fi
	[ -z "${next}" ] && next=0
	if [ "${next}" -eq 0 ]; then
		return 0
	fi
	return "${next}"
}

run_preflight_checks() {
	PRE_FLIGHT_STATUS="running"
	if [ "${PRE_FLIGHT_APPEND_LOG:-false}" != "true" ]; then
		: > "${PRE_FLIGHT_LOG_FILE}"
	fi
	local next="${PRECHECK_SEQUENCE%%,*}"
	if [[ "${PRECHECK_SEQUENCE}" == *,* ]]; then
		PRECHECK_SEQUENCE="${PRECHECK_SEQUENCE#*,}"
	fi
	case "${next}" in
		pass)
			PRE_FLIGHT_STATUS="pass"
			echo "preflight-pass" >> "${PRE_FLIGHT_LOG_FILE}"
			return 0
			;;
		*)
			PRE_FLIGHT_STATUS="fail"
			echo "preflight-fail" >> "${PRE_FLIGHT_LOG_FILE}"
			return 1
			;;
	esac
}

attempt_self_heal_and_reexec() {
	printf '%s\n' "$1" >> "${SELF_HEAL_PHASE_LOG}"
	return 0
}

attempt_render_recovery_after_preflight_failure()
{
	local renderer_exit=0

	if [ "${HARNESS_MODE}" != "template_generate" ] || [ "${HARNESS_GENERATOR_MODE}" != "templates" ]; then
		return 1
	fi
	if [ "${PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED:-false}" = "true" ]; then
		return 1
	fi

	PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED="true"
	{
		echo "Render recovery: deterministic template rerender triggered after pre-flight failure."
		echo "Render recovery: preserving initial pre-flight diagnostics and attempting rerender."
	} >> "${PRE_FLIGHT_LOG_FILE}"

	if run_template_validation_harness_renderer; then
		renderer_exit=0
	else
		renderer_exit=$?
	fi

	if [ "${renderer_exit}" -ne 0 ]; then
		echo "Render recovery: template rerender failed with exit=${renderer_exit}; fail-open to legacy pre-flight failure handling." >> "${PRE_FLIGHT_LOG_FILE}"
		return 2
	fi

	echo "Render recovery: rerender completed; re-running pre-flight checks." >> "${PRE_FLIGHT_LOG_FILE}"
	local PRE_FLIGHT_APPEND_LOG="true"
	if run_preflight_checks; then
		echo "Render recovery: pre-flight checks passed after deterministic rerender." >> "${PRE_FLIGHT_LOG_FILE}"
		return 0
	fi

	echo "Render recovery: pre-flight checks still failing after deterministic rerender." >> "${PRE_FLIGHT_LOG_FILE}"
	return 2
}

if ! run_preflight_checks; then
	render_recovery_exit=1
	if attempt_render_recovery_after_preflight_failure; then
		render_recovery_exit=0
	else
		render_recovery_exit=$?
	fi

	if [ "${render_recovery_exit}" -ne 0 ]; then
		if [ "${render_recovery_exit}" -eq 2 ]; then
			attempt_self_heal_and_reexec "render"
		else
			attempt_self_heal_and_reexec "preflight"
		fi
		echo "terminal-failure" > "${RUNTIME_DIR}/terminal.txt"
		exit 0
	fi
fi

echo "continued" > "${RUNTIME_DIR}/pipeline.txt"
"""

	with tempfile.TemporaryDirectory(prefix="validate-render-recovery-fail-open-") as td:
		tmp_path = Path(td)
		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		env = os.environ.copy()
		env["RUNTIME_DIR"] = str(runtime_dir)
		proc = _run_script(script, env)
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert (runtime_dir / "terminal.txt").read_text(encoding="utf-8").strip() == "terminal-failure"
		assert not (runtime_dir / "pipeline.txt").exists()
		phase_log = (runtime_dir / "self_heal_phase.log").read_text(encoding="utf-8").strip().splitlines()
		assert phase_log == ["render"]
		log = (runtime_dir / "validation_preflight.log").read_text(encoding="utf-8")
		assert "Render recovery: template rerender failed with exit=14" in log


def test_non_template_preflight_failure_preserves_legacy_path() -> None:
	script = """#!/usr/bin/env bash
set -euo pipefail

PRE_FLIGHT_LOG_FILE="${RUNTIME_DIR}/validation_preflight.log"
PRE_FLIGHT_STATUS="not_run"
PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED="false"
HARNESS_MODE="generate"
HARNESS_GENERATOR_MODE="freehand"
SELF_HEAL_PHASE_LOG="${RUNTIME_DIR}/self_heal_phase.log"
RENDER_CALLS_FILE="${RUNTIME_DIR}/render_calls.txt"
PRECHECK_SEQUENCE="fail"

run_template_validation_harness_renderer() {
	echo "called" >> "${RENDER_CALLS_FILE}"
	return 0
}

run_preflight_checks() {
	PRE_FLIGHT_STATUS="running"
	if [ "${PRE_FLIGHT_APPEND_LOG:-false}" != "true" ]; then
		: > "${PRE_FLIGHT_LOG_FILE}"
	fi
	local next="${PRECHECK_SEQUENCE%%,*}"
	if [[ "${PRECHECK_SEQUENCE}" == *,* ]]; then
		PRECHECK_SEQUENCE="${PRECHECK_SEQUENCE#*,}"
	fi
	case "${next}" in
		pass)
			PRE_FLIGHT_STATUS="pass"
			echo "preflight-pass" >> "${PRE_FLIGHT_LOG_FILE}"
			return 0
			;;
		*)
			PRE_FLIGHT_STATUS="fail"
			echo "preflight-fail" >> "${PRE_FLIGHT_LOG_FILE}"
			return 1
			;;
	esac
}

attempt_self_heal_and_reexec() {
	printf '%s\n' "$1" >> "${SELF_HEAL_PHASE_LOG}"
	return 0
}

attempt_render_recovery_after_preflight_failure()
{
	local renderer_exit=0

	if [ "${HARNESS_MODE}" != "template_generate" ] || [ "${HARNESS_GENERATOR_MODE}" != "templates" ]; then
		return 1
	fi
	if [ "${PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED:-false}" = "true" ]; then
		return 1
	fi

	PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED="true"
	{
		echo "Render recovery: deterministic template rerender triggered after pre-flight failure."
		echo "Render recovery: preserving initial pre-flight diagnostics and attempting rerender."
	} >> "${PRE_FLIGHT_LOG_FILE}"

	if run_template_validation_harness_renderer; then
		renderer_exit=0
	else
		renderer_exit=$?
	fi

	if [ "${renderer_exit}" -ne 0 ]; then
		echo "Render recovery: template rerender failed with exit=${renderer_exit}; fail-open to legacy pre-flight failure handling." >> "${PRE_FLIGHT_LOG_FILE}"
		return 2
	fi

	echo "Render recovery: rerender completed; re-running pre-flight checks." >> "${PRE_FLIGHT_LOG_FILE}"
	local PRE_FLIGHT_APPEND_LOG="true"
	if run_preflight_checks; then
		echo "Render recovery: pre-flight checks passed after deterministic rerender." >> "${PRE_FLIGHT_LOG_FILE}"
		return 0
	fi

	echo "Render recovery: pre-flight checks still failing after deterministic rerender." >> "${PRE_FLIGHT_LOG_FILE}"
	return 2
}

if ! run_preflight_checks; then
	render_recovery_exit=1
	if attempt_render_recovery_after_preflight_failure; then
		render_recovery_exit=0
	else
		render_recovery_exit=$?
	fi

	if [ "${render_recovery_exit}" -ne 0 ]; then
		if [ "${render_recovery_exit}" -eq 2 ]; then
			attempt_self_heal_and_reexec "render"
		else
			attempt_self_heal_and_reexec "preflight"
		fi
		echo "terminal-failure" > "${RUNTIME_DIR}/terminal.txt"
		exit 0
	fi
fi

echo "continued" > "${RUNTIME_DIR}/pipeline.txt"
"""

	with tempfile.TemporaryDirectory(prefix="validate-render-recovery-legacy-") as td:
		tmp_path = Path(td)
		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		env = os.environ.copy()
		env["RUNTIME_DIR"] = str(runtime_dir)
		proc = _run_script(script, env)
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		phase_log = (runtime_dir / "self_heal_phase.log").read_text(encoding="utf-8").strip().splitlines()
		assert phase_log == ["preflight"]
		assert not (runtime_dir / "render_calls.txt").exists()
		assert (runtime_dir / "terminal.txt").read_text(encoding="utf-8").strip() == "terminal-failure"


def test_render_recovery_relint_fail_open_uses_render_phase_self_heal() -> None:
	script = """#!/usr/bin/env bash
set -euo pipefail

PRE_FLIGHT_LOG_FILE="${RUNTIME_DIR}/validation_preflight.log"
PRE_FLIGHT_STATUS="not_run"
PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED="false"
HARNESS_MODE="template_generate"
HARNESS_GENERATOR_MODE="templates"
SELF_HEAL_PHASE_LOG="${RUNTIME_DIR}/self_heal_phase.log"
RENDER_CALLS_FILE="${RUNTIME_DIR}/render_calls.txt"
RENDER_EXIT_SEQUENCE="0"
PRECHECK_SEQUENCE="fail,fail"

run_template_validation_harness_renderer() {
	echo "called" >> "${RENDER_CALLS_FILE}"
	local next="${RENDER_EXIT_SEQUENCE%%,*}"
	if [[ "${RENDER_EXIT_SEQUENCE}" == *,* ]]; then
		RENDER_EXIT_SEQUENCE="${RENDER_EXIT_SEQUENCE#*,}"
	fi
	[ -z "${next}" ] && next=0
	if [ "${next}" -eq 0 ]; then
		return 0
	fi
	return "${next}"
}

run_preflight_checks() {
	PRE_FLIGHT_STATUS="running"
	if [ "${PRE_FLIGHT_APPEND_LOG:-false}" != "true" ]; then
		: > "${PRE_FLIGHT_LOG_FILE}"
	fi
	local next="${PRECHECK_SEQUENCE%%,*}"
	if [[ "${PRECHECK_SEQUENCE}" == *,* ]]; then
		PRECHECK_SEQUENCE="${PRECHECK_SEQUENCE#*,}"
	fi
	case "${next}" in
		pass)
			PRE_FLIGHT_STATUS="pass"
			echo "preflight-pass" >> "${PRE_FLIGHT_LOG_FILE}"
			return 0
			;;
		*)
			PRE_FLIGHT_STATUS="fail"
			echo "preflight-fail" >> "${PRE_FLIGHT_LOG_FILE}"
			return 1
			;;
	esac
}

attempt_self_heal_and_reexec() {
	printf '%s\n' "$1" >> "${SELF_HEAL_PHASE_LOG}"
	return 0
}

attempt_render_recovery_after_preflight_failure()
{
	local renderer_exit=0

	if [ "${HARNESS_MODE}" != "template_generate" ] || [ "${HARNESS_GENERATOR_MODE}" != "templates" ]; then
		return 1
	fi
	if [ "${PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED:-false}" = "true" ]; then
		return 1
	fi

	PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED="true"
	{
		echo "Render recovery: deterministic template rerender triggered after pre-flight failure."
		echo "Render recovery: preserving initial pre-flight diagnostics and attempting rerender."
	} >> "${PRE_FLIGHT_LOG_FILE}"

	if run_template_validation_harness_renderer; then
		renderer_exit=0
	else
		renderer_exit=$?
	fi

	if [ "${renderer_exit}" -ne 0 ]; then
		echo "Render recovery: template rerender failed with exit=${renderer_exit}; fail-open to legacy pre-flight failure handling." >> "${PRE_FLIGHT_LOG_FILE}"
		return 2
	fi

	echo "Render recovery: rerender completed; re-running pre-flight checks." >> "${PRE_FLIGHT_LOG_FILE}"
	local PRE_FLIGHT_APPEND_LOG="true"
	if run_preflight_checks; then
		echo "Render recovery: pre-flight checks passed after deterministic rerender." >> "${PRE_FLIGHT_LOG_FILE}"
		return 0
	fi

	echo "Render recovery: pre-flight checks still failing after deterministic rerender." >> "${PRE_FLIGHT_LOG_FILE}"
	return 2
}

if ! run_preflight_checks; then
	render_recovery_exit=1
	if attempt_render_recovery_after_preflight_failure; then
		render_recovery_exit=0
	else
		render_recovery_exit=$?
	fi

	if [ "${render_recovery_exit}" -ne 0 ]; then
		if [ "${render_recovery_exit}" -eq 2 ]; then
			attempt_self_heal_and_reexec "render"
		else
			attempt_self_heal_and_reexec "preflight"
		fi
		echo "terminal-failure" > "${RUNTIME_DIR}/terminal.txt"
		exit 0
	fi
fi

echo "continued" > "${RUNTIME_DIR}/pipeline.txt"
"""

	with tempfile.TemporaryDirectory(prefix="validate-render-recovery-relint-fail-") as td:
		tmp_path = Path(td)
		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		env = os.environ.copy()
		env["RUNTIME_DIR"] = str(runtime_dir)
		proc = _run_script(script, env)
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert (runtime_dir / "terminal.txt").read_text(encoding="utf-8").strip() == "terminal-failure"
		assert not (runtime_dir / "pipeline.txt").exists()
		phase_log = (runtime_dir / "self_heal_phase.log").read_text(encoding="utf-8").strip().splitlines()
		assert phase_log == ["render"]
		render_calls = (runtime_dir / "render_calls.txt").read_text(encoding="utf-8").strip().splitlines()
		assert render_calls == ["called"]
		log = (runtime_dir / "validation_preflight.log").read_text(encoding="utf-8")
		assert log.count("preflight-fail") == 2
		assert "Render recovery: rerender completed; re-running pre-flight checks." in log
		assert "Render recovery: pre-flight checks still failing after deterministic rerender." in log


def main() -> int:
	test_render_recovery_success_continues_pipeline()
	test_render_recovery_fail_open_uses_render_phase_self_heal()
	test_non_template_preflight_failure_preserves_legacy_path()
	test_render_recovery_relint_fail_open_uses_render_phase_self_heal()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
