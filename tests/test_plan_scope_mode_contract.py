#!/usr/bin/env python3
"""Focused contract checks for plan scope-mode prompt wiring."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_PROMPT = REPO_ROOT / "prompts" / "mode-plan.txt"
PLAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "plan.yml"
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


def test_mode_plan_scope_mode_contract_defaults_on() -> None:
	rendered = _render_mode_plan()

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

	assert "PLAN_SCOPE_MODE_REQUIRED=false" in rendered
	assert "optional but preferred" in rendered
	assert "whenever `Scope-mode:` is present" in rendered


def test_plan_workflow_exports_flag_and_keeps_live_prompt_parity() -> None:
	workflow = PLAN_WORKFLOW.read_text(encoding="utf-8")

	assert "PLAN_SCOPE_MODE_REQUIRED: ${{ vars.PLAN_SCOPE_MODE_REQUIRED || 'true' }}" in workflow
	assert "PLAN_SCOPE_MODE_REQUIRED={{PLAN_SCOPE_MODE_REQUIRED}}" in workflow
	assert "`Scope-mode: <Expansion | Selective Expansion | Hold Scope | Reduction>`" in workflow
	assert "`Scope-mode justification:`" in workflow
	assert "Boil the Lake forcing question" in workflow
	assert "Security > Correctness & safety > Backward compatibility" in workflow
	assert "Operational clarity > Performance > Speed; completeness moves correctness." in workflow
	assert "Reduction safety net" in workflow
	assert "MUST emit a clarification Q-ID" in workflow
	assert "existing scope-too-large gate" in workflow


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
	test_reuse_audit_contract_script_runs_cleanly()
	print("OK: plan scope-mode contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
