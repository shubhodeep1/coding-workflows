#!/usr/bin/env python3
"""Parity tests for issue_body_integration_branch in scripts/gh_helpers.sh.

close_merged_issues_sweep and issue_pr_status.yml use the shell helper to read
an issue's target branch (issue #4813). It must agree with the Python parser
scripts/orchestrate_lib.py:extract_integration_branch, which routes planning and
implementation to the same branch.
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


if __name__ == "__main__":
	test_shell_parser_matches_python_parser()
	test_canonical_line_wins_over_alias()
	test_missing_line_prints_nothing()
	print("PASS")
