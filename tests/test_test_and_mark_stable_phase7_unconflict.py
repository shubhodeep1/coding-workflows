#!/usr/bin/env python3
"""Contract checks for Phase 7a of the release gate: the smoke PR is made
mergeable before it is closed, so the close exercises a real
``pull_request.closed`` trigger (run 35672590166 failed ``no_run`` because the
PR was conflicted and GitHub skips ``pull_request`` workflows for conflicted
PRs)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

OPEN_PATH_START = "# PR is still open — exercise the close path end to end."
SNAPSHOT_MARKER = "# Snapshot the most recent cancel-on-close run BEFORE we close the"
CLOSE_MARKER = 'CLOSE_RESPONSE=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}"'
RESULT_MARKER = "PHASE7_UNCONFLICT_RESULT pr=${PR_NUMBER}"


def _read_workflow() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _slice_between(text: str, start_marker: str, end_marker: str) -> str:
	start = text.find(start_marker)
	assert start != -1, f"Missing marker: {start_marker}"
	end = text.find(end_marker, start)
	assert end != -1, f"Missing marker after {start_marker}: {end_marker}"
	return text[start:end]


def _phase7_step_script() -> str:
	doc = yaml.safe_load(_read_workflow())
	steps = doc["jobs"]["e2e-smoke-test"]["steps"]
	for step in steps:
		if step.get("id") == "verify-cancel-on-close":
			return step["run"]
	raise AssertionError("verify-cancel-on-close step not found")


def _unconflict_block(wf: str) -> str:
	return _slice_between(wf, OPEN_PATH_START, SNAPSHOT_MARKER)


def test_unconflict_runs_before_the_pre_close_snapshot_and_the_close() -> None:
	wf = _read_workflow()
	block = _unconflict_block(wf)
	assert "_phase7_refresh_mergeability" in block
	assert RESULT_MARKER in block
	assert wf.index(RESULT_MARKER) < wf.index(SNAPSHOT_MARKER) < wf.index(CLOSE_MARKER)


def test_unconflict_merges_base_into_branch_via_merges_api() -> None:
	block = _unconflict_block(_read_workflow())
	assert 'gh api "repos/${TEST_REPO}/merges"' in block
	assert '-f base="${BRANCH}" -f head="${PHASE7_BASE_REF}"' in block
	# A 409 is the conflict signal; anything else is a transient error.
	assert "grep -Eqi 'HTTP 409|status code 409|conflict'" in block


def test_unconflict_only_rewrites_files_the_base_changed_since_merge_base() -> None:
	block = _unconflict_block(_read_workflow())
	assert '"repos/${TEST_REPO}/compare/${PHASE7_BASE_REF}...${BRANCH}"' in block
	assert '"repos/${TEST_REPO}/pulls/${PR_NUMBER}/files?per_page=100"' in block
	assert 'if [ -z "${PHASE7_MERGE_BASE_SHA}" ]; then' in block
	assert "outcome=kept reason=merge_base_unavailable" in block
	assert 'elif [ -z "${phase7_merge_base_blob}" ]; then' in block
	assert "outcome=kept reason=merge_base_blob_unavailable" in block
	assert 'elif [ -z "${phase7_base_blob}" ]; then' in block
	assert "outcome=kept reason=base_blob_unavailable" in block
	assert '[ "${phase7_base_blob}" != "${phase7_merge_base_blob}" ]' in block
	assert 'gh api -X PUT "repos/${TEST_REPO}/contents/${path}"' in block


def test_unconflict_commits_are_never_labelled_ai_autofix() -> None:
	block = _unconflict_block(_read_workflow())
	messages = [line for line in block.splitlines() if "-f message=" in line or "-f commit_message=" in line]
	assert len(messages) == 2, messages
	for line in messages:
		assert "[E2E Smoke Test]" in line
		assert "[ai-autofix]" not in line


def test_unconflict_never_fails_the_step_and_exports_its_outcome() -> None:
	block = _unconflict_block(_read_workflow())
	assert "exit 1" not in block
	assert 'echo "unconflict=${PHASE7_UNCONFLICT_OUTCOME}" >> "$GITHUB_OUTPUT"' in block
	assert "PHASE7_UNCONFLICT_CHECK pr=${PR_NUMBER}" in block
	assert "PHASE7_UNCONFLICT_FILE path=" in block
	# Non-zero function returns are captured, never left to `set -e`.
	assert block.count("_phase7_merge_base_into_branch || PHASE7_MERGE_RC=$?") == 2
	assert "::warning::Smoke PR #${PR_NUMBER} could not be made mergeable before close" in block


def test_unresolved_mergeability_is_not_reported_as_not_needed() -> None:
	block = _unconflict_block(_read_workflow())
	assert 'elif [ "${PHASE7_MERGEABLE}" != "true" ]; then' in block
	assert 'PHASE7_UNCONFLICT_OUTCOME="still_${PHASE7_MERGEABLE_STATE}"' in block
	assert "mergeability could not be confirmed before close" in block


def test_results_table_reports_the_unconflict_outcome() -> None:
	wf = _read_workflow()
	assert 'CANCEL_UNCONFLICT="${{ steps.verify-cancel-on-close.outputs.unconflict }}"' in wf
	assert "pre-close unconflict=${CANCEL_UNCONFLICT:-n/a}" in wf


def test_phase7_step_script_is_valid_bash() -> None:
	script = _phase7_step_script()
	assert RESULT_MARKER in script
	result = subprocess.run(
		["bash", "-n"],
		input=script,
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stderr


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
