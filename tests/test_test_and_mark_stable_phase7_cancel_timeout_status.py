#!/usr/bin/env python3
"""Contract checks for how the release gate reports a Phase 7 cancel-on-close
run that is still unfinished when the wait budget runs out.

Run 40982514785 found cancel-on-close run #41000719920, but the run was still
not ``completed`` after ``PHASE7_WAIT_BUDGET_MINUTES``. The step printed
``concluded `` with an empty conclusion and wrote an empty ``status=``, and
the results table then read ``SKIPPED (no PR to close)`` and "ALL PHASES
PASSED". Phase 7 now reports ``status=timeout``, and the results table fails
a smoke PR whose Phase 7 reported no outcome.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

NO_RUN_MARKER = 'echo "status=no_run" >> "$GITHUB_OUTPUT"'
TIMEOUT_MARKER = 'if [ "${STATUS:-}" != "completed" ]; then'
CONCLUSION_MARKER = 'if [ "${NEW_CONCLUSION}" != "success" ]; then'
CANCEL_START = 'CANCEL="${{ steps.verify-cancel-on-close.outputs.status }}"'
CANCEL_EXPR = "${{ steps.verify-cancel-on-close.outputs.status }}"
UNCONFLICT_EXPR = "${{ steps.verify-cancel-on-close.outputs.unconflict }}"


def _read_workflow() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _step_script(step_id: str) -> str:
	doc = yaml.safe_load(_read_workflow())
	for step in doc["jobs"]["e2e-smoke-test"]["steps"]:
		if step.get("id") == step_id:
			return step["run"]
	raise AssertionError(f"{step_id} step not found")


def _timeout_tail() -> str:
	script = _step_script("verify-cancel-on-close")
	start = script.rfind(TIMEOUT_MARKER)
	assert start != -1, f"Missing marker: {TIMEOUT_MARKER}"
	# Keep the indentation of the marker line so dedent works.
	line_start = script.rfind("\n", 0, start) + 1
	return textwrap.dedent(script[line_start:])


def _timeout_block() -> str:
	tail = _timeout_tail()
	end = tail.find(CONCLUSION_MARKER)
	assert end != -1, f"Missing marker after timeout branch: {CONCLUSION_MARKER}"
	return tail[:end]


def _cancel_row_script() -> str:
	script = _step_script("verify")
	start = script.find(CANCEL_START)
	assert start != -1, f"Missing marker: {CANCEL_START}"
	line_start = script.rfind("\n", 0, start) + 1
	end = script.find('echo ""', start)
	assert end != -1, "Missing echo \"\" after the Cancel-PR row"
	body = textwrap.dedent(script[line_start:end])
	assert CANCEL_EXPR in body and UNCONFLICT_EXPR in body
	return body.replace(CANCEL_EXPR, "${STUB_CANCEL}").replace(UNCONFLICT_EXPR, "${STUB_UNCONFLICT}")


def _run_bash(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
	full_env.update(env)
	return subprocess.run(
		["bash", "-c", "set -euo pipefail\n" + script],
		env=full_env,
		capture_output=True,
		text=True,
		check=False,
	)


def test_timeout_branch_sits_between_no_run_and_conclusion_check() -> None:
	script = _step_script("verify-cancel-on-close")
	no_run = script.rfind(NO_RUN_MARKER)
	timeout = script.rfind(TIMEOUT_MARKER)
	conclusion = script.rfind(CONCLUSION_MARKER)
	assert no_run != -1 and timeout != -1 and conclusion != -1
	assert no_run < timeout < conclusion


def test_timeout_branch_reports_timeout_and_exits() -> None:
	block = _timeout_block()
	assert "did not complete within ${PHASE7_WAIT_BUDGET_MINUTES}m (status=${STATUS:-?})" in block
	assert 'echo "status=timeout" >> "$GITHUB_OUTPUT"' in block
	assert 'echo "cancel_run_id=${NEW_RUN_ID}" >> "$GITHUB_OUTPUT"' in block
	assert "exit 1" in block


def test_timeout_branch_behaviour() -> None:
	tail = _timeout_tail()
	cases = [
		("pending", "", 1, ["status=timeout", "cancel_run_id=41000719920"]),
		("completed", "failure", 1, ["status=failure", "cancel_run_id=41000719920"]),
		("completed", "success", 0, ["status=success", "cancel_run_id=41000719920"]),
	]
	for status, conclusion, want_rc, want_lines in cases:
		with tempfile.TemporaryDirectory() as tmp:
			out = Path(tmp) / "out"
			out.write_text("", encoding="utf-8")
			result = _run_bash(
				tail,
				{
					"GITHUB_OUTPUT": str(out),
					"NEW_RUN_ID": "41000719920",
					"PHASE7_WAIT_BUDGET_MINUTES": "10",
					"STATUS": status,
					"NEW_CONCLUSION": conclusion,
				},
			)
			lines = out.read_text(encoding="utf-8").splitlines()
		assert result.returncode == want_rc, (status, conclusion, result.stdout, result.stderr)
		assert lines == want_lines, (status, conclusion, lines)
		assert "status=" not in lines
		if status != "completed":
			assert "did not complete within 10m (status=pending)" in result.stdout
			assert "concluded" not in result.stdout


def test_results_cancel_row_behaviour() -> None:
	body = "PASSED=true\n" + _cancel_row_script() + '\necho "PASSED=$PASSED"\n'
	cases = [
		("", "8423", "FAILED (no status", "PASSED=false"),
		("", "", "SKIPPED (no PR to close)", "PASSED=true"),
		("timeout", "8423", "FAILED (timeout;", "PASSED=false"),
		("success", "8423", "PASSED (cancel_on_pr_close ran successfully", "PASSED=true"),
	]
	for cancel, pr_number, want_text, want_passed in cases:
		result = _run_bash(
			body,
			{"STUB_CANCEL": cancel, "STUB_UNCONFLICT": "not_needed", "PR_NUMBER": pr_number},
		)
		assert result.returncode == 0, (cancel, pr_number, result.stderr)
		assert want_text in result.stdout, (cancel, pr_number, result.stdout)
		assert result.stdout.strip().splitlines()[-1] == want_passed, (cancel, pr_number, result.stdout)


def test_phase7_and_verify_scripts_are_valid_bash() -> None:
	for step_id in ("verify-cancel-on-close", "verify"):
		result = subprocess.run(
			["bash", "-n"],
			input=_step_script(step_id),
			capture_output=True,
			text=True,
			check=False,
		)
		assert result.returncode == 0, (step_id, result.stderr)


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
