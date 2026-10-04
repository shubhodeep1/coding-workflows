#!/usr/bin/env python3
"""Contract test for Inventory parity CI step ordering.

Since #4707 split CI into parallel jobs, line order in `ci.yml` only means
run order inside one job, so the steps must also share a job.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import re
from pathlib import Path
from unittest.mock import patch

import inventory_parity


REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WF = REPO_ROOT / ".github" / "workflows" / "ci.yml"

INVENTORY_PARITY_STEP = "Inventory parity"
WORKFLOW_LOG_COLLECTOR_COVERAGE_STEP = "Workflow log collector coverage gate"
WORKFLOW_LOG_ANALYZER_COVERAGE_STEP = "Workflow log analyzer coverage gate"
INVENTORY_PARITY_COMMAND = "PYTHONDONTWRITEBYTECODE=1 python3 tests/inventory_parity.py"
JOB_KEY_LINE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")


def _workflow_lines() -> list[str]:
	try:
		return CI_WF.read_text(encoding="utf-8").splitlines()
	except (OSError, UnicodeError) as exc:
		raise AssertionError(f"Unable to read {CI_WF}: {type(exc).__name__}: {exc}") from exc


def _step_start_line(lines: list[str], step_name: str) -> int:
	marker = f"- name: {step_name}"
	for idx, line in enumerate(lines):
		if line.lstrip() == marker:
			return idx
	raise AssertionError(f"Missing workflow step: {step_name} in {CI_WF}")


def _step_block(lines: list[str], step_name: str) -> str:
	start = _step_start_line(lines, step_name)
	indent = len(lines[start]) - len(lines[start].lstrip())
	block = [lines[start]]
	for line in lines[start + 1 :]:
		stripped = line.lstrip()
		line_indent = len(line) - len(stripped)
		if stripped and line_indent < indent:
			break
		if stripped.startswith("- ") and line_indent == indent:
			break
		block.append(line)
	return "\n".join(block)


def _step_job(lines: list[str], step_name: str) -> str:
	start = _step_start_line(lines, step_name)
	for line in reversed(lines[:start]):
		if line == "jobs:":
			break
		match = JOB_KEY_LINE.match(line)
		if match:
			return match.group(1)
	raise AssertionError(f"No job encloses workflow step: {step_name} in {CI_WF}")


def test_inventory_parity_shares_a_job_with_the_coverage_gates() -> None:
	lines = _workflow_lines()
	parity_job = _step_job(lines, INVENTORY_PARITY_STEP)
	for gate_step in (WORKFLOW_LOG_COLLECTOR_COVERAGE_STEP, WORKFLOW_LOG_ANALYZER_COVERAGE_STEP):
		gate_job = _step_job(lines, gate_step)
		assert gate_job == parity_job, (
			f"{gate_step!r} runs in job {gate_job!r} but {INVENTORY_PARITY_STEP!r} runs in "
			f"job {parity_job!r}; parallel jobs do not order their steps"
		)


def test_inventory_parity_runs_before_workflow_log_collector_coverage_gate() -> None:
	lines = _workflow_lines()
	assert _step_start_line(lines, INVENTORY_PARITY_STEP) < _step_start_line(
		lines,
		WORKFLOW_LOG_COLLECTOR_COVERAGE_STEP,
	)


def test_inventory_parity_runs_before_workflow_log_analyzer_coverage_gate() -> None:
	lines = _workflow_lines()
	assert _step_start_line(lines, INVENTORY_PARITY_STEP) < _step_start_line(
		lines,
		WORKFLOW_LOG_ANALYZER_COVERAGE_STEP,
	)


def test_inventory_parity_step_keeps_exact_command_text() -> None:
	lines = _workflow_lines()
	assert INVENTORY_PARITY_COMMAND in _step_block(lines, INVENTORY_PARITY_STEP)


def test_inventory_parity_reports_first_missing_and_extra_paths() -> None:
	expected = {section: [] for section in inventory_parity.SECTION_ORDER}
	expected["Scripts"] = ["scripts/a.sh", "scripts/c.sh"]
	for documented_scripts, location, missing, extra in (
		(["scripts/c.sh"], "index 0: expected=scripts/a.sh actual=scripts/c.sh", "missing scripts/a.sh", None),
		(["scripts/a.sh", "scripts/b.sh", "scripts/c.sh"], "index 1: expected=scripts/c.sh actual=scripts/b.sh", None, "unexpected scripts/b.sh"),
		(["scripts/a.sh", "scripts/c.sh", "scripts/z.sh"], "index 2: expected=<end> actual=scripts/z.sh", None, "unexpected scripts/z.sh"),
		(["scripts/a.sh"], "index 1: expected=scripts/c.sh actual=<end>", "missing scripts/c.sh", None),
	):
		documented = {section: [] for section in inventory_parity.SECTION_ORDER}
		documented["Scripts"] = documented_scripts
		output = StringIO()
		with (
			patch.object(inventory_parity, "load_exemptions", return_value={}),
			patch.object(inventory_parity, "parse_inventory", return_value=documented),
			patch.object(inventory_parity, "collect_expected_paths", return_value=expected),
			patch.object(inventory_parity, "validate_secondary_documents", return_value=[]),
			redirect_stdout(output),
		):
			assert inventory_parity.main() == 1
		assert f"Scripts: first differing path at {location}" in output.getvalue()
		if missing:
			assert missing in output.getvalue()
		if extra:
			assert extra in output.getvalue()

	# Sorting the diagnostic must not turn ordering into a new parity failure.
	documented = {section: [] for section in inventory_parity.SECTION_ORDER}
	documented["Scripts"] = list(reversed(expected["Scripts"]))
	with (
		patch.object(inventory_parity, "load_exemptions", return_value={}),
		patch.object(inventory_parity, "parse_inventory", return_value=documented),
		patch.object(inventory_parity, "collect_expected_paths", return_value=expected),
		patch.object(inventory_parity, "validate_secondary_documents", return_value=[]),
	):
		assert inventory_parity.main() == 0


def main() -> int:
	tests = [
		value
		for key, value in sorted(globals().items())
		if key.startswith("test_") and callable(value)
	]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
