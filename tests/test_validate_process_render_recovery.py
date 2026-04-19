#!/usr/bin/env python3
"""Regression tests for template-mode preflight render recovery in validate_process.sh."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS_PATH = REPO_ROOT / "scripts" / "validate_process.sh"
SELF_HEAL_PROMPT_PATH = REPO_ROOT / "prompts" / "mode-validate-self-heal.txt"
SELF_HEAL_SCRIPT_PATH = REPO_ROOT / "scripts" / "self_heal_validation.sh"


def _validate_process_text() -> str:
	return VALIDATE_PROCESS_PATH.read_text(encoding="utf-8")


def _self_heal_prompt_text() -> str:
	return SELF_HEAL_PROMPT_PATH.read_text(encoding="utf-8")


def _self_heal_script_text() -> str:
	return SELF_HEAL_SCRIPT_PATH.read_text(encoding="utf-8")


def _extract_function(name: str) -> str:
	text = _validate_process_text()
	match = re.search(
		rf"^{re.escape(name)}\(\)\n\{{.*?^\}}\n",
		text,
		re.DOTALL | re.MULTILINE,
	)
	if not match:
		raise AssertionError(f"could not extract function {name} from validate_process.sh")
	return match.group(0)


def _extract_phase2_block() -> str:
	text = _validate_process_text()
	start_marker = (
		"# ---------------------------------------------------------------\n"
		"# Phase 2: Pre-flight checks for generated harness\n"
		"# ---------------------------------------------------------------\n"
	)
	end_marker = (
		"\n\n# ---------------------------------------------------------------\n"
		"# Phase 3: Execute validation harness (idle-timeout based)\n"
		"# ---------------------------------------------------------------\n"
	)
	start = text.find(start_marker)
	if start == -1:
		raise AssertionError("could not locate phase 2 block start in validate_process.sh")
	end = text.find(end_marker, start)
	if end == -1:
		raise AssertionError("could not locate phase 2 block end in validate_process.sh")
	return text[start:end]


def _phase2_harness_script() -> str:
	classify_fn = _extract_function("classify_preflight_failure")
	recovery_fn = _extract_function("attempt_template_render_recovery_after_preflight_lint")
	phase2_block = _extract_phase2_block()
	return textwrap.dedent(
		f"""\
		#!/usr/bin/env bash
		set -euo pipefail

		RUNTIME_DIR="${{RUNTIME_DIR:?RUNTIME_DIR is required}}"
		SCENARIO="${{SCENARIO:?SCENARIO is required}}"
		RENDERER_EXIT="${{RENDERER_EXIT:-0}}"
		HARNESS_MODE="${{HARNESS_MODE:-template_generate}}"
		PRE_FLIGHT_LOG_FILE="${{RUNTIME_DIR}}/validation_preflight.log"
		DIAGNOSE_RESULT_FILE="${{RUNTIME_DIR}}/validation_diagnosis.json"
		GITHUB_REPOSITORY="example/repo"
		TRACKING_ISSUE_RAW="123"
		PRE_FLIGHT_STATUS="not_run"
		PRE_FLIGHT_FAILURE_KIND="none"
		PRE_FLIGHT_FAILURE_REASON="not_run"
		RUN_PREFLIGHT_CALLS=0
		RUN_RENDERER_CALLS=0

		write_metrics()
		{{
			{{
				echo "preflight_calls=${{RUN_PREFLIGHT_CALLS}}"
				echo "renderer_calls=${{RUN_RENDERER_CALLS}}"
				echo "preflight_status=${{PRE_FLIGHT_STATUS}}"
				echo "preflight_kind=${{PRE_FLIGHT_FAILURE_KIND}}"
				echo "preflight_reason=${{PRE_FLIGHT_FAILURE_REASON}}"
			}} > "${{RUNTIME_DIR}}/metrics.txt"
		}}
		trap write_metrics EXIT

		run_preflight_checks()
		{{
			RUN_PREFLIGHT_CALLS=$((RUN_PREFLIGHT_CALLS + 1))
			: > "${{PRE_FLIGHT_LOG_FILE}}"
			case "${{SCENARIO}}:${{RUN_PREFLIGHT_CALLS}}" in
				lint_then_pass:1)
					PRE_FLIGHT_STATUS="fail"
					echo "Shell syntax check failed: validation/tests/00_canary.sh" >> "${{PRE_FLIGHT_LOG_FILE}}"
					return 1
					;;
				lint_then_pass:2)
					PRE_FLIGHT_STATUS="pass"
					return 0
					;;
				lint_then_lint:1|lint_then_lint:2)
					PRE_FLIGHT_STATUS="fail"
					echo "Shell syntax check failed: validation/tests/00_canary.sh" >> "${{PRE_FLIGHT_LOG_FILE}}"
					return 1
					;;
				non_lint_once:1)
					PRE_FLIGHT_STATUS="fail"
					echo "Missing validation/validate.env" >> "${{PRE_FLIGHT_LOG_FILE}}"
					return 1
					;;
				*)
					PRE_FLIGHT_STATUS="fail"
					echo "Unexpected test scenario call: ${{SCENARIO}}/${{RUN_PREFLIGHT_CALLS}}" >> "${{PRE_FLIGHT_LOG_FILE}}"
					return 1
					;;
			esac
		}}

		run_template_validation_harness_renderer()
		{{
			RUN_RENDERER_CALLS=$((RUN_RENDERER_CALLS + 1))
			return "${{RENDERER_EXIT}}"
		}}

		attempt_self_heal_and_reexec()
		{{
			printf '%s\n' "$1" > "${{RUNTIME_DIR}}/self_heal_phase.txt"
			return 0
		}}

		post_tracking_comment() {{ :; }}
		set_tracking_phase_label() {{ :; }}
		write_result_files() {{ :; }}
		tg_notify() {{ :; }}
		jq() {{ printf '%s\n' '{{}}'; }}

		{classify_fn}
		{recovery_fn}
		{phase2_block}

		echo "phase2_complete=true" > "${{RUNTIME_DIR}}/phase2_complete.txt"
		"""
	)


def _run_phase2_harness(
	scenario: str,
	renderer_exit: int,
	harness_mode: str = "template_generate",
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], str, bool]:
	with tempfile.TemporaryDirectory(prefix="validate-render-recovery-") as td:
		runtime_dir = Path(td)
		script_path = runtime_dir / "phase2_harness.sh"
		script_path.write_text(_phase2_harness_script(), encoding="utf-8")
		script_path.chmod(0o755)

		env = os.environ.copy()
		env["RUNTIME_DIR"] = str(runtime_dir)
		env["SCENARIO"] = scenario
		env["RENDERER_EXIT"] = str(renderer_exit)
		env["HARNESS_MODE"] = harness_mode

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(REPO_ROOT),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

		metrics_path = runtime_dir / "metrics.txt"
		if not metrics_path.exists():
			raise AssertionError(f"metrics file missing. stdout={proc.stdout} stderr={proc.stderr}")
		metrics: dict[str, str] = {}
		for line in metrics_path.read_text(encoding="utf-8").splitlines():
			if "=" not in line:
				continue
			k, v = line.split("=", 1)
			metrics[k] = v

		phase_file = runtime_dir / "self_heal_phase.txt"
		self_heal_phase = phase_file.read_text(encoding="utf-8").strip() if phase_file.exists() else ""
		completed = (runtime_dir / "phase2_complete.txt").exists()
		return proc, metrics, self_heal_phase, completed


def test_template_lint_failure_rerenders_and_relints_before_success() -> None:
	proc, metrics, phase, completed = _run_phase2_harness(
		scenario="lint_then_pass",
		renderer_exit=0,
	)
	assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
	assert completed
	assert phase == ""
	assert metrics.get("preflight_calls") == "2"
	assert metrics.get("renderer_calls") == "1"
	assert metrics.get("preflight_status") == "pass"
	assert metrics.get("preflight_kind") == "none"


def test_template_renderer_failure_fails_open_with_render_phase() -> None:
	proc, metrics, phase, completed = _run_phase2_harness(
		scenario="lint_then_lint",
		renderer_exit=14,
	)
	assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
	assert not completed
	assert phase == "render"
	assert metrics.get("preflight_calls") == "1"
	assert metrics.get("renderer_calls") == "1"
	assert metrics.get("preflight_kind") == "render"
	assert metrics.get("preflight_reason") == "render_retry_renderer_exit_14"


def test_template_rerender_still_failing_relint_fails_open_with_render_phase() -> None:
	proc, metrics, phase, completed = _run_phase2_harness(
		scenario="lint_then_lint",
		renderer_exit=0,
	)
	assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
	assert not completed
	assert phase == "render"
	assert metrics.get("preflight_calls") == "2"
	assert metrics.get("renderer_calls") == "1"
	assert metrics.get("preflight_kind") == "render"
	assert metrics.get("preflight_reason") == "render_retry_exhausted"


def test_non_lint_preflight_failure_skips_rerender_and_stays_preflight_phase() -> None:
	proc, metrics, phase, completed = _run_phase2_harness(
		scenario="non_lint_once",
		renderer_exit=0,
	)
	assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
	assert not completed
	assert phase == "preflight"
	assert metrics.get("preflight_calls") == "1"
	assert metrics.get("renderer_calls") == "0"
	assert metrics.get("preflight_kind") == "non_lint"
	assert metrics.get("preflight_reason") == "missing_validation_artifact"


def test_self_heal_prompt_keeps_prompt_only_scope_and_renderer_guidance() -> None:
	prompt_text = _self_heal_prompt_text()
	assert "You may ONLY edit these four files:" in prompt_text
	assert "prompts/mode-validate-discover.txt" in prompt_text
	assert "prompts/mode-validate-generate.txt" in prompt_text
	assert "prompts/mode-validate-fix-harness.txt" in prompt_text
	assert "prompts/mode-validate-diagnose.txt" in prompt_text
	assert "Harness recovery for template-mode render/lint failures is renderer-driven in validate_process.sh" in prompt_text


def test_self_heal_script_documents_render_phase_tag() -> None:
	script_text = _self_heal_script_text()
	assert (
		'SELF_HEAL_FAILURE_PHASE    — string tag ("generate"|"preflight"|"render"|"canary"|"diagnose"|"runtime"|"discover")'
		in script_text
	)


def main() -> int:
	failed = 0
	for name in sorted(n for n in globals() if n.startswith("test_")):
		try:
			globals()[name]()
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
