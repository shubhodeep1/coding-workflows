#!/usr/bin/env python3
"""Tests for pr_head_ref_is_issue_automation_branch in scripts/gh_helpers.sh.

Issue #5226: off the default branch, close_merged_issues_sweep and
issue_pr_status.yml count a merged PR as an issue's own only when its head is
a branch the automation creates for that issue (and the head lives in this
repository, which the callers check). A closing keyword in the PR body and the
issue's `Integration branch:` line are author-controlled text and no longer
suffice on their own. This file pins which head names the helper accepts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"

ACCEPTED = [
	("10", "ai/issue-10"),
	("10", "ai/issue-10-fix"),
	("10", "ai/issue-10/retry"),
	("10", "fix/10-followup-1790000000"),
	("4813", "ai/issue-4813"),
]

REJECTED = [
	("10", ""),
	("10", "ai/issue-100"),
	("10", "ai/issue-1"),
	("1", "ai/issue-10"),
	("10", "ai/issue-10x"),
	("10", "xai/issue-10"),
	("10", "feature/unrelated"),
	("10", "claude/implement-plan-issue-10-own-project"),
	("10", "fix/10-followup-"),
	("10", "fix/10-followup-abc"),
	("10", "fix/100-followup-1790000000"),
	("10", "fix/10-followup-1790000000-extra"),
	("", "ai/issue-"),
	("abc", "ai/issue-abc"),
	("1.0", "ai/issue-1.0"),
]


def _check(issue: str, head: str) -> bool:
	result = subprocess.run(
		["bash", "-c", 'source "$1" && pr_head_ref_is_issue_automation_branch "$2" "$3"', "_", str(GH_HELPERS), issue, head],
		capture_output=True,
		text=True,
		timeout=30,
	)
	assert result.returncode in (0, 1), (issue, head, result.returncode, result.stderr)
	assert result.stdout == "", (issue, head, result.stdout)
	return result.returncode == 0


def test_accepts_automation_heads() -> None:
	for issue, head in ACCEPTED:
		assert _check(issue, head), f"expected accept: issue={issue!r} head={head!r}"


def test_rejects_other_heads() -> None:
	for issue, head in REJECTED:
		assert not _check(issue, head), f"expected reject: issue={issue!r} head={head!r}"


def test_branch_issue_number_parse_agrees_for_ai_heads() -> None:
	"""issue_pr_status.yml maps a head to its issue with
	`sed -nE 's#^ai/issue-([0-9]+)([-/].*)?$#\\1#p'`. Every `ai/issue-…` head
	the helper accepts must map to the same issue there."""
	for issue, head in ACCEPTED:
		if not head.startswith("ai/"):
			continue
		mapped = subprocess.run(
			["sed", "-nE", r"s#^ai/issue-([0-9]+)([-/].*)?$#\1#p"],
			input=head + "\n",
			capture_output=True,
			text=True,
			timeout=30,
		).stdout.strip()
		assert mapped == issue, (issue, head, mapped)


if __name__ == "__main__":
	test_accepts_automation_heads()
	test_rejects_other_heads()
	test_branch_issue_number_parse_agrees_for_ai_heads()
	print("PASS")
