#!/usr/bin/env python3
"""Contract checks for test-and-mark-stable wait-plan polling guards."""

from __future__ import annotations

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parent.parent
WF_PATH = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"


def _workflow_text() -> str:
	return WF_PATH.read_text(encoding="utf-8")


def test_wait_plan_guards_non_numeric_active_plan_output() -> None:
	wf = _workflow_text()
	assert "PLAN_RUNS_JSON='{\"workflow_runs\":[]}'" in wf
	assert "if _PLAN_RUNS_JSON_RAW=$(gh api \"repos/${TEST_REPO}/actions/runs?per_page=50&created=>${{ steps.create-issue.outputs.created_after }}\" 2>/dev/null); then" in wf
	assert "| jq -se 'length == 1 and (.[0] | type == \"object\" and (.workflow_runs | type == \"array\"))'" in wf
	assert "if _ACTIVE_PLAN_COUNT=$(printf '%s' \"$PLAN_RUNS_JSON\"" in wf
	assert "if ! printf '%s' \"$OTHER_ACTIVE_PLAN_RUNS\" | grep -Eq '^[0-9]+$'; then" in wf
	legacy_pattern = re.compile(
		r"OTHER_ACTIVE_PLAN_RUNS=\$\(\s*gh api .*?\|\| echo \"0\"\s*\)",
		re.DOTALL,
	)
	assert legacy_pattern.search(wf) is None


def test_wait_plan_keeps_wait_behavior_for_active_plan_runs() -> None:
	wf = _workflow_text()
	assert 'if [ "$OTHER_ACTIVE_PLAN_RUNS" -gt 0 ]; then' in wf
	assert "other active Plan run(s)" in wf


if __name__ == "__main__":
	test_wait_plan_guards_non_numeric_active_plan_output()
	test_wait_plan_keeps_wait_behavior_for_active_plan_runs()
