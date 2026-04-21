#!/usr/bin/env python3
"""Regression checks for plan-run polling guard in test-and-mark-stable workflow."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"


def _read_workflow() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def test_other_active_plan_runs_avoids_inline_fallback_substitution() -> None:
	wf = _read_workflow()

	assert "OTHER_ACTIVE_PLAN_RUNS=$(gh api \"repos/${TEST_REPO}/actions/runs?per_page=50&created=>${{ steps.create-issue.outputs.created_after }}\"" not in wf
	assert "--jq '[.workflow_runs[] | select(.name | test(\"Plan\"; \"i\")) | select(.status == \"in_progress\" or .status == \"queued\")] | length' 2>/dev/null || echo \"0\")" not in wf


def test_plan_runs_payload_is_shape_validated_before_counting() -> None:
	wf = _read_workflow()

	assert "_plan_runs_json_valid()" in wf
	assert "jq -se 'length == 1 and (.[0] | type == \"object\" and (.workflow_runs | type == \"array\"))'" in wf
	assert "PLAN_RUNS_JSON='{\"workflow_runs\":[]}'" in wf
	assert "if ! _plan_runs_json_valid \"$PLAN_RUNS_JSON\"; then" in wf
	assert "if _OTHER_ACTIVE_PLAN_RUNS=$(printf '%s' \"$PLAN_RUNS_JSON\"" in wf


def test_other_active_plan_runs_is_numeric_before_arithmetic_comparison() -> None:
	wf = _read_workflow()

	numeric_guard = 'if ! [[ "$OTHER_ACTIVE_PLAN_RUNS" =~ ^[0-9]+$ ]]; then'
	comparison = 'if [ "$OTHER_ACTIVE_PLAN_RUNS" -gt 0 ]; then'

	assert numeric_guard in wf
	assert "OTHER_ACTIVE_PLAN_RUNS=0" in wf
	assert comparison in wf
	assert wf.index(numeric_guard) < wf.index(comparison)


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
