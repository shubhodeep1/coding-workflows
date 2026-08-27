#!/usr/bin/env python3
"""Regression checks for BLOCKED output handling in plan/clarify workflows."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
PLAN_RUNNER = REPO_ROOT / "scripts" / "run_plan_codex.sh"
CLARIFY_WF = REPO_ROOT / ".github" / "workflows" / "clarify.yml"
PROMPT_PLAN = REPO_ROOT / "prompts" / "mode-plan.txt"
PROMPT_CLARIFY = REPO_ROOT / "prompts" / "mode-clarify.txt"

FENCE_SANITIZER = r"s{(^|\n)([ \t]*((?:```|~~~))[^\n]*\n.*?\n[ \t]*\3[ \t]*(?=\n|$))}{$1}gms;"
BLOCKED_RE = re.compile(r"^\s*BLOCKED:\s*(.*\S)\s*$", re.IGNORECASE)
SELF_CHECK_PASS_RE = re.compile(r"^\s*PLAN_SELF_CHECK:\s*PASS\s*$", re.IGNORECASE)
SELF_CHECK_WARNING_RE = re.compile(
	r"^\s*PLAN_SELF_CHECK:\s*WARNING:\s*(.*\S)\s*$",
	re.IGNORECASE,
)
SELF_CHECK_BLOCKER_RE = re.compile(r"^\s*PLAN_SELF_CHECK:\s*BLOCKER:\s*(.*\S)\s*$", re.IGNORECASE)
STATUS_NEEDS_CLARIFICATION_RE = re.compile(
	r"STATUS:.*NEEDS_CLARIFICATION|^\*\*STATUS:\*\*.*NEEDS_CLARIFICATION",
	re.IGNORECASE,
)
Q_LINE_RE = re.compile(r"^\s*(?:\*\*)?Q[0-9]+(?:\*\*)?:(?:\*\*)?\s+", re.IGNORECASE)
CHOICES_RE = re.compile(r"^\s*Choices:\s*$", re.IGNORECASE)
RECOMMENDED_CHOICE_RE = re.compile(
	r"^\s*-\s*(?:\*\*)?[A-Za-z](?:\+[A-Za-z])*(?:\*\*)?\s*(?:—|–|[-)\.:]).*\(RECOMMENDED\)",
	re.IGNORECASE,
)


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _sanitize_plan_output(raw_output: str) -> str:
	with tempfile.TemporaryDirectory() as td:
		source_path = Path(td) / "codex_output.txt"
		source_path.write_text(raw_output, encoding="utf-8")
		result = subprocess.run(
			["perl", "-0pe", FENCE_SANITIZER, str(source_path)],
			capture_output=True,
			check=True,
			encoding="utf-8",
		)
		return result.stdout


def _blocked_reason(parsed_output: str) -> str:
	for line in parsed_output.splitlines():
		match = BLOCKED_RE.match(line)
		if match:
			return match.group(1)
	return ""


def _self_check_summary(parsed_output: str, *, self_check_gate_enabled: bool) -> dict[str, str | int | bool]:
	pass_count = 0
	warning_count = 0
	blocker_count = 0
	for line in parsed_output.splitlines():
		if SELF_CHECK_PASS_RE.match(line):
			pass_count += 1
		elif SELF_CHECK_WARNING_RE.match(line):
			warning_count += 1
		elif SELF_CHECK_BLOCKER_RE.match(line):
			blocker_count += 1

	state = "missing"
	observation = "none"
	if blocker_count > 0:
		state = "blocker"
		if pass_count > 0 or warning_count > 0:
			observation = "mixed"
	elif warning_count > 0:
		state = "warning"
		if pass_count > 0:
			observation = "mixed"
	elif pass_count == 1:
		state = "pass"
	elif pass_count > 1:
		state = "pass"
		observation = "duplicate-pass"

	reopen = self_check_gate_enabled and blocker_count > 0
	if state == "missing" and self_check_gate_enabled:
		observation = "missing"

	return {
		"pass_count": pass_count,
		"warning_count": warning_count,
		"blocker_count": blocker_count,
		"state": state,
		"observation": observation,
		"reopen": reopen,
	}


def _has_structured_clarification_block(parsed_output: str) -> bool:
	q_line: int | None = None
	for line_number, line in enumerate(parsed_output.splitlines(), start=1):
		if Q_LINE_RE.match(line):
			q_line = line_number
			continue
		if q_line is None or line_number - q_line > 20:
			continue
		if CHOICES_RE.match(line) or RECOMMENDED_CHOICE_RE.match(line):
			return True
	return False


def _needs_clarification(parsed_output: str, *, self_check_gate_enabled: bool) -> bool:
	has_status_needs_clarification = any(
		STATUS_NEEDS_CLARIFICATION_RE.search(line) for line in parsed_output.splitlines()
	)
	self_check_reopen = _self_check_summary(
		parsed_output,
		self_check_gate_enabled=self_check_gate_enabled,
	)["reopen"]
	return has_status_needs_clarification or _has_structured_clarification_block(parsed_output) or self_check_reopen


def test_prompt_contract_includes_blocked_rule() -> None:
	plan_prompt = _read(PROMPT_PLAN)
	clarify_prompt = _read(PROMPT_CLARIFY)

	assert "emit exactly `BLOCKED: <short reason>`" in plan_prompt
	assert "## Pre-execution self-check" in plan_prompt
	assert "PLAN_SELF_CHECK: PASS" in plan_prompt
	assert "PLAN_SELF_CHECK: WARNING:" in plan_prompt
	assert "PLAN_SELF_CHECK: BLOCKER:" in plan_prompt
	assert "STATUS: NOT_CLEAR" in plan_prompt
	assert "emit exactly `BLOCKED: <short reason>`" in clarify_prompt
	assert "BLOCKED: <short reason>" in clarify_prompt


def test_plan_workflow_detects_blocked_before_needs_clarification() -> None:
	wf = _read(PLAN_WF)
	plan_runner = _read(PLAN_RUNNER)
	blocked_section = wf.split('BLOCKED_REASON="$(perl -ne \'', 1)[1].split(
		'if [ -n "${BLOCKED_REASON}" ]; then', 1
	)[0]

	assert "- name: Parse planning output" in wf
	assert "PLAN_SELF_CHECK_ENABLED: ${{ vars.PLAN_SELF_CHECK_ENABLED || 'true' }}" in wf
	assert "if (/^\\s*BLOCKED:\\s*(.*\\S)\\s*$/i)" in wf
	assert "echo \"blocked=true\" >> \"$GITHUB_OUTPUT\"" in wf
	assert "${CODEX_OUTPUT_PARSE_FILE}" in blocked_section
	assert "${CODEX_OUTPUT_FILE}" not in blocked_section
	assert "$in_code" not in blocked_section
	assert "7. A pre-execution self-check result" in plan_runner
	assert "PLAN_SELF_CHECK: PASS" in plan_runner
	assert "PLAN_SELF_CHECK: WARNING:" in plan_runner
	assert "PLAN_SELF_CHECK: BLOCKER:" in plan_runner
	assert 'CODEX_OUTPUT_PARSE_FILE="${RUNTIME_DIR}/codex_output_parse.txt"' in wf
	assert "malformed fences do not" in wf
	assert '::error::Failed to sanitize Codex output' in wf
	assert 'if [ ! -s "${CODEX_OUTPUT_PARSE_FILE}" ]; then' in wf
	assert '::error::Sanitized Codex output parse file missing or empty' in wf
	assert "SELF_CHECK_PASS_COUNT" in wf
	assert "SELF_CHECK_WARNING_COUNT" in wf
	assert "SELF_CHECK_BLOCKER_COUNT" in wf
	assert "plan_self_check_state" in wf
	assert "plan_self_check_observation" in wf
	assert "plan_self_check_reopen_clarification" in wf
	assert 'PARSE_SOURCE_FILE="${CODEX_OUTPUT_PARSE_FILE:-${CODEX_OUTPUT_FILE}}"' in wf
	assert "- name: Handle blocked planning output" in wf
	assert "steps.parse_plan.outputs.blocked == 'true'" in wf
	assert "--add-label 'ai:blocked'" in wf
	assert "--remove-label 'ai:planning'" in wf
	assert "--remove-label 'ai:clarification'" in wf
	assert 'index("ai:blocked") != null' in wf
	assert "--remove-label 'ai:blocked'" in wf
	assert "--status \"blocked\"" in wf
	assert '[ "${PLAN_SELF_CHECK_GATE_ENABLED}" = "true" ] && [ "${SELF_CHECK_BLOCKER_COUNT}" -gt 0 ]' in wf
	assert '[ "${HAS_STATUS_NEEDS_CLARIFICATION}" = "true" ] || [ "${HAS_STRUCTURED_CLARIFICATION_BLOCK}" = "true" ] || [ "${SELF_CHECK_REOPEN_CLARIFICATION}" = "true" ]' in wf
	assert "steps.parse_plan.outputs.blocked != 'true' && steps.parse_plan.outputs.needs_clarification == 'true'" in wf


def test_plan_workflow_includes_ref_context_and_mismatch_rule() -> None:
	wf = _read(PLAN_WF)
	plan_runner = _read(PLAN_RUNNER)

	assert "- name: Capture planning ref context" in wf
	assert "git rev-parse HEAD" in wf
	assert "git symbolic-ref --short -q HEAD" in wf
	assert "PLANNING_REF_INTEGRATION_BRANCH_META" in wf
	assert "PLANNING REF CONTEXT" in wf
	assert "If checked-out ref mismatches Integration branch metadata, emit exactly" in plan_runner
	assert "`BLOCKED: integration branch mismatch`" in plan_runner


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


def test_unterminated_fence_does_not_hide_blocked_reason() -> None:
	parsed_output = _sanitize_plan_output(
		"```text\nexample\nBLOCKED: integration branch mismatch\n"
	)

	assert _blocked_reason(parsed_output) == "integration branch mismatch"


def test_unterminated_fence_reopens_clarification_for_self_check_blocker() -> None:
	parsed_output = _sanitize_plan_output(
		"```python\nprint('demo')\nPLAN_SELF_CHECK: BLOCKER: missing files_touched list\n"
	)

	summary_enabled = _self_check_summary(parsed_output, self_check_gate_enabled=True)
	summary_disabled = _self_check_summary(parsed_output, self_check_gate_enabled=False)

	assert summary_enabled["blocker_count"] == 1
	assert summary_enabled["state"] == "blocker"
	assert summary_enabled["observation"] == "none"
	assert summary_enabled["reopen"] is True
	assert summary_disabled["reopen"] is False
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is True
	assert _needs_clarification(parsed_output, self_check_gate_enabled=False) is False


def test_pass_self_check_stays_clear() -> None:
	parsed_output = _sanitize_plan_output(
		"Implementation Plan\nPLAN_SELF_CHECK: PASS\nSTATUS: CLEAR\n"
	)

	summary = _self_check_summary(parsed_output, self_check_gate_enabled=True)

	assert summary["pass_count"] == 1
	assert summary["warning_count"] == 0
	assert summary["blocker_count"] == 0
	assert summary["state"] == "pass"
	assert summary["observation"] == "none"
	assert summary["reopen"] is False
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is False


def test_warning_only_self_check_remains_fail_open() -> None:
	parsed_output = _sanitize_plan_output(
		"Implementation Plan\nPLAN_SELF_CHECK: WARNING: confirm rollout note\nSTATUS: CLEAR\n"
	)

	summary = _self_check_summary(parsed_output, self_check_gate_enabled=True)

	assert summary["pass_count"] == 0
	assert summary["warning_count"] == 1
	assert summary["blocker_count"] == 0
	assert summary["state"] == "warning"
	assert summary["observation"] == "none"
	assert summary["reopen"] is False
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is False


def test_mixed_pass_and_warning_self_check_is_observability_only() -> None:
	parsed_output = _sanitize_plan_output(
		"Implementation Plan\nPLAN_SELF_CHECK: PASS\nPLAN_SELF_CHECK: WARNING: confirm rollout note\nSTATUS: CLEAR\n"
	)

	summary = _self_check_summary(parsed_output, self_check_gate_enabled=True)

	assert summary["pass_count"] == 1
	assert summary["warning_count"] == 1
	assert summary["blocker_count"] == 0
	assert summary["state"] == "warning"
	assert summary["observation"] == "mixed"
	assert summary["reopen"] is False
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is False


def test_duplicate_pass_self_check_is_observability_only() -> None:
	parsed_output = _sanitize_plan_output(
		"Implementation Plan\nPLAN_SELF_CHECK: PASS\nPLAN_SELF_CHECK: PASS\nSTATUS: CLEAR\n"
	)

	summary = _self_check_summary(parsed_output, self_check_gate_enabled=True)

	assert summary["pass_count"] == 2
	assert summary["warning_count"] == 0
	assert summary["blocker_count"] == 0
	assert summary["state"] == "pass"
	assert summary["observation"] == "duplicate-pass"
	assert summary["reopen"] is False
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is False


def test_missing_self_check_is_observability_only() -> None:
	parsed_output = _sanitize_plan_output("Implementation Plan\nSTATUS: CLEAR\n")

	summary = _self_check_summary(parsed_output, self_check_gate_enabled=True)

	assert summary["pass_count"] == 0
	assert summary["warning_count"] == 0
	assert summary["blocker_count"] == 0
	assert summary["state"] == "missing"
	assert summary["observation"] == "missing"
	assert summary["reopen"] is False
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is False


def test_unterminated_fence_does_not_hide_structured_clarification_block() -> None:
	parsed_output = _sanitize_plan_output(
		"```\nnotes\nQ1: Which branch should be used?\nChoices:\n- A - integration branch (RECOMMENDED)\n"
	)

	assert _has_structured_clarification_block(parsed_output) is True
	assert _needs_clarification(parsed_output, self_check_gate_enabled=True) is True


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
