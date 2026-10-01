#!/usr/bin/env python3
"""Contract tests for reusable phase predicates and their caller wrappers."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent

PHASE_WORKFLOWS = {
	"clarify": (
		".github/workflows/clarify.yml",
		".github/workflows/internal-clarify.yml",
		"workflow-templates/ai-clarify.yml",
	),
	"plan": (
		".github/workflows/plan.yml",
		".github/workflows/internal-plan.yml",
		"workflow-templates/ai-plan.yml",
	),
	"implement": (
		".github/workflows/implement.yml",
		".github/workflows/internal-implement.yml",
		"workflow-templates/ai-implement.yml",
	),
	"respond": (
		".github/workflows/orchestrate_clarify_respond.yml",
		".github/workflows/internal-orchestrate-clarify-respond.yml",
		"workflow-templates/ai-orchestrate-clarify-respond.yml",
	),
}


def _normalized_job_predicate(relative_path: str, job_name: str) -> str:
	workflow_path = REPO_ROOT / relative_path
	workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
	predicate = workflow["jobs"][job_name]["if"]
	assert isinstance(predicate, str), f"{relative_path} jobs.{job_name}.if must be a string"
	return " ".join(predicate.split())


def _canonical_predicate(job_name: str) -> str:
	return _normalized_job_predicate(PHASE_WORKFLOWS[job_name][0], job_name)


def _assert_clauses(predicate: str, clauses: tuple[str, ...]) -> None:
	for clause in clauses:
		assert clause in predicate, f"Predicate missing security-sensitive clause: {clause}"


def test_internal_and_consumer_predicates_match_reusable_workflows() -> None:
	for job_name, workflow_paths in PHASE_WORKFLOWS.items():
		canonical = _normalized_job_predicate(workflow_paths[0], job_name)
		for wrapper_path in workflow_paths[1:]:
			actual = _normalized_job_predicate(wrapper_path, job_name)
			assert actual == canonical, f"{wrapper_path} jobs.{job_name}.if drifted from {workflow_paths[0]}"


def test_clarify_predicate_preserves_opened_and_trusted_reclarify_routes() -> None:
	_assert_clauses(
		_canonical_predicate("clarify"),
		(
			"github.event_name == 'issues'",
			"github.event.action == 'opened'",
			"'ai:orchestrator-tracking'",
			"'ai:security-audit'",
			"'ai:retro'",
			"github.event.issue.user.type == 'User'",
			"contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.issue.author_association)",
			"github.event.issue.user.type == 'Bot'",
			"github.event.issue.user.login == 'github-actions[bot]'",
			"github.event.issue.pull_request == null",
			"github.event.comment.user.type == 'User'",
			"contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association)",
			"startsWith(github.event.comment.body, '/reclarify')",
		),
	)
	opened, reclarify = _canonical_predicate("clarify").split(" || (github.event_name == 'issue_comment'", 1)
	assert "github.event.issue.user.type == 'User'" in opened
	assert "github.event.issue.user.login == 'github-actions[bot]'" in opened
	assert "github.event.comment.user.type == 'User'" in reclarify
	assert "github.event.issue.author_association" not in reclarify


# Issue #5243: the /reclarify command clause of the clarify predicate. A
# comment starting with /reclarify behaves as before; a /reclarify at the
# start of a later line counts only in a comment without an automation
# marker, on an issue that waits on a human answer. Issue #5309: any `<!--`
# HTML comment marks automation, and orchestrator tracking and managed
# issues never take the later-line form. An orchestrator-managed issue is
# recognised by its label or by the `Managed by: AI Orchestrator` body line,
# as scripts/claude_issue_route.py does.
RECLARIFY_COMMAND_CLAUSE = (
	"(startsWith(github.event.comment.body, '/reclarify') || "
	"(contains(github.event.comment.body, fromJson('\"\\n/reclarify\"')) && "
	"!contains(github.event.comment.body, '<!--') && "
	"!contains(toJson(github.event.issue.labels.*.name), '\"ai:orchestrator-tracking\"') && "
	"!contains(toJson(github.event.issue.labels.*.name), '\"ai:orchestrator-managed\"') && "
	"!contains(github.event.issue.body, 'Managed by: AI Orchestrator') && "
	"(contains(toJson(github.event.issue.labels.*.name), '\"ai:claude-blocked\"') || "
	"contains(toJson(github.event.issue.labels.*.name), '\"ai:claude-handoff-failed\"') || "
	"contains(toJson(github.event.issue.labels.*.name), '\"ai:blocked\"'))))"
)

AWAITING_ANSWER_LABELS = ("ai:claude-blocked", "ai:claude-handoff-failed", "ai:blocked")
ORCHESTRATOR_LABELS = ("ai:orchestrator-tracking", "ai:orchestrator-managed")


def _reclarify_clause_matches(body: str, labels: list[str], issue_body: str | None = "") -> bool:
	"""Evaluate RECLARIFY_COMMAND_CLAUSE the way GitHub expressions do.

	startsWith and contains compare case-insensitively; toJson renders the
	label names as a pretty-printed JSON array of quoted strings; a null
	issue body coerces to an empty string.
	"""
	body_lc = body.lower()
	issue_body_lc = (issue_body or "").lower()
	labels_json = json.dumps(labels, indent=2).lower()
	newline_command = json.loads('"\\n/reclarify"')
	if body_lc.startswith("/reclarify"):
		return True
	return (
		newline_command in body_lc
		and "<!--" not in body_lc
		and not any(f'"{label}"' in labels_json for label in ORCHESTRATOR_LABELS)
		and "managed by: ai orchestrator" not in issue_body_lc
		and any(f'"{label}"' in labels_json for label in AWAITING_ANSWER_LABELS)
	)


# Issue #5309: the orchestrator's clarify escalation adds ai:blocked, then
# posts model text; before the fix it carried no marker.
ORCHESTRATOR_ESCALATION_TEXT = (
	"Autonomous resolution not possible for issue #42.\n\n"
	"The clarify-resolve phase determined that one or more questions require data that cannot be derived from the repository.\n\n"
	"ESCALATION: the maintainer must pick the region.\n/reclarify\n\n"
	"- Clarify comment ID: 1\n- Cycle: 3/3"
)


RECLARIFY_COMMENT_CASES = (
	# (description, body, labels, expected)
	("first line", "/reclarify", [], True),
	("first line mixed case", "/Reclarify now", ["ai:claude"], True),
	("first line with marker", "/reclarify\n<!-- ai:note:v1 -->", [], True),
	(
		"#5127 answer: /reclarify on a later line",
		"**Q1: A** — twin sync done.\n\n- Tests: 154 passed.\n\n/reclarify\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_",
		["ai:claude", "ai:claude-blocked"],
		True,
	),
	("trailing line, CRLF", "Answer: A\r\n\r\n/reclarify", ["ai:claude-blocked"], True),
	("trailing line, handoff failed", "Fixed the token.\n/reclarify", ["ai:claude-handoff-failed"], True),
	("trailing line, Codex blocked", "Use the v2 API.\n/reclarify", ["ai:blocked"], True),
	("trailing line, not waiting on an answer", "Some text\n/reclarify", ["ai:claude"], False),
	("trailing line, review-blocked is not ai:blocked", "Some text\n/reclarify", ["ai:review-blocked"], False),
	("inline mention", "Answered above, please /reclarify", ["ai:claude-blocked"], False),
	("backticked mention", "Reply, then comment `/reclarify`.", ["ai:claude-blocked"], False),
	("indented line", "Answer\n  /reclarify", ["ai:claude-blocked"], False),
	(
		"blocked comment with a /reclarify line",
		"<!-- ai:claude-blocked:v1 -->\n🤖 **Blocked**\n\nReply, then comment:\n\n/reclarify",
		["ai:claude-blocked"],
		False,
	),
	(
		"implementation-plan comment",
		"Implementation Plan\n\n1. Do it\n\nTo proceed reply:\n\n/approved\n\nTo restart clarification reply:\n\n/reclarify\n\n<!-- ai:implementation-plan:v1 -->",
		["ai:blocked"],
		False,
	),
	("no command", "Thanks!", ["ai:claude-blocked"], False),
	# Issue #5309 cases.
	("trailing line, orchestrator-managed ai:blocked", "Use v2.\n/reclarify", ["ai:orchestrator-managed", "ai:blocked"], False),
	("trailing line, orchestrator-tracking ai:blocked", "Judge: retry.\n/reclarify", ["ai:orchestrator-tracking", "ai:blocked"], False),
	(
		"escalation model text, unmarked, orchestrator-managed",
		ORCHESTRATOR_ESCALATION_TEXT,
		["ai:orchestrator-managed", "ai:blocked"],
		False,
	),
	(
		"escalation model text with its marker, standalone ai:blocked",
		ORCHESTRATOR_ESCALATION_TEXT + "\n\n<!-- ai:clarify-escalation:v1 -->",
		["ai:blocked"],
		False,
	),
	(
		"heal occurrence marker without the ai: prefix",
		"<!-- workflow-failure-heal:occurrence -->\nAnother occurrence:\n/reclarify",
		["ai:claude-blocked"],
		False,
	),
	("any HTML comment", "Answer A\n/reclarify\n<!-- note -->", ["ai:claude-blocked"], False),
)


# Issue #5309 review round 3: an orchestrator child issue is also recognised
# by its `Managed by: AI Orchestrator` body line when the label is missing.
ORCHESTRATOR_CHILD_BODY = "## Task\n\nFix it.\n\n## Metadata\n- Managed by: AI Orchestrator\n- Parent: #41"

RECLARIFY_ISSUE_BODY_CASES = (
	# (description, comment body, labels, issue body, expected)
	("trailing line, body-marked orchestrator issue", "Use v2.\n/reclarify", ["ai:blocked"], ORCHESTRATOR_CHILD_BODY, False),
	("escalation model text, body-marked issue", ORCHESTRATOR_ESCALATION_TEXT, ["ai:blocked"], ORCHESTRATOR_CHILD_BODY, False),
	("trailing line, body marker in another case", "Use v2.\n/reclarify", ["ai:blocked"], "managed BY: ai orchestrator", False),
	("first line, body-marked orchestrator issue", "/reclarify", ["ai:blocked"], ORCHESTRATOR_CHILD_BODY, True),
	("trailing line, null issue body", "Use v2.\n/reclarify", ["ai:blocked"], None, True),
	("trailing line, unrelated issue body", "Use v2.\n/reclarify", ["ai:blocked"], "Managed by: the platform team", True),
)


def test_clarify_predicate_accepts_reclarify_on_any_line_with_guards() -> None:
	predicate = _canonical_predicate("clarify")
	assert predicate.count(RECLARIFY_COMMAND_CLAUSE) == 1, "clarify /reclarify command clause drifted"
	assert json.loads('"\\n/reclarify"') == "\n/reclarify"
	for description, body, labels, expected in RECLARIFY_COMMENT_CASES:
		assert _reclarify_clause_matches(body, labels) is expected, description
	for description, body, labels, issue_body, expected in RECLARIFY_ISSUE_BODY_CASES:
		assert _reclarify_clause_matches(body, labels, issue_body) is expected, description


def test_plan_predicate_preserves_trusted_human_and_bot_answer_routes() -> None:
	_assert_clauses(
		_canonical_predicate("plan"),
		(
			"github.event_name == 'issue_comment'",
			"github.event.action == 'created'",
			"github.event.issue.pull_request == null",
			"github.event.comment.user.type == 'User'",
			"contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association)",
			"github.event.comment.user.type == 'Bot'",
			"github.event.comment.user.login == 'github-actions[bot]'",
			"startsWith(github.event.comment.body, '/answer')",
			"'[auto-answered-by-clarify]'",
			"'[auto-answered-by-orchestrator]'",
		),
	)


def test_implement_predicate_preserves_trusted_human_and_bot_approval_routes() -> None:
	_assert_clauses(
		_canonical_predicate("implement"),
		(
			"github.event_name == 'issue_comment'",
			"github.event.action == 'created'",
			"github.event.issue.pull_request == null",
			"github.event.comment.user.type == 'User'",
			"contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association)",
			"github.event.comment.user.type == 'Bot'",
			"github.event.comment.user.login == 'github-actions[bot]'",
			"startsWith(github.event.comment.body, '/approved')",
			"'[auto-approved-by-plan]'",
		),
	)


def test_clarify_response_predicate_preserves_actor_and_content_guards() -> None:
	_assert_clauses(
		_canonical_predicate("respond"),
		(
			"github.event_name == 'issue_comment'",
			"github.event.action == 'created'",
			"github.event.issue.pull_request == null",
			"github.event.comment.user.type == 'Bot'",
			"github.event.comment.user.login == 'github-actions[bot]'",
			"github.event.comment.user.type == 'User'",
			"contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association)",
			"contains(github.event.comment.body, 'Clarification required')",
		),
	)


README_WRAPPER_EXAMPLES = {
	"clarify": "ai-clarify",
	"plan": "ai-plan",
	"implement": "ai-implement",
}


def _readme_wrapper_example(template_name: str) -> dict:
	"""Return the parsed ```yaml block that README.md shows for one core wrapper."""
	readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
	heading = f"**`.github/workflows/{template_name}.yml`**"
	heading_index = readme.find(heading)
	assert heading_index >= 0, f"README missing heading for {template_name}.yml"
	fence_start = readme.find("```yaml\n", heading_index)
	assert fence_start >= 0, f"README missing YAML fence after {template_name}.yml heading"
	fence_start += len("```yaml\n")
	fence_end = readme.find("\n```", fence_start)
	assert fence_end >= 0, f"README missing closing fence for {template_name}.yml example"
	raw_example = readme[fence_start:fence_end]
	assert "@<40-character-release-sha> # stable" in raw_example
	return yaml.safe_load(raw_example)


def test_readme_core_wrapper_examples_carry_reusable_predicates() -> None:
	# README's "Create wrapper workflows" section is what a consumer hand-copies
	# when it does not install workflow-templates/ verbatim. An example without
	# the job-level predicate is the SEC-B06 shape (any commenter dispatches a
	# secrets-inheriting run), so the examples must match the canonical
	# reusable-workflow predicate exactly like the shipped templates do.
	for job_name, template_name in README_WRAPPER_EXAMPLES.items():
		example = _readme_wrapper_example(template_name)
		predicate = example["jobs"][job_name].get("if")
		assert isinstance(predicate, str), f"README {template_name}.yml example lacks jobs.{job_name}.if"
		assert " ".join(predicate.split()) == _canonical_predicate(job_name), (
			f"README {template_name}.yml example predicate drifted from the reusable workflow"
		)
		expected_uses = f"shubhodeep1/coding-workflows/.github/workflows/{PHASE_WORKFLOWS[job_name][0].rsplit('/', 1)[1]}@<40-character-release-sha>"
		assert example["jobs"][job_name]["uses"] == expected_uses


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
