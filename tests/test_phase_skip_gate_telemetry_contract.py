#!/usr/bin/env python3
"""Contract tests for phase skip/gate telemetry in reusable workflows."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CLARIFY_WF = REPO_ROOT / ".github" / "workflows" / "clarify.yml"
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
IMPLEMENT_WF = REPO_ROOT / ".github" / "workflows" / "implement.yml"
ORCH_CLARIFY_RESPOND_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_clarify_respond.yml"
CI_WF = REPO_ROOT / ".github" / "workflows" / "ci.yml"
AGENTS_MD = REPO_ROOT / "agents.md"


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _step_block(path: Path, step_name: str) -> str:
	marker = f"- name: {step_name}"
	lines = _read(path).splitlines()
	start = next((i for i, line in enumerate(lines) if line.lstrip() == marker), -1)
	assert start != -1, f"Missing workflow step: {step_name} in {path}"
	indent = len(lines[start]) - len(lines[start].lstrip())
	block = [lines[start]]
	for line in lines[start + 1 :]:
		stripped = line.lstrip()
		line_indent = len(line) - len(stripped)
		if stripped and line_indent < indent:
			break
		if stripped.startswith("- name:") and line_indent == indent:
			break
		block.append(line)
	return "\n".join(block)


def _assert_before(block: str, earlier: str, later: str) -> None:
	earlier_idx = block.find(earlier)
	assert earlier_idx != -1, f"Missing earlier marker: {earlier}"
	later_idx = block.find(later, earlier_idx)
	assert later_idx != -1, f"Missing later marker after {earlier}: {later}"
	assert earlier_idx < later_idx, f"Expected {earlier!r} before {later!r}"


def test_clarify_route_emits_stable_gate_telemetry() -> None:
	block = _step_block(CLARIFY_WF, "Decide clarify route")

	assert "AI_PHASE_GATE_V1 phase=clarify gate=route reason=issue_closed outcome=skip issue=${ISSUE_NUMBER}" in block
	assert "AI_PHASE_GATE_V1 phase=clarify gate=route reason=orchestrator_fast_path outcome=defer issue=${ISSUE_NUMBER}" in block


def test_plan_gate_steps_emit_stable_skip_and_defer_telemetry() -> None:
	validate_block = _step_block(PLAN_WF, "Validate planning phase label")
	assert "AI_PHASE_GATE_V1 phase=plan gate=trigger_validation reason=invalid_trigger_body outcome=skip issue=${ISSUE_NUMBER}" in validate_block
	assert "AI_PHASE_GATE_V1 phase=plan gate=trigger_validation reason=issue_closed outcome=skip issue=${ISSUE_NUMBER}" in validate_block
	assert "AI_PHASE_GATE_V1 phase=plan gate=trigger_validation reason=missing_expected_phase_label outcome=skip issue=${ISSUE_NUMBER}" in validate_block
	_assert_before(
		validate_block,
		"AI_PHASE_GATE_V1 phase=plan gate=trigger_validation reason=invalid_trigger_body outcome=skip issue=${ISSUE_NUMBER}",
		"exit 0",
	)

	existing_pr_block = _step_block(PLAN_WF, "Skip when issue already has a PR")
	assert "AI_PHASE_GATE_V1 phase=plan gate=existing_pr_check reason=existing_pr outcome=skip issue=${ISSUE_NUMBER}" in existing_pr_block

	stale_block = _step_block(PLAN_WF, "Skip stale /answer comments")
	assert "AI_PHASE_GATE_V1 phase=plan gate=comment_freshness reason=no_answer_comment outcome=skip issue=${ISSUE_NUMBER}" in stale_block
	assert "AI_PHASE_GATE_V1 phase=plan gate=comment_freshness reason=stale_answer_comment outcome=skip issue=${ISSUE_NUMBER} trigger_comment_id=${TRIGGER_COMMENT_ID} latest_comment_id=${LATEST_ANSWER_COMMENT_ID}" in stale_block
	_assert_before(
		stale_block,
		"AI_PHASE_GATE_V1 phase=plan gate=comment_freshness reason=no_answer_comment outcome=skip issue=${ISSUE_NUMBER}",
		"exit 0",
	)

	claim_block = _step_block(PLAN_WF, "Check and claim /answer command")
	assert "AI_PHASE_GATE_V1 phase=plan gate=command_claim reason=already_processed outcome=skip issue=${ISSUE_NUMBER} comment_id=${TRIGGER_COMMENT_ID}" in claim_block
	assert "AI_PHASE_GATE_V1 phase=plan gate=command_claim reason=claimed_elsewhere outcome=skip issue=${ISSUE_NUMBER} comment_id=${TRIGGER_COMMENT_ID}" in claim_block

	auto_answer_block = _step_block(PLAN_WF, "Evaluate orchestrator auto-answer eligibility")
	assert "AI_PHASE_GATE_V1 phase=plan gate=orchestrator_auto_answer reason=not_orchestrator_managed outcome=defer issue=${ISSUE_NUMBER}" in auto_answer_block
	assert "AI_PHASE_GATE_V1 phase=plan gate=orchestrator_auto_answer reason=parse_failed outcome=defer issue=${ISSUE_NUMBER}" in auto_answer_block

	auto_approve_block = _step_block(PLAN_WF, "Auto-approve clear plan")
	assert "AI_PHASE_GATE_V1 phase=plan gate=auto_approve reason=issue_closed outcome=defer issue=${ISSUE_NUMBER}" in auto_approve_block
	assert "AI_PHASE_GATE_V1 phase=plan gate=auto_approve reason=auto_implement_disabled outcome=defer issue=${ISSUE_NUMBER}" in auto_approve_block


def test_implement_gate_steps_emit_stable_skip_telemetry() -> None:
	precheck_block = _step_block(IMPLEMENT_WF, "Precheck approval phase label")
	assert "AI_PHASE_GATE_V1 phase=implement gate=phase_precheck reason=issue_closed outcome=skip issue=${ISSUE_NUMBER}" in precheck_block
	assert "AI_PHASE_GATE_V1 phase=implement gate=phase_precheck reason=already_implementing outcome=skip issue=${ISSUE_NUMBER}" in precheck_block
	assert "AI_PHASE_GATE_V1 phase=implement gate=phase_precheck reason=wrong_phase outcome=skip issue=${ISSUE_NUMBER}" in precheck_block

	existing_pr_block = _step_block(IMPLEMENT_WF, "Exit when existing PR is found")
	assert "AI_PHASE_GATE_V1 phase=implement gate=existing_pr_check reason=existing_pr outcome=skip issue=${ISSUE_NUMBER}" in existing_pr_block
	_assert_before(
		existing_pr_block,
		"AI_PHASE_GATE_V1 phase=implement gate=existing_pr_check reason=existing_pr outcome=skip issue=${ISSUE_NUMBER}",
		"exit 0",
	)

	validate_block = _step_block(IMPLEMENT_WF, "Validate approval phase label")
	assert "AI_PHASE_GATE_V1 phase=implement gate=phase_validation reason=destructive_blocked outcome=skip issue=${ISSUE_NUMBER}" in validate_block
	assert "AI_PHASE_GATE_V1 phase=implement gate=phase_validation reason=scope_blocked outcome=skip issue=${ISSUE_NUMBER}" in validate_block
	assert "AI_PHASE_GATE_V1 phase=implement gate=phase_validation reason=wrong_phase outcome=skip issue=${ISSUE_NUMBER}" in validate_block

	claim_block = _step_block(IMPLEMENT_WF, "Claim /approved command")
	assert "AI_PHASE_GATE_V1 phase=implement gate=command_claim reason=already_processed outcome=skip issue=${ISSUE_NUMBER} comment_id=${APPROVAL_COMMENT_ID}" in claim_block
	assert "AI_PHASE_GATE_V1 phase=implement gate=command_claim reason=claimed_elsewhere outcome=skip issue=${ISSUE_NUMBER} comment_id=${APPROVAL_COMMENT_ID}" in claim_block


def test_orchestrate_clarify_respond_gate_steps_emit_stable_telemetry() -> None:
	metadata_block = _step_block(ORCH_CLARIFY_RESPOND_WF, "Check orchestrator metadata")
	assert "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=orchestrator_metadata reason=not_orchestrator_managed outcome=skip issue=${ISSUE_NUMBER}" in metadata_block

	parse_block = _step_block(ORCH_CLARIFY_RESPOND_WF, "Parse and post answer")
	assert "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=command_claim reason=already_processed outcome=skip issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID}" in parse_block
	assert "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=command_claim reason=claimed_elsewhere outcome=skip issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID}" in parse_block
	assert "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=auto_answer reason=escalate_requested outcome=defer issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID} cycle=${CYCLE} max_cycles=${MAX_CYCLES}" in parse_block
	assert "AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=auto_answer reason=loop_guard_blocked outcome=defer issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID} loop_reason=${LOOP_REASON} cycle=${CYCLE} max_cycles=${MAX_CYCLES}" in parse_block
	_assert_before(
		parse_block,
		"AI_PHASE_GATE_V1 phase=orchestrate_clarify_respond gate=auto_answer reason=loop_guard_blocked outcome=defer issue=${ISSUE_NUMBER} comment_id=${CLARIFICATION_COMMENT_ID} loop_reason=${LOOP_REASON} cycle=${CYCLE} max_cycles=${MAX_CYCLES}",
		"exit 0",
	)


def test_agents_and_ci_register_phase_gate_contract() -> None:
	agents_text = _read(AGENTS_MD)
	assert "- `AI_PHASE_GATE_V1`" in agents_text

	ci_text = _read(CI_WF)
	assert "PYTHONDONTWRITEBYTECODE=1 python3 tests/test_phase_skip_gate_telemetry_contract.py" in ci_text


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
