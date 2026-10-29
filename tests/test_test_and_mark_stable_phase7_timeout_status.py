#!/usr/bin/env python3
"""Phase 7 must report a cancel-on-close run that never finished as a timeout.

Run 40794181365 found the PR-close run but it stayed ``pending`` past the
budget. The step wrote an empty ``status=`` and the results table printed
``SKIPPED (no PR to close)`` and "ALL PHASES PASSED". These tests run the
real step text with a fake clock and listing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

WAIT_START = "DEADLINE=$(( $(date +%s) + (PHASE7_WAIT_BUDGET_MINUTES * 60) ))"
CONCLUSION_CHECK = 'if [ "${NEW_CONCLUSION}" != "success" ]; then'
TIMEOUT_ERROR = "did not complete within ${PHASE7_WAIT_BUDGET_MINUTES}m (status=${STATUS:-?})"


def _read_workflow() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _slice_between(text: str, start_marker: str, end_marker: str) -> str:
	start = text.find(start_marker)
	assert start != -1, f"Missing marker: {start_marker}"
	end = text.find(end_marker, start)
	assert end != -1, f"Missing marker after {start_marker}: {end_marker}"
	return text[start:end]


def _step_run(step_id: str) -> str:
	data = yaml.safe_load(_read_workflow())
	for step in data["jobs"]["e2e-smoke-test"]["steps"]:
		if step.get("id") == step_id:
			return step["run"]
	raise AssertionError(f"step {step_id} not found")


def _open_pr_wait_block() -> str:
	branch = _slice_between(
		_read_workflow(),
		"# PR is still open — exercise the close path end to end.",
		"# Per-phase soft-error analysis: cancel-on-pr-close",
	)
	return textwrap.dedent("          " + _slice_between(branch, WAIT_START, CONCLUSION_CHECK))


def _run_wait(runs: list) -> tuple[subprocess.CompletedProcess, str]:
	with tempfile.TemporaryDirectory() as tmp:
		clock = os.path.join(tmp, "clock")
		output = os.path.join(tmp, "github_output")
		runs_file = os.path.join(tmp, "runs.json")
		with open(clock, "w", encoding="utf-8") as fh:
			fh.write("1000\n")
		open(output, "w", encoding="utf-8").close()
		with open(runs_file, "w", encoding="utf-8") as fh:
			fh.write(json.dumps({"workflow_runs": runs}))
		prelude = "\n".join(
			[
				"set -euo pipefail",
				'date() { if [ "${1:-}" = "+%s" ]; then cat "$CLOCK"; else command date "$@"; fi; }',
				'sleep() { echo $(( $(cat "$CLOCK") + $1 )) > "$CLOCK"; }',
				'phase7_list_cancel_runs() { PHASE7_CANCEL_RUNS_JSON="$(cat "$RUNS_FILE")"; return 0; }',
				"gh() { return 1; }",
				"PR_NUMBER=42",
				"PR_CLOSED_AT=2026-10-20T00:00:00Z",
				"PRE_RUN_ID=100",
				"TEST_REPO=o/r",
				"",
			]
		)
		env = dict(
			os.environ,
			CLOCK=clock,
			RUNS_FILE=runs_file,
			GITHUB_OUTPUT=output,
			PHASE7_WAIT_BUDGET_MINUTES="1",
		)
		proc = subprocess.run(
			["bash", "-c", prelude + _open_pr_wait_block() + "\necho REACHED_CONCLUSION_CHECK\n"],
			env=env,
			capture_output=True,
			text=True,
			check=False,
		)
		with open(output, encoding="utf-8") as fh:
			github_output = fh.read()
	return proc, github_output


def test_pending_run_past_budget_writes_timeout() -> None:
	proc, out = _run_wait([{"id": 200, "event": "pull_request", "status": "pending", "conclusion": None}])
	assert proc.returncode == 1, (proc.stdout, proc.stderr)
	lines = out.splitlines()
	assert "status=timeout" in lines, out
	assert "cancel_run_id=200" in lines, out
	assert "status=" not in lines, out
	assert "::error::cancel-on-close run #200 did not complete within 1m (status=pending)" in proc.stdout
	assert "REACHED_CONCLUSION_CHECK" not in proc.stdout


def test_completed_run_reaches_conclusion_check() -> None:
	proc, out = _run_wait([{"id": 200, "event": "pull_request", "status": "completed", "conclusion": "success"}])
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert "REACHED_CONCLUSION_CHECK" in proc.stdout
	assert "status=timeout" not in out


def test_no_run_still_writes_no_run() -> None:
	proc, out = _run_wait([{"id": 99, "event": "pull_request", "status": "pending", "conclusion": None}])
	assert proc.returncode == 1, (proc.stdout, proc.stderr)
	assert "status=no_run" in out.splitlines()
	assert "status=timeout" not in out


def test_timeout_branch_sits_between_no_run_block_and_conclusion_check() -> None:
	script = _step_run("verify-cancel-on-close")
	open_start = script.index("# PR is still open — exercise the close path end to end.")
	no_run = script.index('if [ -z "${NEW_RUN_ID}" ]; then', open_start)
	error = script.index(TIMEOUT_ERROR, no_run)
	timeout = script.index('echo "status=timeout"', error)
	conclusion = script.index(CONCLUSION_CHECK, timeout)
	assert no_run < error < timeout < conclusion


def _summary_block() -> str:
	script = _step_run("verify")
	start = script.index('CANCEL="${{ steps.verify-cancel-on-close.outputs.status }}"')
	end = script.index('echo ""', start)
	block = script[start:end]
	block = block.replace("${{ steps.verify-cancel-on-close.outputs.status }}", "${T_CANCEL}")
	block = block.replace("${{ steps.verify-cancel-on-close.outputs.unconflict }}", "${T_UNCONFLICT}")
	assert "${{" not in block
	return block


def _run_summary(cancel: str, pr_number: str) -> str:
	env = dict(os.environ, T_CANCEL=cancel, T_UNCONFLICT="not_needed", PR_NUMBER=pr_number)
	proc = subprocess.run(
		["bash", "-c", "set -euo pipefail\nPASSED=true\n" + _summary_block() + '\necho "PASSED=$PASSED"\n'],
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)
	assert proc.returncode == 0, proc.stderr
	return proc.stdout


def test_summary_empty_status_with_pr_fails_closed() -> None:
	out = _run_summary("", "42")
	assert "FAILED (no status" in out
	assert "SKIPPED" not in out
	assert "PASSED=false" in out


def test_summary_empty_status_without_pr_is_skipped() -> None:
	out = _run_summary("", "")
	assert "SKIPPED (no PR to close)" in out
	assert "PASSED=true" in out


def test_summary_timeout_fails() -> None:
	out = _run_summary("timeout", "42")
	assert "FAILED (timeout" in out
	assert "PASSED=false" in out


def test_summary_success_passes() -> None:
	out = _run_summary("success", "42")
	assert "PASSED (" in out
	assert "PASSED=true" in out


def test_summary_step_script_is_valid_bash() -> None:
	script = re.sub(r"\$\{\{[^}]*\}\}", "X", _step_run("verify"))
	proc = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, check=False)
	assert proc.returncode == 0, proc.stderr


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
