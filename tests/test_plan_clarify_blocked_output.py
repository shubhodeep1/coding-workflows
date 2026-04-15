#!/usr/bin/env python3
"""Regression checks for BLOCKED output handling in plan/clarify workflows."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
CLARIFY_WF = REPO_ROOT / ".github" / "workflows" / "clarify.yml"
PROMPT_PLAN = REPO_ROOT / "prompts" / "mode-plan.txt"
PROMPT_CLARIFY = REPO_ROOT / "prompts" / "mode-clarify.txt"


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def test_prompt_contract_includes_blocked_rule() -> None:
	plan_prompt = _read(PROMPT_PLAN)
	clarify_prompt = _read(PROMPT_CLARIFY)

	assert "emit exactly `BLOCKED: <short reason>`" in plan_prompt
	assert "emit exactly `BLOCKED: <short reason>`" in clarify_prompt
	assert "BLOCKED: <short reason>" in clarify_prompt


def test_plan_workflow_detects_blocked_before_needs_clarification() -> None:
	wf = _read(PLAN_WF)

	assert "- name: Parse planning output" in wf
	assert "if (/^\\s*BLOCKED:\\s*(.*\\S)\\s*$/i)" in wf
	assert "echo \"blocked=true\" >> \"$GITHUB_OUTPUT\"" in wf
	assert "- name: Handle blocked planning output" in wf
	assert "steps.parse_plan.outputs.blocked == 'true'" in wf
	assert "--add-label 'ai:blocked'" in wf
	assert "--remove-label 'ai:planning'" in wf
	assert "--remove-label 'ai:clarification'" in wf
	assert 'index("ai:blocked") != null' in wf
	assert "--remove-label 'ai:blocked'" in wf
	assert "--status \"blocked\"" in wf
	assert "steps.parse_plan.outputs.blocked != 'true' && steps.parse_plan.outputs.needs_clarification == 'true'" in wf


def test_plan_workflow_includes_ref_context_and_mismatch_rule() -> None:
	wf = _read(PLAN_WF)

	assert "- name: Capture planning ref context" in wf
	assert "git rev-parse HEAD" in wf
	assert "git symbolic-ref --short -q HEAD" in wf
	assert "PLANNING_REF_INTEGRATION_BRANCH_META" in wf
	assert "PLANNING REF CONTEXT" in wf
	assert "If checked-out ref mismatches Integration branch metadata, emit exactly" in wf
	assert "`BLOCKED: integration branch mismatch`" in wf


def test_clarify_workflow_detects_and_escalates_blocked_output() -> None:
	wf = _read(CLARIFY_WF)

	assert "- name: Parse Codex output" in wf
	assert "if (/^\\s*BLOCKED:\\s*(.*\\S)\\s*$/i)" in wf
	assert "echo \"blocked=true\" >> \"$GITHUB_OUTPUT\"" in wf
	assert "- name: Handle blocked clarification output" in wf
	assert "steps.parse_codex.outputs.blocked == 'true'" in wf
	assert "--add-label 'ai:blocked'" in wf
	assert "--remove-label 'ai:clarification'" in wf
	assert "--remove-label 'ai:planning'" in wf
	assert "steps.clarify_route.outputs.skip_codex != 'true' && steps.parse_codex.outputs.blocked != 'true' && steps.parse_codex.outputs.needs_clarification == 'true'" in wf
	assert "OUTCOME=\"blocked\"" in wf


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
