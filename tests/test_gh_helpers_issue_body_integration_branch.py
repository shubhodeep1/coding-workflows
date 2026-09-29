#!/usr/bin/env python3
"""Parity tests for issue_body_integration_branch in scripts/gh_helpers.sh.

close_merged_issues_sweep and issue_pr_status.yml use the shell helper to read
an issue's target branch (issue #4813). It must agree with the Python parser
scripts/orchestrate_lib.py:extract_integration_branch, which routes planning and
implementation to the same branch.

The file also covers issue_body_orchestrator_project_branch, the lineage
parser both callers use for a labelled orchestrator-managed child
(issue #4957).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from orchestrate_lib import extract_integration_branch  # noqa: E402


BODIES = [
	"",
	"No branch line here.\nissue #12",
	"- Integration branch: `claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason`\n",
	"Integration branch: orchestrator/project-192",
	"**Integration branch:** `orchestrator/project-7`\n",
	"  - Integration branch:   `feature/x`  \n",
	"**Target branch:** `orchestrator/project-3965` (integration branch for the project)\n",
	"Target branch: stable\n",
	"Target branch: `stable`\n",
	"- Target branch: stable\n- Integration branch: `orchestrator/project-9`\n",
	"Prose that mentions Integration branch: inline is not a line start? Integration branch: nope\n",
	"Target branch: two words\n",
]


def _shell_parse(body: str) -> str:
	result = subprocess.run(
		[
			"bash",
			"-c",
			'source "$1" >/dev/null 2>&1; issue_body_integration_branch "$2"',
			"_",
			str(REPO_ROOT / "scripts" / "gh_helpers.sh"),
			body,
		],
		capture_output=True,
		text=True,
		timeout=30,
		env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
	)
	assert result.returncode == 0, result.stderr
	return result.stdout.strip()


def test_shell_parser_matches_python_parser() -> None:
	for body in BODIES:
		assert _shell_parse(body) == extract_integration_branch(body), repr(body)


def test_canonical_line_wins_over_alias() -> None:
	body = "- Target branch: stable\n- Integration branch: `orchestrator/project-9`\n"
	assert _shell_parse(body) == "orchestrator/project-9"


def test_missing_line_prints_nothing() -> None:
	assert _shell_parse("Refs #10\n") == ""


def _shell_project_branch(body: str) -> str:
	result = subprocess.run(
		[
			"bash",
			"-c",
			'source "$1" >/dev/null 2>&1; issue_body_orchestrator_project_branch "$2"',
			"_",
			str(REPO_ROOT / "scripts" / "gh_helpers.sh"),
			body,
		],
		capture_output=True,
		text=True,
		timeout=30,
		env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
	)
	assert result.returncode == 0, result.stderr
	return result.stdout.strip()


PROJECT_BRANCH_CASES = [
	# Orchestrator metadata block (orchestrate.yml / orchestrate_poll_process.sh).
	("---\n**Orchestrator metadata** (do not edit)\n- Tracking issue: #192\n- Integration branch: orchestrator/project-192\n- Managed by: AI Orchestrator\n", "orchestrator/project-192"),
	# Bold form written by workflow_failure_heal (compose-issue).
	("- **Target branch:** `stable`\n- **Tracking issue:** #12\n- **Integration branch:** `orchestrator/project-12`\n", "orchestrator/project-12"),
	# CRLF line endings and loose spacing.
	("  -   Tracking issue:   #77  \r\n", "orchestrator/project-77"),
	# A reissue that carries the same metadata twice still names one project.
	("- Tracking issue: #5\n\n- Tracking issue: #5\n", "orchestrator/project-5"),
	# Two different tracking issues are ambiguous: fail closed.
	("- Tracking issue: #5\n- Tracking issue: #6\n", ""),
	# A prose mention is not the metadata line.
	("The Tracking issue: #5 is mentioned in prose.\n", ""),
	("Tracking issue: #5 (see thread)\n", ""),
	("- Tracking issue: 5\n", ""),
	("Refs #10\n", ""),
	("", ""),
]


def test_project_branch_from_tracking_issue_line() -> None:
	for body, expected in PROJECT_BRANCH_CASES:
		assert _shell_project_branch(body) == expected, repr(body)


if __name__ == "__main__":
	test_shell_parser_matches_python_parser()
	test_canonical_line_wins_over_alias()
	test_missing_line_prints_nothing()
	test_project_branch_from_tracking_issue_line()
	print("PASS")
