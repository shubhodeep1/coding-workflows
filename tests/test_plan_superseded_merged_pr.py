#!/usr/bin/env python3
"""Classifier tests for scripts/plan_superseded_merged_pr.py (issue #7216).

Fake issue fetchers and fake `gh` reads only; no network. The #7185 regression
is a re-issue (no labels, pipeline author) of check-failure triage issue
#6977 for PR #6976, which merged with CI green on its final head.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import files_touched_scope_guard as ftsg  # noqa: E402
import plan_superseded_merged_pr as psm  # noqa: E402

REPO = "acme/widgets"
PIPELINE = "pipeline-bot"
HEAD_SHA = "a" * 40
HEAD_BRANCH = "ai/issue-6969"


def triage_body(pr: int = 6976, *, repo: str = REPO, marker_pr: int | None = None, check_run_id: str | None = None,
	check: str = "CI") -> str:
	lines = [
		"<!-- check-failure-triage:fp=" + "0" * 64 + " -->",
		"<!-- check-failure-triage:gen=1 -->",
		"<!-- check-failure-triage:root=0 -->",
		f"<!-- check-failure-triage:pr={marker_pr or pr} -->",
		"",
		"## Automated CI failure triage (generation 1 of max 3)",
		"",
		f"- **Repository:** `{repo}`",
		f"- **Pull request:** https://github.com/{repo}/pull/{pr} (`{HEAD_BRANCH}`)",
		f"- **Failing check:** `{check}` (conclusion: `failure`)",
	]
	if check_run_id:
		lines.append(f"- **Check run id:** `{check_run_id}`")
		lines.append(f"- **Check details:** https://github.com/{repo}/runs/{check_run_id}")
	else:
		lines.append(f"- **Check details:** https://github.com/{repo}/actions/runs/123/job/9")
	lines += [f"- **Head SHA:** `{'b' * 40}`", "", "---", "", "Diagnosis prose. Re-issued from #1", "", "---"]
	return "\n".join(lines)


def issue(number: int, body: str, *, author: str = PIPELINE, association: str = "NONE", labels=()) -> dict:
	return {"number": number, "body": body, "user": {"login": author}, "author_association": association,
		"labels": [{"name": name} for name in labels], "title": "Re-issue of #6977: CI on PR #6976"}


TRIAGE_6977 = issue(6977, triage_body(), author="owner-human", association="OWNER", labels=("ai:check-triage",))
REISSUE_7185 = issue(7185, "Re-issued by the unblock judge from #6977.\n\nSpecification: fix CI on `ai/issue-6969`.\n\nWhy: it failed.")


class FakeGh:
	def __init__(self, *, pr=None, workflow_run=None, check_runs=None, run_meta=None):
		self.calls: list[list[str]] = []
		self.pr = pr if pr is not None else merged_pr()
		self.workflow_run = workflow_run if workflow_run is not None else {"status": "completed", "conclusion": "success"}
		self.check_runs = check_runs
		self.run_meta = run_meta if run_meta is not None else {"workflow_id": 77, "repository": {"full_name": REPO}}

	def __call__(self, args):
		self.calls.append(list(args))
		path = next(arg for arg in args if arg.startswith("repos/"))
		if "/pulls/" in path:
			return self.pr
		if "/check-runs" in path:
			return self.check_runs
		if "/actions/workflows/" in path:
			return {"workflow_runs": [self.workflow_run] if self.workflow_run else []}
		if "/actions/runs/" in path:
			return self.run_meta
		raise AssertionError(path)


def merged_pr(*, merged=True, state="closed", head_repo=REPO) -> dict:
	return {"merged_at": "2026-10-10T00:00:00Z" if merged else None, "state": state,
		"head": {"sha": HEAD_SHA, "ref": HEAD_BRANCH, "repo": {"full_name": head_repo} if head_repo else None},
		"base": {"ref": "main"}}


def fetcher(issues: dict):
	reads: list[int] = []

	def fetch(number):
		reads.append(number)
		return issues.get(number)

	fetch.reads = reads
	return fetch


def test_7185_regression_reissue_of_merged_green_pr_is_superseded():
	gh = FakeGh()
	result = psm.classify(REISSUE_7185, REPO, PIPELINE, fetcher({6977: TRIAGE_6977}), gh)
	assert result["outcome"] == "superseded", result
	assert result["source_issue"] == 6977
	assert result["pr"] == 6976
	assert result["base_ref"] == "main"
	assert result["final_head_sha"] == HEAD_SHA
	assert HEAD_BRANCH not in result["comment_body"]
	assert f"<!-- ai:plan-superseded:v1 pr=6976 head={HEAD_SHA} -->" in result["comment_body"]
	assert "Reopening the issue does not undo this" in result["comment_body"]
	assert "first set the repository variable `PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED` to `false`" in result["comment_body"]
	# Workflow-run triage: resolve the workflow from the details run, then the head's newest run.
	assert any("actions/runs/123" in " ".join(call) for call in gh.calls)
	assert any(f"actions/workflows/77/runs?head_sha={HEAD_SHA}" in " ".join(call) for call in gh.calls)


def test_7216_followup_shape_is_not_a_candidate_and_makes_no_calls():
	body = "Accepted with this follow-up by the unblock judge (#7185).\n\nFollow-up: make planning detect merged PRs."
	gh = FakeGh()
	fetch = fetcher({7185: REISSUE_7185, 6977: TRIAGE_6977})
	result = psm.classify(issue(7216, body), REPO, PIPELINE, fetch, gh)
	assert (result["outcome"], result["reason"]) == ("plan", "not_candidate")
	assert gh.calls == [] and fetch.reads == []


def test_direct_triage_issue_is_superseded():
	result = psm.classify(TRIAGE_6977, REPO, PIPELINE, fetcher({}), FakeGh())
	assert result["outcome"] == "superseded"
	assert result["source_issue"] == 6977


@pytest.mark.parametrize("pr, reason", [
	(merged_pr(merged=False, state="open"), "pr_open"),
	(merged_pr(merged=False, state="closed"), "pr_closed_unmerged"),
	(merged_pr(head_repo="evil/fork"), "fork_head"),
	(merged_pr(head_repo=None), "fork_head"),
])
def test_pr_states_that_plan_as_usual(pr, reason):
	result = psm.classify(REISSUE_7185, REPO, PIPELINE, fetcher({6977: TRIAGE_6977}), FakeGh(pr=pr))
	assert (result["outcome"], result["reason"]) == ("plan", reason)


def test_pr_read_failure_plans_as_usual():
	gh = FakeGh()
	gh.pr = None
	result = psm.classify(REISSUE_7185, REPO, PIPELINE, fetcher({6977: TRIAGE_6977}), gh)
	assert (result["outcome"], result["reason"]) == ("plan", "pr_read_failed")


@pytest.mark.parametrize("run, expected", [
	({"status": "completed", "conclusion": "failure"}, "failed"),
	({"status": "in_progress", "conclusion": None}, "pending"),
])
def test_merged_but_check_not_passed(run, expected):
	result = psm.classify(REISSUE_7185, REPO, PIPELINE, fetcher({6977: TRIAGE_6977}), FakeGh(workflow_run=run))
	assert result["outcome"] == "merged_check_not_passed"
	assert result["check_result"] == expected
	assert result["head_ref"] == HEAD_BRANCH
	assert "plan against main" in result["facts"]
	assert HEAD_BRANCH not in result["facts"]


@pytest.mark.parametrize("gh_kwargs", [
	{"workflow_run": {}},
	{"run_meta": {"workflow_id": 77, "repository": {"full_name": "other/repo"}}},
	{"run_meta": {"workflow_id": "77", "repository": {"full_name": REPO}}},
])
def test_unknown_check_result_plans_as_usual(gh_kwargs):
	result = psm.classify(REISSUE_7185, REPO, PIPELINE, fetcher({6977: TRIAGE_6977}), FakeGh(**gh_kwargs))
	assert (result["outcome"], result["reason"]) == ("plan", "check_unknown")


@pytest.mark.parametrize("payload, expected", [
	({"total_count": 2, "check_runs": [{"status": "completed", "conclusion": "success"}] * 2}, "success"),
	({"total_count": 2, "check_runs": [{"status": "completed", "conclusion": "success"},
		{"status": "completed", "conclusion": "failure"}]}, "failed"),
	({"total_count": 3, "check_runs": [{"status": "completed", "conclusion": "success"}]}, "unknown"),
	({"total_count": 0, "check_runs": []}, "unknown"),
	({"total_count": 1, "check_runs": [{"status": "queued"}]}, "pending"),
	(None, "unknown"),
])
def test_check_run_triage_predicate(payload, expected):
	source = triage_body(check_run_id="555", check="lint")
	triage = issue(6977, source, author="owner-human", association="OWNER", labels=("ai:check-triage",))
	gh = FakeGh(check_runs=payload)
	result = psm.classify(triage, REPO, PIPELINE, fetcher({}), gh)
	assert result["check_result"] == expected
	call = gh.calls[-1]
	assert call[:3] == ["-X", "GET", f"repos/{REPO}/commits/{HEAD_SHA}/check-runs"]
	assert "check_name=lint" in call and "filter=latest" in call


def test_lineage_depth_exceeded_plans_as_usual():
	chain = {}
	for number in range(100, 100 + ftsg.ORIGIN_MAX_DEPTH + 1):
		chain[number] = issue(number, f"**⚠️ Re-issued from #{number + 1}** — stalled.")
	start = issue(99, "Re-issued by the unblock judge from #100.")
	result = psm.classify(start, REPO, PIPELINE, fetcher(chain), FakeGh())
	assert (result["outcome"], result["reason"]) == ("plan", "origin_unverified")


def test_issue_read_cap_plans_as_usual():
	refs = "\n".join(f"Re-issued from #{n}" for n in range(200, 200 + ftsg.ORIGIN_MAX_ISSUES + 1))
	issues = {n: TRIAGE_6977 for n in range(200, 210)}
	result = psm.classify(issue(99, refs), REPO, PIPELINE, fetcher(issues), FakeGh())
	assert (result["outcome"], result["reason"]) == ("plan", "origin_unverified")


def test_untrusted_source_author_plans_as_usual():
	outsider = dict(TRIAGE_6977, user={"login": "outsider"}, author_association="CONTRIBUTOR")
	result = psm.classify(REISSUE_7185, REPO, PIPELINE, fetcher({6977: outsider}), FakeGh())
	assert (result["outcome"], result["reason"]) == ("plan", "untrusted_origin")


def test_untrusted_direct_triage_issue_is_not_a_source():
	outsider = dict(TRIAGE_6977, user={"login": "outsider"}, author_association="NONE")
	gh = FakeGh()
	result = psm.classify(outsider, REPO, PIPELINE, fetcher({}), gh)
	assert result["outcome"] == "plan" and gh.calls == []


def test_human_authored_reissue_line_is_not_walked():
	human = issue(7300, "Re-issued from #6977", author="owner-human", association="OWNER")
	fetch = fetcher({6977: TRIAGE_6977})
	result = psm.classify(human, REPO, PIPELINE, fetch, FakeGh())
	assert (result["outcome"], result["reason"]) == ("plan", "no_source")
	assert fetch.reads == []


def test_failed_issue_read_plans_as_usual():
	result = psm.classify(REISSUE_7185, REPO, PIPELINE, fetcher({}), FakeGh())
	assert (result["outcome"], result["reason"]) == ("plan", "origin_unverified")


@pytest.mark.parametrize("body", [
	triage_body(marker_pr=1234),
	triage_body(repo="other/repo"),
	triage_body().replace("- **Pull request:**", "- **PR:**"),
])
def test_malformed_triage_header_is_not_a_source(body):
	triage = issue(6977, body, author="owner-human", association="OWNER", labels=("ai:check-triage",))
	result = psm.classify(REISSUE_7185, REPO, PIPELINE, fetcher({6977: triage}), FakeGh())
	assert result["outcome"] == "plan"
	assert result["reason"] == "mixed_lineage"


def test_header_after_rule_is_ignored():
	body = triage_body().replace("- **Pull request:**", "- **PR:**") + "\n- **Pull request:** https://github.com/acme/widgets/pull/6976 (`x`)"
	triage = issue(6977, body, author="owner-human", association="OWNER", labels=("ai:check-triage",))
	assert psm.parse_triage_source(triage, REPO, PIPELINE) is None


def test_two_different_triage_sources_are_ambiguous():
	other = issue(6990, triage_body(pr=6991), author="owner-human", association="OWNER", labels=("ai:check-triage",))
	start = issue(7400, "Re-issued by the unblock judge from #6977.\n- Replaces: #6990")
	result = psm.classify(start, REPO, PIPELINE, fetcher({6977: TRIAGE_6977, 6990: other}), FakeGh())
	assert (result["outcome"], result["reason"]) == ("plan", "ambiguous_source")


def test_branch_ending_at_non_triage_issue_is_mixed():
	human = issue(6000, "Please fix the widget.", author="owner-human", association="OWNER")
	start = issue(7401, "Re-issued by the unblock judge from #6000.\nRe-issued from #6977")
	result = psm.classify(start, REPO, PIPELINE, fetcher({6000: human, 6977: TRIAGE_6977}), FakeGh())
	assert (result["outcome"], result["reason"]) == ("plan", "mixed_lineage")


def test_reference_inside_prose_is_not_followed():
	start = issue(7402, "Some text. Re-issued from #6977 in passing.")
	gh = FakeGh()
	result = psm.classify(start, REPO, PIPELINE, fetcher({6977: TRIAGE_6977}), gh)
	assert result["outcome"] == "plan"
	assert gh.calls == []


def test_security_issue_is_not_walked():
	start = issue(7403, REISSUE_7185["body"], labels=("ai:security",))
	result = psm.classify(start, REPO, PIPELINE, fetcher({6977: TRIAGE_6977}), FakeGh())
	assert (result["outcome"], result["reason"]) == ("plan", "no_source")


def test_stall_reissue_chain_reaches_triage_source():
	stall = issue(7000, REISSUE_7185["body"] + "\n\n---\n\n**⚠️ Re-issued from #7185** — stalled.")
	result = psm.classify(stall, REPO, PIPELINE, fetcher({7185: REISSUE_7185, 6977: TRIAGE_6977}), FakeGh())
	assert result["outcome"] == "superseded"
	assert result["issues_read"] == [6977, 7185] or set(result["issues_read"]) == {6977, 7185}


def test_missing_pipeline_login_plans_as_usual():
	result = psm.classify(REISSUE_7185, REPO, "", fetcher({6977: TRIAGE_6977}), FakeGh())
	assert (result["outcome"], result["reason"]) == ("plan", "identity_unavailable")


def test_reissue_patterns_track_the_scope_guard_definitions():
	sources = {pattern.pattern for pattern in ftsg.ORIGIN_REFERENCE_PATTERNS}
	assert len(psm.REISSUE_REFERENCE_PATTERNS) == 3
	for pattern in psm.REISSUE_REFERENCE_PATTERNS:
		assert pattern.pattern in sources
	texts = " ".join(pattern.pattern for pattern in psm.REISSUE_REFERENCE_PATTERNS)
	assert "Accepted with this follow-up" not in texts and "Parent issue" not in texts


def test_comment_text_is_sanitised():
	body = triage_body(check="CI @team <b>")
	triage = issue(6977, body, author="owner-human", association="OWNER", labels=("ai:check-triage",))
	result = psm.classify(triage, REPO, PIPELINE, fetcher({}), FakeGh())
	assert "@team" not in result["comment_body"] and "<b>" not in result["comment_body"]


def test_cli_writes_json_and_rejects_bad_repo(tmp_path):
	meta = tmp_path / "meta.json"
	meta.write_text(json.dumps(issue(7216, "Accepted with this follow-up by the unblock judge (#7185).")))
	out = tmp_path / "out.json"
	script = REPO_ROOT / "scripts" / "plan_superseded_merged_pr.py"
	env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
	proc = subprocess.run([sys.executable, "-I", "-B", str(script), "classify", "--issue-meta-file", str(meta),
		"--repo", REPO, "--pipeline-login", PIPELINE, "--out", str(out)], capture_output=True, text=True, env=env)
	assert proc.returncode == 0, proc.stderr
	assert json.loads(out.read_text())["reason"] == "not_candidate"
	assert "PLAN_SUPERSEDED_MERGED_PR classify outcome=plan reason=not_candidate" in proc.stderr
	bad = subprocess.run([sys.executable, "-I", "-B", str(script), "classify", "--issue-meta-file", str(meta),
		"--repo", "not a repo", "--out", str(out)], capture_output=True, text=True, env=env)
	assert bad.returncode == 2
