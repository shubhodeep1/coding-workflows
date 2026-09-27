"""Contract for scripts/claude_pr_sweep.py — the CLAUDE.md §26.H catch-all
that queues a fresh Claude fixer for claude/* PRs nobody handled — its
queue items (scripts/claude_issue_route.py), and its wiring in
.github/workflows/review_autofix_sweep.yml."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("claude_pr_sweep", ROOT / "scripts" / "claude_pr_sweep.py")
sweeper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweeper)
route = sweeper.claude_issue_route

NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.timezone.utc)
HEAD = "b" * 40
SWEEP_WF = ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"


def _pr(number, ref="claude/x", repo="o/r", draft=False, title="t", body=""):
	return {"number": number, "draft": draft, "title": title, "body": body,
		"head": {"ref": ref, "repo": {"full_name": repo}}}


def test_load_repos_puts_this_repo_first_and_dedupes(tmp_path):
	registry = tmp_path / "r.json"
	registry.write_text(json.dumps(["o/a", "O/R", "bad slug", 5, "o/a"]))
	assert sweeper.load_repos(registry, "o/r") == ["o/r", "o/a"]
	assert sweeper.load_repos(tmp_path / "missing.json", "o/r") == ["o/r"]


def test_candidates_are_open_same_repo_claude_prs(monkeypatch):
	prs = [_pr(1), _pr(2, ref="ai/issue-2"), _pr(3, draft=True), _pr(4, repo="fork/r"), _pr(5, title="x [skip ai]"), _pr(6, ref="claude/y")]
	monkeypatch.setattr(sweeper.check_in_status, "gh_api_list", lambda path: prs if path == "repos/o/r/pulls?state=open" else pytest.fail(path))
	assert sweeper.list_candidates("o/r") == [{"number": 1, "head_ref": "claude/x"}, {"number": 6, "head_ref": "claude/y"}]


def _setup(monkeypatch, verdicts, candidates=None):
	monkeypatch.setattr(sweeper, "list_candidates", lambda repo: candidates if candidates is not None else [{"number": n, "head_ref": "claude/x"} for n in verdicts])

	def fake_verdict(repo, number, stuck, min_age, now):
		assert min_age == 2 and stuck == sweeper.check_in_status.DEFAULT_STUCK_HOURS
		verdict = verdicts[number]
		if isinstance(verdict, Exception):
			raise verdict
		return verdict

	monkeypatch.setattr(sweeper.check_in_status, "check_pr_hand_back", fake_verdict)
	posted = []

	def fake_claim(repo, number, head, kind, by):
		posted.append((repo, number, head, kind, by))
		return 0, {"posted": True, "reason": "ok"}

	monkeypatch.setattr(sweeper.claude_fix_claim, "post_claim", fake_claim)
	return posted


def _due(state="ci-failed", kind="ci"):
	return {"done": True, "state": state, "kind": kind, "head_sha": HEAD, "reason": "due"}


def _run(queue=None, queued=None, **overrides):
	kwargs = dict(min_age_hours=2, dry_run=False, self_repo="o/self", queue_token="ghs_x", run_id="123",
		run_url="https://github.com/o/self/actions/runs/123", allowed=["o/r", "o/self"],
		queue=queue or (lambda *a: pytest.fail("must not queue")), queued=queued or (lambda *a: set()))
	kwargs.update(overrides)
	return sweeper.sweep(kwargs.pop("repos", ["o/r"]), NOW, **kwargs)


def test_due_pr_is_queued_once_and_its_head_claimed(monkeypatch, capsys):
	posted = _setup(monkeypatch, {7: _due()})
	queued_items = []

	def fake_queue(self_repo, token, repo, number, head, kind, claim, run_url):
		queued_items.append((self_repo, token, repo, number, head, kind, claim, run_url))
		return 901

	summary = _run(queue=fake_queue)
	assert summary["queued"] == 1 and summary["due"] == 1
	assert queued_items == [("o/self", "ghs_x", "o/r", 7, HEAD, "ci", "sweep-run-123", "https://github.com/o/self/actions/runs/123")]
	assert posted == [("o/r", 7, HEAD, "ci", "sweep-run-123")]
	assert "ghs_x" not in capsys.readouterr().out


def test_an_open_queue_item_for_the_pr_is_not_duplicated(monkeypatch):
	posted = _setup(monkeypatch, {7: _due()})
	summary = _run(queued=lambda *a: {("o/r", 7)})
	assert summary["already_queued"] == 1 and summary["queued"] == 0 and posted == []


def test_not_due_claimed_or_held_prs_are_skipped(monkeypatch):
	verdicts = {
		1: {"done": False, "state": "claimed", "reason": "claimed"},
		2: {"done": False, "state": "held", "reason": "held"},
		3: {"done": False, "state": "open", "reason": "young"},
		4: {"done": True, "state": "merged", "reason": "merged"},
	}
	posted = _setup(monkeypatch, verdicts)
	summary = _run()
	assert summary["skipped"] == 4 and summary["queued"] == 0 and posted == []


def test_dry_run_neither_reads_the_queue_nor_queues(monkeypatch):
	posted = _setup(monkeypatch, {7: _due()})
	summary = _run(dry_run=True, queued=lambda *a: pytest.fail("no queue read in a dry run"))
	assert summary["due"] == 1 and summary["queued"] == 0 and posted == []


def test_without_a_queue_token_it_only_reports(monkeypatch, capsys):
	posted = _setup(monkeypatch, {7: _due("conflict", "conflict")})
	summary = _run(queue_token="", queued=lambda *a: pytest.fail("no queue read without a token"))
	assert summary["reported"] == 1 and posted == []
	assert "::warning::CLAUDE_PR_SWEEP report_only repo=o/r pr=#7" in capsys.readouterr().out


def test_a_failed_queue_read_reports_instead_of_risking_duplicates(monkeypatch, capsys):
	posted = _setup(monkeypatch, {7: _due()})

	def broken(*a):
		raise sweeper.QueueError("HTTP 502")

	summary = _run(queued=broken)
	assert summary["reported"] == 1 and summary["errors"] == 1 and posted == []
	assert "queue_read_failed" in capsys.readouterr().out


def test_queue_failure_does_not_claim(monkeypatch):
	posted = _setup(monkeypatch, {7: _due()})

	def failing(*a):
		raise sweeper.QueueError("HTTP 500")

	summary = _run(queue=failing)
	assert summary["errors"] == 1 and posted == []


def test_read_errors_fail_open_per_pr_and_per_repo(monkeypatch):
	posted = _setup(monkeypatch, {7: sweeper.check_in_status.ReadError("HTTP 502"), 8: _due()})

	def listing(repo):
		if repo == "o/broken":
			raise sweeper.check_in_status.ReadError("HTTP 403")
		return [{"number": 7, "head_ref": "claude/x"}, {"number": 8, "head_ref": "claude/x"}]

	monkeypatch.setattr(sweeper, "list_candidates", listing)
	summary = _run(repos=["o/broken", "o/r"], queue=lambda *a: 5)
	assert summary["errors"] == 2 and summary["queued"] == 1 and [entry[1] for entry in posted] == [8]


def test_queue_pr_fix_posts_a_trusted_queue_issue_with_the_queue_token(monkeypatch):
	calls = []

	def fake_gh(token, args):
		calls.append((token, args[:4]))
		payload = json.loads(Path(args[-1]).read_text())
		assert payload["title"] == "[claude-issue-queue] fix o/r#7"
		assert payload["labels"] == ["ai:claude-issue-queue"]
		assert route.QUEUE_MARKER in payload["body"]
		assert route.build_pr_fix_text("o/r", 7, HEAD, "review", "sweep-run-9") in payload["body"] + "\n"
		return json.dumps({"number": 77})

	monkeypatch.setattr(sweeper, "_gh_as", fake_gh)
	assert sweeper.queue_pr_fix("o/self", "ghs_x", "o/r", 7, HEAD, "review", "sweep-run-9", "") == 77
	assert calls == [("ghs_x", ["api", "-X", "POST", "repos/o/self/issues"])]


def test_queued_pr_fixes_reads_trusted_items_only(monkeypatch):
	item = route.build_pr_fix_queue_issue("o/r", 7, HEAD, "ci", "sweep-run-1")
	issues = [
		{"number": 50, "state": "open", "title": item["title"], "body": item["body"], "labels": [{"name": route.QUEUE_LABEL}], "user": {"login": "github-actions[bot]"}},
		{"number": 51, "state": "open", "title": "[claude-issue-queue] fix o/r#8", "body": route.build_pr_fix_queue_issue("o/r", 8, HEAD, "ci", "sweep-run-1")["body"], "labels": [{"name": route.QUEUE_LABEL}], "user": {"login": "mallory"}},
	]
	monkeypatch.setattr(sweeper, "_gh_as", lambda token, args: json.dumps(issues))
	assert sweeper.queued_pr_fixes("o/self", "ghs_x", ["o/r"]) == {("o/r", 7)}


def test_pr_fix_text_round_trips_and_rejects_bad_fields():
	text = route.build_pr_fix_text("o/r", 7, HEAD, "blocked", "sweep-run-3")
	assert text == f"claude_pr_fix.v1\nrepo: o/r\npr: 7\nurl: https://github.com/o/r/pull/7\nhead: {HEAD}\nkind: blocked\nclaim: sweep-run-3\n"
	assert route.parse_pr_fix_text(text)["pr_number"] == 7
	for bad in (("o/r", 7, "abc", "ci", "sweep-run-1"), ("o/r", 7, HEAD, "nope", "sweep-run-1"), ("o/r", 7, HEAD, "ci", "session_x"), ("bad slug", 7, HEAD, "ci", "sweep-run-1")):
		with pytest.raises(ValueError):
			route.build_pr_fix_text(*bad)


def test_queue_pending_lists_pr_fix_items_next_to_issue_items():
	pr_item = route.build_pr_fix_queue_issue("o/r", 7, HEAD, "ci", "sweep-run-1")
	newer = route.build_pr_fix_queue_issue("o/r", 7, "c" * 40, "conflict", "sweep-run-2")
	issue_item = route.build_queue_issue({"repo": "o/r", "issue_number": 3, "issue_url": "https://github.com/o/r/issues/3", "trigger": "opened", "skip_security_pass": False})
	issues = [
		{"number": 60, "state": "open", "title": pr_item["title"], "body": pr_item["body"], "labels": [{"name": route.QUEUE_LABEL}], "user": {"login": "github-actions[bot]"}},
		{"number": 61, "state": "open", "title": issue_item["title"], "body": issue_item["body"], "labels": [{"name": route.QUEUE_LABEL}], "user": {"login": "github-actions[bot]"}},
		{"number": 62, "state": "open", "title": newer["title"], "body": newer["body"], "labels": [{"name": route.QUEUE_LABEL}], "user": {"login": "github-actions[bot]"}},
		{"number": 63, "state": "open", "title": "[claude-issue-queue] fix o/r#9", "body": pr_item["body"], "labels": [{"name": route.QUEUE_LABEL}], "user": {"login": "github-actions[bot]"}},
	]
	out = route.queue_pending(issues, ["o/r"])
	by_type = {entry["item_type"]: entry for entry in out["pending"]}
	assert set(by_type) == {"pr_fix", "issue"}
	fix = by_type["pr_fix"]
	assert fix["pr_number"] == 7 and fix["head"] == "c" * 40 and fix["kind"] == "conflict" and fix["claim"] == "sweep-run-2"
	assert [queued["number"] for queued in fix["queue_issues"]] == [60, 62]
	assert fix["fire_text"].startswith("claude_pr_fix.v1\n")
	assert {"queue_issue": 63, "reason": "title_mismatch"} in out["ignored"]


def test_workflow_runs_the_catch_all_hourly_with_the_queue_token():
	workflow = yaml.safe_load(SWEEP_WF.read_text())
	on = workflow.get("on", workflow.get(True))
	crons = [entry["cron"] for entry in on["schedule"]]
	assert "*/30 * * * *" in crons and "17 * * * *" in crons
	assert "17 * * * *" in workflow["jobs"]["sweep"]["if"]
	job = workflow["jobs"]["claude-pr-catch-all"]
	assert "17 * * * *" in job["if"] and "workflow_dispatch" in job["if"]
	assert job["permissions"] == {"contents": "read", "issues": "write"}
	step = job["steps"][-1]
	assert "python3 scripts/claude_pr_sweep.py" in step["run"]
	env = step["env"]
	assert env["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	assert env["CLAUDE_PR_SWEEP_QUEUE_TOKEN"] == "${{ github.token }}"
	assert "CLAUDE_ISSUE_ROUTINE_TOKEN" not in env
	assert env["CLAUDE_PR_SWEEP_MIN_AGE_HOURS"] == "${{ vars.CLAUDE_PR_SWEEP_MIN_AGE_HOURS || '2' }}"
	assert env["CLAUDE_FIX_CLAIM_LEASE_HOURS"] == "${{ vars.CLAUDE_FIX_CLAIM_LEASE_HOURS || '3' }}"
	assert env["CLAUDE_FIX_HAND_BACK_CAP"] == "${{ vars.CLAUDE_FIX_HAND_BACK_CAP || '3' }}"
	assert "${{" not in step["run"]
