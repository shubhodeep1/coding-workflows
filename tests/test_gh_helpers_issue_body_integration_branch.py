#!/usr/bin/env python3
"""Parity tests for issue_body_integration_branch in scripts/gh_helpers.sh.

close_merged_issues_sweep and issue_pr_status.yml use the shell helper to read
an issue's target branch (issue #4813). It must agree with the Python parser
scripts/orchestrate_lib.py:extract_integration_branch, which routes planning and
implementation to the same branch.

Issue #4956: the helper parses one line at a time in linear time instead of
running those regexes over the whole body. The line-bounded tests below compare
it with the original regexes applied to each line on its own, and bound its
run time on bodies that made the regexes run for minutes.

The file also covers issue_body_orchestrator_project_branch, the lineage
parser both callers use for a labelled orchestrator-managed child
(issue #4957).
"""

from __future__ import annotations

import random
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from orchestrate_lib import (  # noqa: E402
	INTEGRATION_BRANCH_LINE_RE,
	TARGET_BRANCH_LINE_RE,
	extract_integration_branch,
)


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
	# A label line whose value is empty or whitespace-only still matches and
	# ends the search as "", so a later valid line is not used (PR #5005
	# review round 1).
	"Integration branch: `\nTarget branch: `real/branch`\n",
	"Integration branch:   \nTarget branch: `real/branch`\n",
	"Integration branch: ` `\nTarget branch: real\n",
	"Target branch: `  `\nTarget branch: real\n",
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


def _shell_parse_many(bodies: list[str], timeout: float = 120) -> list[str]:
	"""Run the helper once per body inside a single bash process."""
	result = subprocess.run(
		[
			"bash",
			"-c",
			'source "$1" >/dev/null 2>&1; shift; for b in "$@"; do issue_body_integration_branch "$b"; printf "\\0"; done',
			"_",
			str(REPO_ROOT / "scripts" / "gh_helpers.sh"),
			*bodies,
		],
		capture_output=True,
		timeout=timeout,
		env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
	)
	assert result.returncode == 0, result.stderr
	# Decode by hand: text=True would turn a "\r" in a value into "\n".
	outputs = result.stdout.decode("utf-8").split("\0")
	assert outputs[-1] == "", repr(outputs[-1])
	outputs = outputs[:-1]
	assert len(outputs) == len(bodies)
	return [out.removesuffix("\n") for out in outputs]


def _line_bounded_reference(body: str) -> str:
	"""The original regexes, applied to each line on its own.

	Lines split on "\\n" only, the boundary re.MULTILINE uses for ^ and $; a
	"\\r" stays inside its line as whitespace.
	"""
	lines = body.split("\n")
	for line in lines:
		match = INTEGRATION_BRANCH_LINE_RE.match(line)
		if match:
			return match.group(1).strip()
	for line in lines:
		alias_match = TARGET_BRANCH_LINE_RE.match(line)
		if alias_match:
			return (alias_match.group(1) or alias_match.group(2) or "").strip()
	return ""


LINE_BOUNDED_BODIES = [
	"Integration branch: `feature/x`\r\n",
	"Integration branch:\tfeature/tab\t\n",
	"Integration branch: `feature/nbsp` \n",
	"Integration branch:  \n",
	"Integration branch: ` `\n",
	"Integration branch: `\n",
	"Integration branch:`\n",
	"Integration branch: ``\n",
	"Integration branch: `a`b`\n",
	"Integration branch: a`\n",
	"Integration branch: `a\n",
	"Integration branch: two words\n",
	"Integration branch:\n",
	"Integration branch:\nfeature/next-line\n",
	# The whole-body regex takes "Target branch: real" as the value here
	# (its \s* runs past the newline); line by line it is "" (PR #5005
	# review round 2).
	"Integration branch: `\nTarget branch: real\n",
	"Integration branch:   \nTarget branch: real\n",
	"-Integration branch: dash/no-space\n",
	"- - Integration branch: double/dash\n",
	"*Integration branch:* not/bold\n",
	"**Integration branch:** `bold/x`\n**Target branch:** `bold/y`\n",
	"Target branch: `a` trailing prose\n",
	"Target branch: `a`b\n",
	"Target branch: `a`\tprose\n",
	"Target branch: ``\n",
	"Target branch: ` `\n",
	"Target branch: a`b\n",
	"Target branch: `unclosed\n",
	"Target branch:\n",
	"Target branch: `a`\nIntegration branch: `b`\n",
	"text\n\n\n  - Integration branch: `late/line`\n",
]


def test_shell_parser_matches_line_bounded_grammar() -> None:
	rng = random.Random(4956)
	atoms = [
		"Integration branch:", "**Integration branch:**", "Target branch:", "**Target branch:**",
		"-", "- ", "`", "``", " ", "\t", "\r", "\x0b", "\x0c", "\x1c", "\x85", " ", " ",
		"\n", "a", "b/c", "x-1", "(note)", "*", ":", "Integration", " branch:",
	]
	generated = [
		"".join(rng.choice(atoms) for _ in range(rng.randint(1, 12)))
		for _ in range(300)
	]
	bodies = LINE_BOUNDED_BODIES + generated
	for body, shell_value in zip(bodies, _shell_parse_many(bodies)):
		assert shell_value == _line_bounded_reference(body), repr(body)


def test_pathological_bodies_finish_quickly() -> None:
	# Before issue #4956 the blank-line bodies took minutes (a failed search
	# is quadratic in blank lines) and each space-run line took minutes too
	# (cubic within a line).
	cases = [
		("x\n" + "\n" * 60000 + "y", ""),
		("x\n" + "\n" * 60000 + "Target branch: `after/blank-lines`", "after/blank-lines"),
		("Integration branch: a" + " " * 5000 + "`b\nIntegration branch: `real/branch`", "real/branch"),
		("Target branch: `a" + " " * 5000 + "b` prose", "a" + " " * 5000 + "b"),
		("Target branch: a" + " " * 5000 + "b\n" + " \n" * 20000, ""),
		(("Integration branch: a" + " " * 30 + "`b\n") * 1500, ""),
	]
	for body, expected in cases:
		started = time.monotonic()
		(value,) = _shell_parse_many([body], timeout=30)
		elapsed = time.monotonic() - started
		assert value == expected, repr(body[:80])
		assert elapsed < 5, f"{elapsed:.1f}s for {body[:40]!r}"


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
	test_shell_parser_matches_line_bounded_grammar()
	test_pathological_bodies_finish_quickly()
	test_project_branch_from_tracking_issue_line()
	print("PASS")
