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
	assert '--fixtures-root "tests/fixtures/selftest"' in wf
	assert '--summary-path "artifacts/validation-selftest-summary.json"' in wf
	assert '--log-dir "artifacts/validation-selftest-logs"' in wf
	assert '--runtime-command "bash scripts/validate_driver.sh"' in wf
	assert '- name: Upload validation self-test artifacts' in wf
	assert 'if: always()' in wf
	assert 'uses: actions/upload-artifact@v6' in wf
	assert 'artifacts/validation-selftest-summary.json' in wf
	assert 'artifacts/validation-selftest-logs/' in wf
	assert '- name: Update validation self-test status artifact' in wf
	assert 'python3 scripts/validation_selftest_status.py' in wf
	assert '--summary-path "artifacts/validation-selftest-summary.json"' in wf
	assert '--status-path "analysis/validation-selftest-status.json"' in wf


def test_workflow_commits_only_status_file_when_changed() -> None:
	wf = _workflow_text()
	assert 'permissions:' in wf
	assert 'contents: write' in wf
	assert '- name: Commit validation self-test status' in wf
	assert "if: always() && github.ref_type == 'branch'" in wf
	assert 'status_file="analysis/validation-selftest-status.json"' in wf
	assert 'git add "${status_file}"' in wf
	assert 'if git diff --cached --quiet; then' in wf
	assert 'git commit -m "chore: update validation self-test status"' in wf
	assert 'git pull --rebase origin "${TARGET_BRANCH}"' in wf
	assert 'git push origin "HEAD:${TARGET_BRANCH}"' in wf


def test_workflow_emits_machine_readable_summary_to_step_summary() -> None:
	wf = _workflow_text()
	assert '- name: Write self-test summary' in wf
	assert 'summary_file="artifacts/validation-selftest-summary.json"' in wf
	assert "summary_data=\"$(jq -r '" in wf
	assert "fixture_lines=\"$(jq -r '" in wf
	assert '(.overall_status // "unknown")' in wf
	assert '(.totals.fixtures // 0)' in wf
	assert '.fixtures[]?' in wf
	assert '.stages.clone.status' in wf
	assert '] | @tsv' in wf
	assert 'GITHUB_STEP_SUMMARY' in wf


def main() -> int:
	test_workflow_has_nightly_schedule_and_manual_dispatch()
	test_workflow_runs_matrix_and_always_uploads_artifacts()
	test_workflow_commits_only_status_file_when_changed()
	test_workflow_emits_machine_readable_summary_to_step_summary()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
