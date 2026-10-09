#!/usr/bin/env python3
"""Contract for review_autofix.yml steps whose bodies live in scripts/.

The moved steps keep their name, if:, env: and continue-on-error: in the
workflow; their run: block only resolves the script and sources it in the
step shell. These checks pin the wrapper shape, the resolution order that
keeps consumer repos and older PR branches working, the fail-closed /
fail-open split, and the staging entries.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import (  # noqa: E402
	REPO_ROOT,
	REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS,
	REVIEW_AUTOFIX_STEP_SCRIPTS,
	REVIEW_AUTOFIX_WORKFLOW_PATH,
	expanded_review_autofix_text,
	review_autofix_step_script_body,
)


STAGE_SUPPORT_HELPER = REPO_ROOT / "scripts" / "stage_workflow_support.sh"


def _steps_by_name(workflow_text: str) -> dict[str, dict]:
	workflow = yaml.safe_load(workflow_text)
	steps: dict[str, dict] = {}
	for job in workflow["jobs"].values():
		for step in job.get("steps", []):
			if "name" in step:
				steps.setdefault(step["name"], step)
	return steps


def test_moved_steps_run_only_the_resolving_wrapper() -> None:
	steps = _steps_by_name(REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8"))
	for step_name, (script_name, severity) in REVIEW_AUTOFIX_STEP_SCRIPTS.items():
		assert step_name in steps, f"Step not found in review_autofix.yml: {step_name}"
		run = steps[step_name]["run"]
		if script_name in REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS:
			checkout_dir = REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS[script_name]
			expected_gate_lines = [
				f'REVIEW_AUTOFIX_STEP_SCRIPT="${{GITHUB_WORKSPACE}}/{checkout_dir}/scripts/{script_name}"',
				'if [ "${HEAD_GATE_HELPER_VERIFIED:-false}" != "true" ] || [ ! -f "${REVIEW_AUTOFIX_STEP_SCRIPT}" ]; then',
			]
			gate_run_lines = run.rstrip("\n").split("\n")
			assert gate_run_lines[:2] == expected_gate_lines, f"{step_name}: unexpected gate-job resolution:\n{run}"
			assert gate_run_lines[2].startswith("  echo \"::error::") and gate_run_lines[3] == "  exit 1", step_name
			assert gate_run_lines[-2:] == ["fi", 'source "${REVIEW_AUTOFIX_STEP_SCRIPT}"'], step_name
			assert len(gate_run_lines) == 6, f"{step_name}: gate-job wrapper must have no other resolution path"
			assert "SUPPORT_SCRIPTS_DIR" not in run and ".codex-workflow-src" not in run, (
				f"{step_name}: gate-job steps run before support staging and must read only the verified head-gate checkout"
			)
			assert "${{" not in run, f"{step_name}: wrapper must not carry GitHub expressions"
			continue
		expected_lines = [
			f'REVIEW_AUTOFIX_STEP_SCRIPT="${{SUPPORT_SCRIPTS_DIR:-}}/{script_name}"',
			f'[ -f "${{REVIEW_AUTOFIX_STEP_SCRIPT}}" ] || REVIEW_AUTOFIX_STEP_SCRIPT="${{GITHUB_WORKSPACE}}/.codex-workflow-src/scripts/{script_name}"',
			'if [ ! -f "${REVIEW_AUTOFIX_STEP_SCRIPT}" ]; then',
		]
		run_lines = run.rstrip("\n").split("\n")
		assert run_lines[:3] == expected_lines, f"{step_name}: unexpected resolution order:\n{run}"
		assert run_lines[-2:] == ["fi", 'source "${REVIEW_AUTOFIX_STEP_SCRIPT}"'], (
			f"{step_name}: the script must be sourced in the step shell (not run in a child bash) "
			"so BASH_ENV state, set -e and exit codes behave as they did inline"
		)
		if severity == "error":
			assert run_lines[3].startswith("  echo \"::error::") and run_lines[4] == "  exit 1", step_name
		else:
			assert run_lines[3].startswith("  echo \"::warning::") and run_lines[4] == "  exit 0", step_name
		assert "${{" not in run, f"{step_name}: wrapper must not carry GitHub expressions"


def test_fail_open_only_for_steps_that_run_after_staging_failures() -> None:
	steps = _steps_by_name(REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8"))
	for step_name, (_script_name, severity) in REVIEW_AUTOFIX_STEP_SCRIPTS.items():
		runs_always = str(steps[step_name].get("if", "")).startswith("always()")
		assert (severity == "warning") == runs_always, (
			f"{step_name}: only always() steps may skip on a missing script; "
			f"got severity={severity} for if={steps[step_name].get('if')!r}"
		)


def test_step_scripts_exist_are_executable_and_parse() -> None:
	for script_name, _severity in REVIEW_AUTOFIX_STEP_SCRIPTS.values():
		path = REPO_ROOT / "scripts" / script_name
		assert path.is_file(), f"Missing step script: {path}"
		assert path.stat().st_mode & 0o111, f"Step script is not executable: {path}"
		text = path.read_text(encoding="utf-8")
		assert text.startswith("#!/usr/bin/env bash\n"), f"{script_name}: missing bash shebang"
		body = review_autofix_step_script_body(script_name)
		assert body.startswith("set -euo pipefail\n"), f"{script_name}: body must open with set -euo pipefail"
		# GitHub only substitutes ${{ }} inside the workflow file; in a
		# sourced script it would reach bash literally.
		assert "${{" not in text, f"{script_name}: contains a GitHub expression that would never be substituted"
		result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
		assert result.returncode == 0, f"{script_name}: bash -n failed: {result.stderr}"


def test_step_scripts_are_required_bootstrap_scripts() -> None:
	stage_helper = STAGE_SUPPORT_HELPER.read_text(encoding="utf-8")
	match = re.search(r'^REQUIRED_BOOTSTRAP_SCRIPTS="([^"]*)"', stage_helper, re.MULTILINE)
	assert match, "REQUIRED_BOOTSTRAP_SCRIPTS not found in scripts/stage_workflow_support.sh"
	required = set(match.group(1).split())
	for script_name, _severity in REVIEW_AUTOFIX_STEP_SCRIPTS.values():
		assert script_name in required, f"{script_name} must be staged via REQUIRED_BOOTSTRAP_SCRIPTS"


def test_every_wrapper_is_registered_and_expands() -> None:
	raw = REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8")
	wrapped = set(
		re.findall(
			r'REVIEW_AUTOFIX_STEP_SCRIPT="(?:\$\{SUPPORT_SCRIPTS_DIR:-\}|\$\{GITHUB_WORKSPACE\}/\.codex-head-gate-src/scripts)/([a-z0-9_]+\.sh)"',
			raw,
		)
	)
	registered = {script_name for script_name, _severity in REVIEW_AUTOFIX_STEP_SCRIPTS.values()}
	assert wrapped == registered, f"Wrapper/registry mismatch: wrapped={sorted(wrapped)} registered={sorted(registered)}"
	expanded = expanded_review_autofix_text()
	assert "REVIEW_AUTOFIX_STEP_SCRIPT" not in expanded
	expanded_steps = _steps_by_name(expanded)
	for step_name, (script_name, _severity) in REVIEW_AUTOFIX_STEP_SCRIPTS.items():
		assert expanded_steps[step_name]["run"].rstrip("\n") == review_autofix_step_script_body(script_name).rstrip("\n")


def test_gate_job_step_scripts_come_only_from_verified_head_gate_checkout() -> None:
	raw = REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8")
	workflow = yaml.safe_load(raw)
	gate_steps = workflow["jobs"]["gate"]["steps"]
	names = [step.get("name") for step in gate_steps]
	by_name = {step.get("name"): step for step in gate_steps}
	checkout = by_name["Checkout head-gate helper"]
	verify = by_name["Verify head-gate helper identity"]
	assert checkout["with"]["ref"] == "${{ steps.resolve_support.outputs.review_support_sha }}"
	assert checkout["with"]["path"] == ".codex-head-gate-src"
	sparse = checkout["with"]["sparse-checkout"].split()
	assert 'echo "HEAD_GATE_HELPER_VERIFIED=true" >> "$GITHUB_ENV"' in verify["run"]
	for step_name, (script_name, _severity) in REVIEW_AUTOFIX_STEP_SCRIPTS.items():
		if script_name not in REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS:
			continue
		assert REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS[script_name] == ".codex-head-gate-src"
		assert f"scripts/{script_name}" in sparse, f"{script_name} must be in the head-gate sparse checkout"
		assert f"[ ! -f .codex-head-gate-src/scripts/{script_name} ]" in verify["run"], (
			f"{script_name} must be checked by the head-gate identity verify step"
		)
		assert step_name in by_name, f"{step_name} must run in the gate job"
		assert "HEAD_GATE_HELPER_VERIFIED" in by_name[step_name]["run"]
		assert names.index("Verify head-gate helper identity") < names.index(step_name)


def main() -> int:
	test_functions = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
	for func in test_functions:
		func()
	print(f"OK: {len(test_functions)} review_autofix step-script contract checks passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
