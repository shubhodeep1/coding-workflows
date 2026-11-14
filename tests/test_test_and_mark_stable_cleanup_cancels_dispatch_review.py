#!/usr/bin/env python3
"""The E2E cleanup step cancels the Phase 3c workflow_dispatch review run (#9418)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

BLOCK_START = "# Cancel the Phase 3c workflow_dispatch review run (#9418)."
BLOCK_END = "# Delete test branch"


def _read_workflow() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _slice_between(text: str, start_marker: str, end_marker: str) -> str:
	start = text.find(start_marker)
	assert start != -1, f"Missing marker: {start_marker}"
	end = text.find(end_marker, start)
	assert end != -1, f"Missing marker after {start_marker}: {end_marker}"
	return text[start:end]


def _cleanup_step(wf: str) -> str:
	return _slice_between(
		wf,
		"- name: Cleanup test artifacts",
		"# Temporary: reveal which git invocation",
	)


def _cancel_block(wf: str) -> str:
	return _slice_between(_cleanup_step(wf), BLOCK_START, BLOCK_END)


def test_cleanup_step_cancels_dispatch_review_run_before_branch_delete() -> None:
	step = _cleanup_step(_read_workflow())
	assert "if: always()" in step
	for key in ("ISSUE_NUMBER:", "PR_NUMBER:", "TRACKING_NUMBER:"):
		assert key in step
	block = _cancel_block(_read_workflow())
	assert "REVIEW_WORKFLOW_FILE" in block
	assert "event=workflow_dispatch" in block
	assert "branch=${BRANCH}" in block
	assert "repos/${TEST_REPO}/actions/" in block
	assert "/cancel" in block
	assert "43300092840" in block and "43283656910" in block
	cancel_pos = step.find('/actions/runs/${CLEANUP_DISPATCH_RUN_ID}/cancel"')
	delete_pos = step.find('git/refs/heads/${BRANCH}" -X DELETE')
	assert cancel_pos != -1 and delete_pos != -1
	assert cancel_pos < delete_pos
	# BRANCH is defined once, before the cancel block.
	assert step.count('BRANCH="ai/issue-${ISSUE_NUMBER}"') == 1
	assert step.find('BRANCH="ai/issue-${ISSUE_NUMBER}"') < step.find(BLOCK_START)


def test_cancel_block_calls_are_fail_soft() -> None:
	block = _cancel_block(_read_workflow())
	listing = _slice_between(block, "CLEANUP_DISPATCH_REVIEW_RUN_IDS=", "CLEANUP_DISPATCH_MATCHED=0")
	assert "_safe_gh_jq" in listing
	# A failed listing warns instead of folding into an empty listing.
	assert 'if ! CLEANUP_DISPATCH_REVIEW_RUN_IDS="$(_safe_gh_jq' in block
	assert '|| echo "")"' not in listing
	assert "Warning: could not list dispatch review runs on ${BRANCH}" in listing
	post_line = next(line for line in block.splitlines() if "gh api -X POST" in line)
	assert post_line.strip().startswith("if gh api -X POST")
	assert "Warning: could not cancel dispatch review run" in block


def _run_block(
	env_extra: dict[str, str], listing: str, gh_rc: int = 0, list_rc: int = 0
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	block = textwrap.dedent(_cancel_block(_read_workflow()))
	with tempfile.TemporaryDirectory() as tmp:
		calls = Path(tmp) / "calls.log"
		listing_file = Path(tmp) / "listing.txt"
		listing_file.write_text(listing, encoding="utf-8")
		script = textwrap.dedent(
			f"""\
			set -euo pipefail
			_safe_gh_jq() {{ echo "LIST $*" >> "{calls}"; cat "{listing_file}"; return {list_rc}; }}
			gh() {{ echo "GH $*" >> "{calls}"; return {gh_rc}; }}
			BRANCH="ai/issue-${{ISSUE_NUMBER}}"
			"""
		) + block
		env = {
			"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
			"TEST_REPO": "owner/repo",
			"ISSUE_NUMBER": "9410",
			"PR_NUMBER": "9416",
			"REVIEW_WORKFLOW_FILE": "internal-review.yml",
		}
		env.update(env_extra)
		result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)
		lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
	return result, lines


def test_cancel_block_cancels_only_active_dispatch_runs_on_branch() -> None:
	listing = "\n".join(
		[
			"101 queued workflow_dispatch ai/issue-9410",
			"102 in_progress workflow_dispatch ai/issue-9410",
			"103 completed workflow_dispatch ai/issue-9410",
			"104 queued pull_request ai/issue-9410",
			"105 queued workflow_dispatch ai/issue-1",
			"abc queued workflow_dispatch ai/issue-9410",
			"106 weird::status workflow_dispatch ai/issue-9410",
		]
	) + "\n"
	result, calls = _run_block({}, listing)
	assert result.returncode == 0, result.stderr
	list_calls = [c for c in calls if c.startswith("LIST ")]
	assert len(list_calls) == 1
	assert "repos/owner/repo/actions/workflows/internal-review.yml/runs?event=workflow_dispatch&branch=ai/issue-9410&per_page=20" in list_calls[0]
	posts = [c for c in calls if c.startswith("GH ")]
	assert posts == [
		"GH api -X POST repos/owner/repo/actions/runs/101/cancel",
		"GH api -X POST repos/owner/repo/actions/runs/102/cancel",
	]
	assert "Cancelled dispatch review run 101 (status=queued)" in result.stdout
	assert "Cancelled dispatch review run 102 (status=in_progress)" in result.stdout
	assert "weird" not in result.stdout


def test_cancel_block_failing_cancel_warns_and_exits_zero() -> None:
	result, calls = _run_block({}, "101 queued workflow_dispatch ai/issue-9410\n", gh_rc=1)
	assert result.returncode == 0, result.stderr
	assert "Warning: could not cancel dispatch review run 101" in result.stdout
	assert any("/actions/runs/101/cancel" in c for c in calls)


def test_cancel_block_empty_listing_reports_none() -> None:
	result, calls = _run_block({}, "")
	assert result.returncode == 0, result.stderr
	assert "No active dispatch review runs on ai/issue-9410" in result.stdout
	assert not [c for c in calls if c.startswith("GH ")]


def test_cancel_block_failed_listing_warns_and_does_not_report_none() -> None:
	result, calls = _run_block({}, "", list_rc=1)
	assert result.returncode == 0, result.stderr
	assert "Warning: could not list dispatch review runs on ai/issue-9410" in result.stdout
	assert "No active dispatch review runs" not in result.stdout
	assert len([c for c in calls if c.startswith("LIST ")]) == 1
	assert not [c for c in calls if c.startswith("GH ")]


def test_cancel_block_skips_on_invalid_inputs() -> None:
	for extra in (
		{"ISSUE_NUMBER": "9410; rm"},
		{"PR_NUMBER": ""},
		{"REVIEW_WORKFLOW_FILE": "../x.yml"},
	):
		result, calls = _run_block(extra, "101 queued workflow_dispatch ai/issue-9410\n")
		assert result.returncode == 0, result.stderr
		assert calls == [], (extra, calls)
		assert "Skipped dispatch review-run cancel (missing or invalid inputs)" in result.stdout


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
	for test in tests:
		test()
	print(f"{len(tests)} tests passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
