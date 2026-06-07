#!/usr/bin/env python3
"""Regression check for the alt-model E2E cleanup PR selector.

The "Cleanup alt-model artifacts" step in test-and-mark-stable.yml closes the
throwaway implement PR for the alt-model canary issue.  It used to discover that
PR via the *first* `cross-referenced` event on the canary issue's timeline.
When the alt-model implement run failed, no `ai/issue-<N>` PR ever existed, so
the first cross-reference was whatever unrelated PR merely mentioned the canary
issue (e.g. a fix PR whose body said `Refs #<N>`).  That PR was then force-closed
as collateral damage (this is exactly what closed PR #3179).  The selector must
instead target only the implement PR whose head branch is `ai/issue-<N>` — the
same selector Phase 3a's wait-implement uses.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"


def _read_workflow() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _cleanup_alt_model_step(wf: str) -> str:
	marker = "- name: Cleanup alt-model artifacts"
	start = wf.index(marker)
	# The step body runs until the next step (`- name:`) or job boundary.
	next_step = wf.find("\n      - name:", start + len(marker))
	end = next_step if next_step != -1 else len(wf)
	return wf[start:end]


def test_cross_referenced_timeline_heuristic_is_gone() -> None:
	wf = _read_workflow()
	# The brittle "first cross-referenced issue number" PR-discovery heuristic
	# must not exist anywhere in the workflow.
	assert 'select(.event == "cross-referenced") | .source.issue.number] | first' not in wf


def test_alt_model_cleanup_targets_ai_issue_head_branch_pr() -> None:
	wf = _read_workflow()
	step = _cleanup_alt_model_step(wf)
	# PR discovery for cleanup must use the ai/issue-<N> head-branch selector.
	assert "pulls?state=open&head=${TEST_REPO%%/*}:ai/issue-${ISSUE_NUMBER}&per_page=1" in step
	# And it must not fall back to the canary issue's cross-reference timeline.
	assert "issues/${ISSUE_NUMBER}/timeline" not in step
	# The discovered PR is still closed via the shared retry helper.
	assert 'close_with_retry "pulls" "${PR_NUMBER}"' in step


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
