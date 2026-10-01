#!/usr/bin/env python3
"""Comment URLs are not linked issues, and Claude project merges leave issues alone (issue #5776).

Incident (2026-09-30, run 36731103232): conformance-fix PR #5649 merged into
#4867's own project branch, `claude/implement-plan-issue-4867-…`. Its body said
`Refs #4867` and linked twice to comments on #4867
(`…/issues/4867#issuecomment-…`). The body/title fallback of
`issue_pr_status.yml` counted those comment links as linked issues, so #4867 was
labelled `ai:merged` and its AI-memory lineage finalized as `merged` while the
project was still running.

`extract_repo_scoped_issue_refs_from_text` (`scripts/gh_helpers.sh`) now ignores
a repo-scoped issue URL or path whose number is followed by a `#` fragment.
These tests pin that directly, and drive the real `Update linked issue labels
when PR closes` and `Finalize linked issue lineage state` steps through the stub
harness of `tests/test_issue_pr_status_target_branch_gate.py` for the cases the
issue lists. The harness is loaded by path, so this module never edits that
file.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"
GATE_TESTS = REPO_ROOT / "tests" / "test_issue_pr_status_target_branch_gate.py"

_gate_spec = importlib.util.spec_from_file_location("_issue_pr_status_gate_harness", GATE_TESTS)
assert _gate_spec is not None and _gate_spec.loader is not None, GATE_TESTS
gate_harness = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(gate_harness)

REPOSITORY = gate_harness.REPOSITORY
CLAUDE_PROJECT_BRANCH = "claude/implement-plan-x"
CLAUDE_FIX_HEAD = "claude/implement-plan-x-conformance-fix-3"


def _extract(text: str, repository: str = REPOSITORY) -> list[int]:
	script = textwrap.dedent(
		f"""\
		set -euo pipefail
		source "{GH_HELPERS.as_posix()}"
		extract_repo_scoped_issue_refs_from_text "$REPOSITORY_INPUT" "$TEXT_INPUT"
		"""
	)
	env = os.environ.copy()
	env.update({"REPOSITORY_INPUT": repository, "TEXT_INPUT": text})
	result = subprocess.run(
		["bash", "-c", script],
		cwd=str(REPO_ROOT),
		env=env,
		capture_output=True,
		text=True,
		check=True,
	)
	return [int(line) for line in result.stdout.split()]


def _comment_url(issue: int, comment: int) -> str:
	return f"https://github.com/{REPOSITORY}/issues/{issue}#issuecomment-{comment}"


INCIDENT_PR_BODY = (
	"Conformance fix 3b.\n\n"
	f"Answers the finding in {_comment_url(4867, 5908534539)} and the follow-up in\n"
	f"{_comment_url(4867, 5908534540)}.\n\n"
	"Refs #4867\n"
)


# --- The helper ---------------------------------------------------------------

def test_comment_url_alone_yields_no_linked_issue() -> None:
	assert _extract(_comment_url(4867, 5908534539)) == []
	assert _extract(INCIDENT_PR_BODY) == []


def test_any_fragment_after_the_issue_number_is_not_a_link() -> None:
	for text in (
		f"https://github.com/{REPOSITORY}/issues/12#top",
		f"https://github.com/{REPOSITORY}/issues/12#",
		f"{REPOSITORY}/issues/12#issuecomment-1",
		f"see ({REPOSITORY}/issues/12#x).",
	):
		assert _extract(text) == [], text


def test_fragment_after_a_query_or_slash_is_not_a_link() -> None:
	"""A notification link puts a query before the comment fragment; the
	fragment still points inside the issue (round 2 review of PR #5825)."""
	for text in (
		f"https://github.com/{REPOSITORY}/issues/12?notification_referrer_id=NT_1#issuecomment-1",
		f"{REPOSITORY}/issues/12?x=1&y=2#issuecomment-1",
		f"https://github.com/{REPOSITORY}/issues/12/#top",
		f"https://github.com/{REPOSITORY}/issues/12/x#y",
		f"[comment](https://github.com/{REPOSITORY}/issues/12?q=3#issuecomment-4).",
		f"<https://github.com/{REPOSITORY}/issues/12?q#c>",
	):
		assert _extract(text) == [], text


def test_bare_urls_paths_and_closing_keywords_still_link() -> None:
	assert _extract(f"https://github.com/{REPOSITORY}/issues/78") == [78]
	assert _extract(f"{REPOSITORY}/issues/56.") == [56]
	assert _extract(f"(https://github.com/{REPOSITORY}/issues/11)") == [11]
	assert _extract(f"https://github.com/{REPOSITORY}/issues/9/ and https://github.com/{REPOSITORY}/issues/10?x=1") == [9, 10]
	assert _extract("Fixes #34\ncloses #35") == [34, 35]
	# A digit in the query is not the issue number.
	assert _extract(f"https://github.com/{REPOSITORY}/issues/13?page=2") == [13]
	assert _extract(f"[t](https://github.com/{REPOSITORY}/issues/14?a=5).") == [14]
	# A fragment after whitespace belongs to other text, not to the URL.
	assert _extract(f"https://github.com/{REPOSITORY}/issues/15?a=1 #note") == [15]
	assert _extract("Fixes #16#later") == [16]


def test_mixed_text_keeps_only_the_real_links() -> None:
	text = (
		f"Fixes #7. Context: {_comment_url(8, 1)} and {_comment_url(7, 2)}; "
		f"tracker https://github.com/{REPOSITORY}/issues/9"
	)
	assert _extract(text) == [7, 9]
	# The same issue linked both bare and with a fragment still counts once.
	assert _extract(f"{_comment_url(8, 1)} https://github.com/{REPOSITORY}/issues/8") == [8]


# --- The label/close and lineage steps ------------------------------------------

def test_claude_project_merge_with_refs_and_comment_url_leaves_issue_untouched() -> None:
	"""The #5649 shape: no closing reference, only `Refs #N` and comment URLs."""
	gate = gate_harness._run_step(
		issues={4867: {"body": "Close the permission-prompt duplicates.", "labels": ["ai:claude"]}},
		pr_base_ref=CLAUDE_PROJECT_BRANCH,
		pr_body=INCIDENT_PR_BODY,
		closing_refs=[],
		pr_head_ref=CLAUDE_FIX_HEAD,
	)
	assert gate["labels"] == [], gate
	assert gate["closed"] == [], gate
	assert "No linked issues found for PR #4748 (even after body/title fallback)." in gate["stdout"], gate["stdout"]
	assert "Found linked issues via PR body/title fallback" not in gate["stdout"], gate["stdout"]
	assert gate_harness._issue_list(gate["env"].get("LINKED_ISSUE_NUMBERS", "")) == [], gate["env"]
	assert gate_harness._issue_list(gate["env"].get("LINEAGE_FINALIZE_ISSUE_NUMBERS", "")) == [], gate["env"]
	lineage = gate_harness._run_lineage_step(gate["env"])
	assert lineage["finalized"] == [], lineage


def test_claude_project_merge_with_closing_keyword_leaves_issue_untouched() -> None:
	"""A `/implement-plan-claude` phase or fix PR that does carry a closing
	keyword, merged into a `claude/implement-plan-*` branch, is still not the
	issue's completion: no label, no close, no lineage. That holds even when the
	issue names that branch as its integration branch, because the head is not
	an automation head for the issue."""
	for issue_body in (
		"Standalone issue.",
		f"Sub-issue.\n\n- Integration branch: `{CLAUDE_PROJECT_BRANCH}`\n",
	):
		gate = gate_harness._run_step(
			issues={4867: {"body": issue_body, "labels": ["ai:claude"]}},
			pr_base_ref=CLAUDE_PROJECT_BRANCH,
			pr_body=f"Fixes #4867\n\n{_comment_url(4867, 1)}\n",
			closing_refs=[4867],
			pr_head_ref=CLAUDE_FIX_HEAD,
		)
		assert gate["labels"] == [], (issue_body, gate)
		assert gate["closed"] == [], (issue_body, gate)
		assert gate_harness._issue_list(gate["env"]["LINKED_ISSUE_NUMBERS"]) == [4867], (issue_body, gate["env"])
		assert gate_harness._issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [], (issue_body, gate["env"])
		assert "Updating issue #4867 with label" not in gate["stdout"], (issue_body, gate["stdout"])
		lineage = gate_harness._run_lineage_step(gate["env"])
		assert lineage["finalized"] == [], (issue_body, lineage)


def test_managed_child_on_integration_branch_still_labels_and_closes() -> None:
	for closing_refs in ([10], []):
		gate = gate_harness._run_step(
			issues={10: {"body": gate_harness.MANAGED_CHILD_BODY, "labels": ["ai:orchestrator-managed"]}},
			pr_base_ref="orchestrator/project-5",
			pr_body=f"Fixes #10\n\nSee {_comment_url(5, 3)}\n",
			closing_refs=closing_refs,
			pr_head_ref="ai/issue-10",
		)
		assert gate["labels"] == [("10", "ai:merged")], (closing_refs, gate)
		assert gate["closed"] == [10], (closing_refs, gate)
		# The comment link to tracking issue #5 is not a linked issue.
		assert gate_harness._issue_list(gate["env"]["LINKED_ISSUE_NUMBERS"]) == [10], (closing_refs, gate["env"])
		assert gate_harness._issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [10], (closing_refs, gate["env"])
		lineage = gate_harness._run_lineage_step(gate["env"])
		assert lineage["finalized"] == [(10, "merged")], (closing_refs, lineage)


def test_default_branch_merge_with_fixes_still_labels_and_closes() -> None:
	for closing_refs in ([10], []):
		gate = gate_harness._run_step(
			issues={
				10: {"body": "Standalone issue.", "labels": ["ai:claude"]},
				11: {"body": "Related issue.", "labels": ["ai:claude"]},
			},
			pr_base_ref="main",
			pr_body=f"Fixes #10\n\nDiscussed in {_comment_url(11, 4)}\n",
			closing_refs=closing_refs,
			pr_head_ref="claude/implement-plan-issue-10-x",
		)
		assert gate["labels"] == [("10", "ai:merged")], (closing_refs, gate)
		assert gate["closed"] == [10], (closing_refs, gate)
		assert gate_harness._issue_list(gate["env"]["LINKED_ISSUE_NUMBERS"]) == [10], (closing_refs, gate["env"])
		lineage = gate_harness._run_lineage_step(gate["env"])
		assert lineage["finalized"] == [(10, "merged")], (closing_refs, lineage)


if __name__ == "__main__":
	test_comment_url_alone_yields_no_linked_issue()
	test_any_fragment_after_the_issue_number_is_not_a_link()
	test_fragment_after_a_query_or_slash_is_not_a_link()
	test_bare_urls_paths_and_closing_keywords_still_link()
	test_mixed_text_keeps_only_the_real_links()
	test_claude_project_merge_with_refs_and_comment_url_leaves_issue_untouched()
	test_claude_project_merge_with_closing_keyword_leaves_issue_untouched()
	test_managed_child_on_integration_branch_still_labels_and_closes()
	test_default_branch_merge_with_fixes_still_labels_and_closes()
	print("PASS")
