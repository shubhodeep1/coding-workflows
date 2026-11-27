#!/usr/bin/env python3
"""Focused contract checks for plan reuse-audit prompt wiring."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_PROMPT = REPO_ROOT / "prompts" / "mode-plan.txt"
PLAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "plan.yml"
PLAN_RUNNER = REPO_ROOT / "scripts" / "run_plan_codex.sh"
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


def test_mode_plan_reuse_audit_contract_defaults_on() -> None:
	rendered = _render_mode_plan()

	assert "8. Scope-mode requirement gate (current render: `PLAN_SCOPE_MODE_REQUIRED=true`): emit" in rendered
	assert "9. Reuse-audit requirement gate (current render: `PLAN_REUSE_AUDIT_REQUIRED=true`): emit" in rendered
	assert "10. Scope-mode justification requirement gate (current render: `PLAN_SCOPE_MODE_REQUIRED=true`): emit" in rendered
	assert "PLAN_REUSE_AUDIT_REQUIRED=true" in rendered
	assert "`Reuse-audit: extends <existing-name>`" in rendered
	assert "`Reuse-audit: net-new (Layer 3) — <justification>`" in rendered
	assert "`scripts/gh_helpers.sh` and `scripts/memory_helpers.sh`" in rendered
	assert "`_fetch_candidate_issue_details_graphql`" in rendered
	assert "`_fetch_linked_pr_status_graphql`" in rendered
	assert "`ACTIVE_WORKFLOW_ISSUES`" in rendered
	assert "`STALL_MANAGED_LINKED_PR_CACHE`" in rendered
	assert "only propose net-new code when Layers 1 and 2 genuinely fail to" in rendered
	assert "When Layer 3 is necessary, justify why repo reuse fails;" in rendered
	assert "do not present net-new code as the default when a real reuse candidate" in rendered

	scope_idx = rendered.index("`Scope-mode: <Expansion | Selective Expansion | Hold Scope | Reduction>`")
	reuse_idx = rendered.index("`Reuse-audit: extends <existing-name>`")
	justification_idx = rendered.index("`Scope-mode justification:`")
	assert scope_idx < reuse_idx < justification_idx


def test_mode_plan_reuse_audit_contract_relaxes_when_flag_disabled() -> None:
	rendered = _render_mode_plan("--var", "PLAN_REUSE_AUDIT_REQUIRED=false")

	assert "PLAN_REUSE_AUDIT_REQUIRED=false" in rendered
	assert "Reuse-audit requirement gate (current render: `PLAN_REUSE_AUDIT_REQUIRED=false`)" in rendered
	assert "this line is optional but preferred" in rendered


def test_plan_workflow_exports_reuse_audit_flag_and_keeps_live_prompt_parity() -> None:
	workflow = PLAN_WORKFLOW.read_text(encoding="utf-8")
	plan_runner = PLAN_RUNNER.read_text(encoding="utf-8")

	assert "PLAN_REUSE_AUDIT_REQUIRED: ${{ vars.PLAN_REUSE_AUDIT_REQUIRED || 'true' }}" in workflow
	assert "9. Reuse-audit requirement gate (current render: `PLAN_REUSE_AUDIT_REQUIRED={{PLAN_REUSE_AUDIT_REQUIRED}}`): emit" in plan_runner
	assert "10. Scope-mode justification requirement gate (current render: `PLAN_SCOPE_MODE_REQUIRED={{PLAN_SCOPE_MODE_REQUIRED}}`): emit" in plan_runner
	assert "`Reuse-audit: extends <existing-name>`" in plan_runner
	assert "`Reuse-audit: net-new (Layer 3) — <justification>`" in plan_runner
	assert "`scripts/gh_helpers.sh` and `scripts/memory_helpers.sh`" in plan_runner
	assert "`_fetch_candidate_issue_details_graphql`" in plan_runner
	assert "`_fetch_linked_pr_status_graphql`" in plan_runner
	assert "`ACTIVE_WORKFLOW_ISSUES`" in plan_runner
	assert "`STALL_MANAGED_LINKED_PR_CACHE`" in plan_runner
	assert "only propose net-new code when Layers 1 and 2 genuinely fail to" in plan_runner
	assert "When Layer 3 is necessary, justify why repo reuse fails;" in plan_runner
	assert "do not present net-new code as the default when a real reuse candidate" in plan_runner


def main() -> int:
	test_mode_plan_reuse_audit_contract_defaults_on()
	test_mode_plan_reuse_audit_contract_relaxes_when_flag_disabled()
	test_plan_workflow_exports_reuse_audit_flag_and_keeps_live_prompt_parity()
	print("OK: plan reuse-audit contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
