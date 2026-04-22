#!/usr/bin/env python3
"""Contract tests for nightly validation self-test workflow."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "nightly-validation-selftest.yml"


def _workflow_text() -> str:
	return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_workflow_has_nightly_schedule_and_manual_dispatch() -> None:
	wf = _workflow_text()
	assert 'name: Nightly Validation Self-Test' in wf
	assert 'schedule:' in wf
	assert 'cron: "15 2 * * *"' in wf
	assert 'workflow_dispatch:' in wf


def test_workflow_runs_matrix_and_always_uploads_artifacts() -> None:
	wf = _workflow_text()
	assert 'python3 scripts/validation_selftest_matrix.py' in wf
	assert '--fixtures-root "examples/validation-fixtures"' in wf
	assert '--summary-path "artifacts/validation-selftest-summary.json"' in wf
	assert '--log-dir "artifacts/validation-selftest-logs"' in wf
	assert '- name: Upload validation self-test artifacts' in wf
	assert 'if: always()' in wf
	assert 'uses: actions/upload-artifact@v4' in wf
	assert 'artifacts/validation-selftest-summary.json' in wf
	assert 'artifacts/validation-selftest-logs/' in wf


def test_workflow_emits_machine_readable_summary_to_step_summary() -> None:
	wf = _workflow_text()
	assert '- name: Write self-test summary' in wf
	assert 'summary_file="artifacts/validation-selftest-summary.json"' in wf
	assert "summary_data=\"$(jq -r '" in wf
	assert "fixture_lines=\"$(jq -r '" in wf
	assert '(.overall_status // "unknown")' in wf
	assert '(.totals.fixtures // 0)' in wf
	assert '.fixtures[]?' in wf
	assert '] | @tsv' in wf
	assert 'GITHUB_STEP_SUMMARY' in wf


def main() -> int:
	test_workflow_has_nightly_schedule_and_manual_dispatch()
	test_workflow_runs_matrix_and_always_uploads_artifacts()
	test_workflow_emits_machine_readable_summary_to_step_summary()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
