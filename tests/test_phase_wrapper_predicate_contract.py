#!/usr/bin/env python3
"""Contract tests for reusable phase predicates and their caller wrappers."""

from __future__ import annotations

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
			"github.event.issue.pull_request == null",
			"github.event.comment.user.type == 'User'",
			"contains(fromJson('[\"OWNER\",\"MEMBER\",\"COLLABORATOR\"]'), github.event.comment.author_association)",
			"startsWith(github.event.comment.body, '/reclarify')",
		),
	)


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
