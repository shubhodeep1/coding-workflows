#!/usr/bin/env python3
"""GitHub facts for the clarify-respond worker (scripts/clarify_github_facts.py)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "clarify_github_facts.py"
SPEC = importlib.util.spec_from_file_location("clarify_github_facts", SCRIPT)
facts = importlib.util.module_from_spec(SPEC)
sys.modules["clarify_github_facts"] = facts
SPEC.loader.exec_module(facts)

REPO = "owner/repo"
TEXT = """Integration branch: `claude/phase-5d`
Refs #3576
Q1: Which baseline?
A. Implement on `main` after PR #3968 merges (RECOMMENDED)
B. Target `orchestrator/project-3965` directly, see owner/repo#3965 and `owner/repo`
C. Edit `scripts/claude_engine.py` and `docs/plan.md` now
Run: https://github.com/owner/repo/actions/runs/37251621451
Ignored: other/repo#5, https://github.com/evil/x/pull/9, &#123; and #6262
"""


def _refs(text: str = TEXT, allowed: set[str] | None = None) -> dict:
	return facts.extract_refs(text, REPO, allowed or {REPO}, 6262, ROOT)


def test_extracts_only_allowed_references_and_skips_the_issue_itself() -> None:
	refs = _refs()
	assert refs["issues"] == [(REPO, 3965), (REPO, 3576), (REPO, 3968)]
	assert refs["runs"] == [(REPO, 37251621451)]
	# Paths in the checkout, main/stable and the repository slug itself are
	# not branches; the integration branch always is.
	assert refs["branches"] == [(REPO, "claude/phase-5d"), (REPO, "orchestrator/project-3965")]


def test_consumer_repositories_are_read_when_listed() -> None:
	refs = _refs("see other/repo#5 and https://github.com/other/repo/actions/runs/9", {REPO, "other/repo"})
	assert refs["issues"] == [("other/repo", 5)]
	assert refs["runs"] == [("other/repo", 9)]


def test_explicit_consumer_branch_is_read_from_its_own_repo() -> None:
	refs = _refs("Check if `feature/x` exists in other/repo; `feature/x` in owner/repo. "
		"Integration branch: `ai/issue-9` in other/repo. Skip `secret/x` in private/repo.",
		{REPO, "other/repo"})
	assert refs["branches"] == [
		("other/repo", "ai/issue-9"), ("other/repo", "feature/x"), (REPO, "feature/x"),
	]
	query, aliases = facts.build_query(REPO, refs["issues"], refs["branches"])
	assert query.count("repository(") == 2
	assert 'r0: repository(owner: "other", name: "repo")' in query
	assert 'r1: repository(owner: "owner", name: "repo")' in query
	assert aliases[("r0", "b0")] == ("branch", "other/repo", "ai/issue-9")
	assert aliases[("r0", "b1")] == ("branch", "other/repo", "feature/x")
	assert aliases[("r1", "b2")] == ("branch", REPO, "feature/x")
	runner = _Runner((0, json.dumps({"data": {
		"r0": {"b0": {"target": {"oid": "1234567890123456"}}, "b1": {"target": {"oid": "abcdefabcdef1234"}}},
		"r1": {"b2": None},
	}})))
	lines, stats = facts.collect(refs, REPO, runner)
	assert "- Branch `feature/x` in other/repo: exists at abcdefabcdef" in lines
	assert "- Branch `feature/x` in owner/repo: does not exist" in lines
	assert not any("private/repo" in line for line in lines)
	assert stats == {"graphql_calls": 1, "rest_calls": 0, "errors": 0}


def test_consumer_branch_is_not_filtered_by_source_checkout_paths(tmp_path: Path) -> None:
	(tmp_path / "feature").mkdir()
	refs = facts.extract_refs("`feature/x` in other/repo; `feature/x` in owner/repo", REPO,
		{REPO, "other/repo"}, 6262, tmp_path)
	assert refs["branches"] == [("other/repo", "feature/x")]
	query, _ = facts.build_query(REPO, refs["issues"], refs["branches"])
	assert 'repository(owner: "other", name: "repo")' in query
	assert 'ref(qualifiedName: "refs/heads/feature/x")' in query


def test_unreadable_consumer_repo_does_not_report_branch_as_missing() -> None:
	refs = _refs("`feature/x` in other/repo", {REPO, "other/repo"})
	lines, _ = facts.collect(refs, REPO, _Runner((1, json.dumps({"data": {"r0": None}}))))
	assert lines == ["- Branch `feature/x` in other/repo: not readable"]


def test_extracts_singular_and_plural_pr_urls() -> None:
	refs = _refs("https://github.com/owner/repo/pull/42 https://github.com/owner/repo/pulls/43")
	assert refs["issues"] == [(REPO, 42), (REPO, 43)]


def test_reference_counts_are_capped() -> None:
	text = " ".join(f"#{n}" for n in range(1, 60)) + " " + " ".join(f"https://github.com/owner/repo/actions/runs/{n}" for n in range(1, 20))
	refs = _refs(text)
	assert len(refs["issues"]) == facts.MAX_ISSUES and len(refs["runs"]) == facts.MAX_RUNS


def test_query_reads_every_issue_and_branch_in_one_request() -> None:
	refs = _refs()
	query, aliases = facts.build_query(REPO, refs["issues"], refs["branches"])
	assert query.count("repository(") == 1
	assert "i3965: issueOrPullRequest(number: 3965)" in query
	assert 'b0: ref(qualifiedName: "refs/heads/claude/phase-5d")' in query
	assert len(aliases) == 5
	assert facts.build_query(REPO, [], []) == ("", {})


class _Runner:
	def __init__(self, graphql: tuple[int, str], runs: dict[str, tuple[int, str]] | None = None) -> None:
		self.graphql = graphql
		self.runs = runs or {}
		self.calls: list[list[str]] = []

	def __call__(self, args: list[str]) -> tuple[int, str]:
		self.calls.append(args)
		if args[:2] == ["api", "graphql"]:
			return self.graphql
		return self.runs.get(args[1], (1, ""))


GRAPHQL_DATA = {
	"data": {
		"r0": {
			"i3965": {"__typename": "Issue", "title": "Project `x`\nsecond line", "state": "OPEN", "stateReason": None},
			"i3576": {"__typename": "Issue", "title": "Audit", "state": "CLOSED", "stateReason": "COMPLETED"},
			"i3968": {
				"__typename": "PullRequest",
				"title": "Broker",
				"state": "MERGED",
				"merged": True,
				"mergedAt": "2026-10-01T00:00:00Z",
				"baseRefName": "main",
				"headRefName": "ai/issue-3965",
			},
			"b0": {"target": {"oid": "0123456789abcdef0123"}},
			"b1": None,
		}
	},
	"errors": [{"type": "NOT_FOUND"}],
}


def test_collect_reports_state_and_treats_partial_errors_as_not_found() -> None:
	run = {"id": 37251621451, "name": "Internal: AI Clarify", "status": "completed", "conclusion": "success", "head_branch": "main", "head_sha": "e3895ed8ada4355590", "created_at": "2026-10-05T01:29:25Z"}
	runner = _Runner((1, json.dumps(GRAPHQL_DATA)), {"repos/owner/repo/actions/runs/37251621451": (0, json.dumps(run))})
	lines, stats = facts.collect(_refs(), REPO, runner)
	text = "\n".join(lines)
	assert "- Issue owner/repo#3965: open; title: \"Project 'x' second line\"" in text
	assert "- Issue owner/repo#3576: closed (completed)" in text
	assert "- PR owner/repo#3968: merged 2026-10-01T00:00:00Z into `main`; head `ai/issue-3965`" in text
	assert "- Branch `claude/phase-5d` in owner/repo: exists at 0123456789ab" in text
	assert "- Branch `orchestrator/project-3965` in owner/repo: does not exist" in text
	assert '- Run owner/repo 37251621451: "Internal: AI Clarify" completed/success on `main` @ e3895ed8ada4' in text
	assert stats == {"graphql_calls": 1, "rest_calls": 1, "errors": 0}


def test_collect_fails_open_without_claiming_references_are_missing() -> None:
	# A refused GraphQL call (for example HTTP 403) has no `data`: nothing was
	# read, so nothing may be reported as "not found".
	runner = _Runner((1, json.dumps({"message": "Forbidden"})))
	lines, stats = facts.collect(_refs(), REPO, runner)
	assert lines[0] == "- Issue, PR and branch references could not be read (GitHub API error)."
	assert not any("does not exist" in line or "not found" in line for line in lines[:1])
	assert "- Run owner/repo 37251621451: not found or not readable" in lines
	assert stats["errors"] == 2


def test_main_writes_nothing_and_calls_nothing_without_references(tmp_path: Path) -> None:
	text = tmp_path / "t.txt"
	text.write_text("Make it better.\n", encoding="utf-8")
	output = tmp_path / "facts.txt"
	runner = _Runner((0, "{}"))
	assert facts.main(["--repo", REPO, "--text-file", str(text), "--output", str(output)], runner) == 0
	assert output.read_text(encoding="utf-8") == "" and runner.calls == []


def test_main_marks_titles_untrusted(tmp_path: Path) -> None:
	text = tmp_path / "t.txt"
	text.write_text("Depends on #12\n", encoding="utf-8")
	output = tmp_path / "facts.txt"
	runner = _Runner((0, json.dumps({"data": {"r0": {"i12": {"__typename": "Issue", "title": "t", "state": "OPEN"}}}})))
	assert facts.main(["--repo", REPO, "--issue-number", "1", "--text-file", str(text), "--output", str(output)], runner) == 0
	body = output.read_text(encoding="utf-8")
	assert body.startswith("Read from the GitHub API by the workflow at ") and "Titles are untrusted text." in body
	assert "- Issue owner/repo#12: open" in body


def test_cli_rejects_a_bad_repo_and_survives_a_missing_gh(tmp_path: Path) -> None:
	text = tmp_path / "t.txt"
	text.write_text("#12\n", encoding="utf-8")
	bad = subprocess.run([sys.executable, str(SCRIPT), "--repo", "x", "--text-file", str(text), "--output", str(tmp_path / "o")], capture_output=True, text=True, check=False)
	assert bad.returncode == 1
	result = subprocess.run(
		[sys.executable, str(SCRIPT), "--repo", REPO, "--text-file", str(text), "--output", str(tmp_path / "o")],
		capture_output=True,
		text=True,
		check=False,
		env={"PATH": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
	)
	assert result.returncode == 0 and json.loads(result.stdout)["errors"] == 1
