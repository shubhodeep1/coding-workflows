#!/usr/bin/env python3
"""Focused contract checks for plan scope-mode prompt wiring."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_PROMPT = REPO_ROOT / "prompts" / "mode-plan.txt"
PLAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "plan.yml"
PLAN_RUNNER = REPO_ROOT / "scripts" / "run_plan_codex.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
REUSE_AUDIT_CONTRACT_TEST = REPO_ROOT / "tests" / "test_plan_reuse_audit_contract.py"
RENDER_PROMPT = REPO_ROOT / "scripts" / "render_prompt.py"


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def _render_mode_plan(*extra_args: str) -> str:
	proc = subprocess.run(
		[sys.executable, str(RENDER_PROMPT), str(PLAN_PROMPT), *extra_args],
		cwd=str(REPO_ROOT),
		env=_base_env(),
		text=True,
		capture_output=True,
		timeout=60,
	)
	assert proc.returncode == 0, proc.stderr
	return proc.stdout


def _assert_decisions_contract(text: str) -> None:
	assert "## Decisions" in text
	assert "### D<n> — <title>" in text
	assert "Each decision record must include non-empty bullets for `Chosen`, `Alternatives considered`, and `Why`." in text


def _workflow_step_block(workflow: str, step_name: str) -> dict[str, object]:
	parsed_workflow = yaml.safe_load(workflow)
	assert isinstance(parsed_workflow, dict), "workflow YAML must parse to a mapping"

	jobs = parsed_workflow.get("jobs")
	assert isinstance(jobs, dict), "workflow YAML missing jobs mapping"

	matching_steps: list[dict[str, object]] = []
	for job in jobs.values():
		if not isinstance(job, dict):
			continue
		steps = job.get("steps")
		if not isinstance(steps, list):
			continue
		for step in steps:
			if isinstance(step, dict) and step.get("name") == step_name:
				matching_steps.append(step)

	assert matching_steps, f"missing workflow step: {step_name}"
	assert len(matching_steps) == 1, f"duplicate workflow step: {step_name}"
	return matching_steps[0]


def test_mode_plan_scope_mode_contract_defaults_on() -> None:
	rendered = _render_mode_plan()

	_assert_decisions_contract(rendered)
	assert "PLAN_SCOPE_MODE_REQUIRED=true" in rendered
	assert "`Scope-mode: <Expansion | Selective Expansion | Hold Scope | Reduction>`" in rendered
	assert "`Scope-mode justification:`" in rendered
	assert "Boil the Lake forcing question" in rendered
	assert "Security > Correctness & safety > Backward compatibility" in rendered
	assert "Operational clarity > Performance > Speed; completeness moves correctness." in rendered
	assert "Reduction safety net" in rendered
	assert "MUST emit a clarification Q-ID" in rendered
	assert "existing scope-too-large gate" in rendered


def test_mode_plan_scope_mode_contract_relaxes_when_flag_disabled() -> None:
	rendered = _render_mode_plan("--var", "PLAN_SCOPE_MODE_REQUIRED=false")

	_assert_decisions_contract(rendered)
	assert "PLAN_SCOPE_MODE_REQUIRED=false" in rendered
	assert "optional but preferred" in rendered
	assert "whenever `Scope-mode:` is present" in rendered


def test_plan_workflow_exports_flag_and_keeps_live_prompt_parity() -> None:
	workflow = PLAN_WORKFLOW.read_text(encoding="utf-8")
	plan_runner = PLAN_RUNNER.read_text(encoding="utf-8")

	_assert_decisions_contract(plan_runner)
	assert "PLAN_SCOPE_MODE_REQUIRED: ${{ vars.PLAN_SCOPE_MODE_REQUIRED || 'true' }}" in workflow
	assert "PLAN_SCOPE_MODE_REQUIRED={{PLAN_SCOPE_MODE_REQUIRED}}" in plan_runner
	assert "`Scope-mode: <Expansion | Selective Expansion | Hold Scope | Reduction>`" in plan_runner
	assert "`Scope-mode justification:`" in plan_runner
	assert "Boil the Lake forcing question" in plan_runner
	assert "Security > Correctness & safety > Backward compatibility" in plan_runner
	assert "Operational clarity > Performance > Speed; completeness moves correctness." in plan_runner
	assert "Reduction safety net" in plan_runner
	assert "MUST emit a clarification Q-ID" in plan_runner
	assert "existing scope-too-large gate" in plan_runner


def test_ci_workflow_keeps_plan_decisions_lint_contract() -> None:
	workflow = CI_WORKFLOW.read_text(encoding="utf-8")
	unit_test_step = _workflow_step_block(workflow, "Plan decisions lint unit tests")
	advisory_step = _workflow_step_block(workflow, "Plan decisions advisory lint")

	unit_test_run = unit_test_step.get("run")
	assert isinstance(unit_test_run, str)
	assert "PYTHONDONTWRITEBYTECODE=1 python3 tests/test_lint_plan_decisions.py" in unit_test_run

	assert advisory_step.get("continue-on-error") is True
	advisory_env = advisory_step.get("env")
	assert isinstance(advisory_env, dict)
	assert advisory_env.get("DOCS_DECISION_LINT_ENABLED") == "${{ vars.DOCS_DECISION_LINT_ENABLED || 'false' }}"

	advisory_run = advisory_step.get("run")
	assert isinstance(advisory_run, str)
	assert "decision_lint_stderr=\"$(mktemp)\"" in advisory_run
	assert "PYTHONDONTWRITEBYTECODE=1 python3 scripts/lint_plan_decisions.py 2> \"${decision_lint_stderr}\"" in advisory_run
	assert "if [ \"${DOCS_DECISION_LINT_ENABLED}\" = \"true\" ] && [ -s \"${decision_lint_stderr}\" ]; then" in advisory_run
	assert 'echo "### Plan decision lint advisories"' in advisory_run
	assert 'cat "${decision_lint_stderr}" >&2' in advisory_run


def test_reuse_audit_contract_script_runs_cleanly() -> None:
	proc = subprocess.run(
		[sys.executable, str(REUSE_AUDIT_CONTRACT_TEST)],
		cwd=str(REPO_ROOT),
		env=_base_env(),
		text=True,
		capture_output=True,
		timeout=60,
	)
	assert proc.returncode == 0, proc.stderr or proc.stdout


def main() -> int:
	test_mode_plan_scope_mode_contract_defaults_on()
	test_mode_plan_scope_mode_contract_relaxes_when_flag_disabled()
	test_plan_workflow_exports_flag_and_keeps_live_prompt_parity()
	test_ci_workflow_keeps_plan_decisions_lint_contract()
	test_reuse_audit_contract_script_runs_cleanly()
	print("OK: plan scope-mode contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
