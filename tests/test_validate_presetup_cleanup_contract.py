#!/usr/bin/env python3
"""Contract: validate.yml cleanup steps when authorization or runtime setup fails.

Run 36378523375 showed the `always()` steps after a failed
`Authorize explicit validation target` producing secondary errors: running a
missing validate_process.sh, writing /validation_status.json, sourcing
scripts/ from a missing checkout and uploading `/`. These tests pin the gates
that stop that while authorization stays fail-closed.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"

VALIDATE_RUN_IF = (
	"always() && steps.workspace_after_create_hook.outcome != 'failure'"
	" && steps.workspace_before_run_hook.outcome != 'failure'"
	" && steps.authorized_target.outcome == 'success'"
	" && steps.runtime.outcome == 'success'"
)
FINALIZE_IF = "env.CLAUDE_CAPACITY_WAIT != 'true' && (always())"

RUNTIME_DIR_EXPR = "${{ steps.runtime.outputs.runtime_dir }}"
WORKSPACE_GUARD_EXPR = "${{ steps.workspace_state.outputs.workspace_path == '' && '!' || '' }}"
WORKSPACE_PATH_EXPR = "${{ steps.workspace_state.outputs.workspace_path }}"

EXPECTED_NOT_INITIALIZED_OUTPUTS = {
	"status": "error",
	"raw_status": "not_initialized",
	"summary": "Validation did not start: authorization or runtime setup did not complete.",
	"failure_summary": "Authorization, checkout or runtime setup failed before validation started. See the earlier step errors.",
	"status_file": "",
	"metadata_file": "",
	"failure_owner": "none",
	"harness_report": "false",
	"harness_stage": "",
	"harness_reason": "",
	"harness_fp": "",
	"harness_signature_b64": "",
}


def _validate_job() -> dict[str, object]:
	doc = yaml.safe_load(VALIDATE_WORKFLOW.read_text(encoding="utf-8"))
	assert isinstance(doc, dict)
	job = doc["jobs"]["validate"]
	assert isinstance(job, dict)
	return job


def _steps() -> list[dict[str, object]]:
	steps = _validate_job().get("steps")
	assert isinstance(steps, list)
	return [step for step in steps if isinstance(step, dict)]


def _step_by_name(name: str) -> dict[str, object]:
	for step in _steps():
		if str(step.get("name", "")).strip() == name:
			return step
	raise AssertionError(f"Step not found: {name}")


def _step_by_id(step_id: str) -> dict[str, object]:
	for step in _steps():
		if step.get("id") == step_id:
			return step
	raise AssertionError(f"Step id not found: {step_id}")


def _run_text(step: dict[str, object]) -> str:
	run = step.get("run")
	assert isinstance(run, str)
	return run


def _parse_outputs(path: Path) -> dict[str, str]:
	outputs: dict[str, str] = {}
	for line in path.read_text(encoding="utf-8").splitlines():
		key, _, value = line.partition("=")
		outputs[key] = value
	return outputs


def test_authorization_step_is_unchanged_and_fail_closed() -> None:
	step = _step_by_id("authorized_target")
	assert step.get("name") == "Authorize explicit validation target"
	assert "if" not in step
	assert "continue-on-error" not in step
	assert "exit 1" in _run_text(step)


def test_validate_run_requires_authorization_and_runtime() -> None:
	step = _step_by_id("validate_run")
	assert step.get("name") == "Run validation process"
	assert step.get("if") == VALIDATE_RUN_IF
	assert step.get("continue-on-error") is True
	env = step.get("env")
	assert isinstance(env, dict) and "RUNTIME_DIR" in env


def test_upload_step_never_renders_bare_root_path() -> None:
	step = _step_by_name("Upload validation artifacts")
	condition = str(step.get("if"))
	assert condition.startswith(FINALIZE_IF)
	assert "steps.runtime.outputs.runtime_dir != ''" in condition
	path_block = str(step.get("with", {}).get("path"))
	for expr in (RUNTIME_DIR_EXPR, WORKSPACE_GUARD_EXPR, WORKSPACE_PATH_EXPR):
		assert expr in path_block, expr
	for workspace_path in ("", "/w"):
		rendered = (
			path_block.replace(WORKSPACE_GUARD_EXPR, "!" if workspace_path == "" else "")
			.replace(WORKSPACE_PATH_EXPR, workspace_path)
			.replace(RUNTIME_DIR_EXPR, "/r")
		)
		assert "${{" not in rendered
		lines = [line.strip() for line in rendered.splitlines() if line.strip()]
		assert lines, rendered
		for line in lines:
			if line.startswith("!"):
				continue
			assert line not in ("/", "/validation/"), (workspace_path, line)
		if workspace_path == "":
			assert "!/validation/" in lines
		else:
			assert "/w/validation/" in lines
		assert "/r/" in lines


def test_collect_status_without_runtime_dir_writes_no_files(tmp_path: Path) -> None:
	step = _step_by_id("collect_status")
	assert step.get("if") == FINALIZE_IF
	body = _run_text(step)
	assert "${{" not in body
	for label, runtime_dir in (("empty", ""), ("missing", str(tmp_path / "case-missing" / "missing"))):
		case_dir = tmp_path / f"case-{label}"
		case_dir.mkdir()
		output_file = case_dir / "out"
		env = {
			"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
			"HOME": str(case_dir),
			"RUNTIME_DIR": runtime_dir,
			"GITHUB_OUTPUT": str(output_file),
		}
		result = subprocess.run(
			["bash", "-c", body],
			cwd=str(case_dir),
			env=env,
			capture_output=True,
			text=True,
			check=False,
		)
		assert result.returncode == 0, (label, result.stderr)
		assert _parse_outputs(output_file) == EXPECTED_NOT_INITIALIZED_OUTPUTS, label
		assert sorted(p.name for p in case_dir.iterdir()) == ["out"], label
		assert not (case_dir / "missing").exists()


def test_enforce_outcome_still_fails_on_not_initialized(tmp_path: Path) -> None:
	enforce = _step_by_name("Enforce validation outcome")
	assert enforce.get("if") == FINALIZE_IF
	assert _step_by_name("Write run summary").get("if") == FINALIZE_IF
	result = subprocess.run(
		["bash", "-c", _run_text(enforce)],
		cwd=str(tmp_path),
		env={
			"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
			"VALIDATION_STATUS": "error",
			"VALIDATION_RAW_STATUS": "not_initialized",
		},
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 1, result.stderr


def test_before_remove_hook_skips_without_staged_helper() -> None:
	step = _step_by_name("Run workspace before_remove hook")
	assert step.get("if") == "always() && env.VALIDATE_HOOK_HELPER != ''"
	assert ': "${VALIDATE_HOOK_HELPER:?' in _run_text(step)


def test_checkout_dependent_finalize_steps_are_gated() -> None:
	for name in ("Record validation candidate", "Record validation run end"):
		condition = str(_step_by_name(name).get("if"))
		assert condition == f"{FINALIZE_IF} && steps.runtime.outcome == 'success'", name
	force_tick = str(_step_by_name("Force orchestrate poll after validation finalization").get("if"))
	assert force_tick == f"{FINALIZE_IF} && steps.authorized_target.outcome == 'success'"


def test_not_initialized_marker_and_no_setup_state_output() -> None:
	assert "raw_status=not_initialized" in VALIDATE_WORKFLOW.read_text(encoding="utf-8")
	outputs = _validate_job().get("outputs") or {}
	assert isinstance(outputs, dict)
	assert "setup_state" not in outputs
