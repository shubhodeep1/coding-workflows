#!/usr/bin/env python3
"""Contract test for Inventory parity CI step ordering."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WF = REPO_ROOT / ".github" / "workflows" / "ci.yml"

INVENTORY_PARITY_STEP = "Inventory parity"
WORKFLOW_LOG_COLLECTOR_COVERAGE_STEP = "Workflow log collector coverage gate"
WORKFLOW_LOG_ANALYZER_COVERAGE_STEP = "Workflow log analyzer coverage gate"
INVENTORY_PARITY_COMMAND = "PYTHONDONTWRITEBYTECODE=1 python3 tests/inventory_parity.py"


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
