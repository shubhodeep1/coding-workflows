"""Contract for scripts/claude_pr_sweep.py — the CLAUDE.md §26.H catch-all
that starts a fresh Claude fixer for claude/* PRs nobody handled, and its
wiring in .github/workflows/review_autofix_sweep.yml."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("claude_pr_sweep", ROOT / "scripts" / "claude_pr_sweep.py")
sweeper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweeper)

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


def test_fire_text_has_fixed_keys_only():
	assert sweeper.build_fire_text("o/r", 7, HEAD, "ci", "sweep-run-9") == (
		f"claude_pr_fix.v1\nrepo: o/r\npr: 7\nurl: https://github.com/o/r/pull/7\nhead: {HEAD}\nkind: ci\nclaim: sweep-run-9\n")


def test_candidates_are_open_same_repo_claude_prs(monkeypatch):
	prs = [_pr(1), _pr(2, ref="ai/issue-2"), _pr(3, draft=True), _pr(4, repo="fork/r"), _pr(5, title="x [skip ai]"), _pr(6, ref="claude/y")]
	monkeypatch.setattr(sweeper.check_in_status, "gh_api_list", lambda path: prs if path == "repos/o/r/pulls?state=open" else pytest.fail(path))
	assert sweeper.list_candidates("o/r") == [{"number": 1, "head_ref": "claude/x"}, {"number": 6, "head_ref": "claude/y"}]


def _setup(monkeypatch, verdicts, claims=None, candidates=None):
	monkeypatch.setattr(sweeper, "list_candidates", lambda repo: candidates if candidates is not None else [{"number": n, "head_ref": "claude/x"} for n in verdicts])

	def fake_verdict(repo, number, stuck, min_age, now):
		assert min_age == 2 and stuck == sweeper.check_in_status.DEFAULT_STUCK_HOURS
		verdict = verdicts[number]
		if isinstance(verdict, Exception):
			raise verdict
		return verdict

	monkeypatch.setattr(sweeper.check_in_status, "check_pr_hand_back", fake_verdict)
	posted = claims if claims is not None else []

	def fake_claim(repo, number, head, kind, by):
		posted.append((repo, number, head, kind, by))
		return 0, {"posted": True, "reason": "ok"}

	monkeypatch.setattr(sweeper.claude_fix_claim, "post_claim", fake_claim)
	return posted


def _due(state="ci-failed", kind="ci"):
	return {"done": True, "state": state, "kind": kind, "head_sha": HEAD, "reason": "due"}


def test_due_pr_starts_one_fixer_and_claims_the_head(monkeypatch, capsys):
	posted = _setup(monkeypatch, {7: _due()})
	fired = []

	def fake_fire(routine_id, token, text, **kwargs):
		fired.append((routine_id, text))
		return True, "HTTP 2xx after 1 attempt(s)", "https://claude.ai/code/session_x"

	summary = sweeper.sweep(["o/r"], NOW, min_age_hours=2, dry_run=False, routine_id="trig_abc", token="secret", run_id="123", fire=fake_fire)
	assert summary["started"] == 1 and summary["due"] == 1
	assert fired == [("trig_abc", sweeper.build_fire_text("o/r", 7, HEAD, "ci", "sweep-run-123"))]
	assert posted == [("o/r", 7, HEAD, "ci", "sweep-run-123")]
	assert "secret" not in capsys.readouterr().out


def test_not_due_claimed_or_held_prs_are_skipped(monkeypatch):
	verdicts = {
		1: {"done": False, "state": "claimed", "reason": "claimed"},
		2: {"done": False, "state": "held", "reason": "held"},
		3: {"done": False, "state": "open", "reason": "young"},
		4: {"done": True, "state": "merged", "reason": "merged"},
	}
	posted = _setup(monkeypatch, verdicts)
	summary = sweeper.sweep(["o/r"], NOW, min_age_hours=2, dry_run=False, routine_id="trig_abc", token="t", run_id="1",
		fire=lambda *a, **k: pytest.fail("must not fire"))
	assert summary["skipped"] == 4 and summary["started"] == 0 and posted == []


def test_dry_run_neither_fires_nor_claims(monkeypatch):
	posted = _setup(monkeypatch, {7: _due()})
	summary = sweeper.sweep(["o/r"], NOW, min_age_hours=2, dry_run=True, routine_id="trig_abc", token="t", run_id="1",
		fire=lambda *a, **k: pytest.fail("must not fire"))
	assert summary["due"] == 1 and summary["started"] == 0 and posted == []


def test_unconfigured_routine_reports_instead(monkeypatch, capsys):
	posted = _setup(monkeypatch, {7: _due("conflict", "conflict")})
	summary = sweeper.sweep(["o/r"], NOW, min_age_hours=2, dry_run=False, routine_id="", token="", run_id="1",
		fire=lambda *a, **k: pytest.fail("must not fire"))
	assert summary["reported"] == 1 and posted == []
	assert "::warning::CLAUDE_PR_SWEEP report_only repo=o/r pr=#7" in capsys.readouterr().out


def test_fire_failure_does_not_claim(monkeypatch):
	posted = _setup(monkeypatch, {7: _due()})
	summary = sweeper.sweep(["o/r"], NOW, min_age_hours=2, dry_run=False, routine_id="trig_abc", token="t", run_id="1",
		fire=lambda *a, **k: (False, "HTTP 500 after 5 attempt(s)", ""))
	assert summary["errors"] == 1 and posted == []


def test_read_errors_fail_open_per_pr_and_per_repo(monkeypatch):
	posted = _setup(monkeypatch, {7: sweeper.check_in_status.ReadError("HTTP 502"), 8: _due()})

	def listing(repo):
		if repo == "o/broken":
			raise sweeper.check_in_status.ReadError("HTTP 403")
		return [{"number": 7, "head_ref": "claude/x"}, {"number": 8, "head_ref": "claude/x"}]

	monkeypatch.setattr(sweeper, "list_candidates", listing)
	summary = sweeper.sweep(["o/broken", "o/r"], NOW, min_age_hours=2, dry_run=False, routine_id="trig_abc", token="t", run_id="1",
		fire=lambda *a, **k: (True, "ok", ""))
	assert summary["errors"] == 2 and summary["started"] == 1 and [entry[1] for entry in posted] == [8]


class _Response(io.BytesIO):
	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False


def test_fire_retries_transient_errors_then_succeeds():
	attempts = []

	def opener(request, timeout):
		attempts.append(request)
		assert request.headers["Authorization"] == "Bearer tok"
		assert json.loads(request.data) == {"text": "hello"}
		if len(attempts) < 3:
			raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
		return _Response(json.dumps({"claude_code_session_url": "https://claude.ai/code/s"}).encode())

	sleeps = []
	ok, detail, url = sweeper.fire_routine("trig_1", "tok", "hello", beta="b", base_url="https://x/routines", opener=opener, sleep=sleeps.append)
	assert ok and url == "https://claude.ai/code/s" and sleeps == [2, 4]
	assert attempts[0].full_url == "https://x/routines/trig_1/fire" and "tok" not in detail


def test_fire_stops_on_a_client_error():
	def opener(request, timeout):
		raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)

	ok, detail, url = sweeper.fire_routine("trig_1", "tok", "x", beta="b", base_url="https://x", opener=opener, sleep=lambda s: pytest.fail("no retry"))
	assert not ok and detail == "HTTP 401 after 1 attempt(s)" and url == ""


def test_workflow_runs_the_catch_all_hourly_with_its_secrets():
	workflow = yaml.safe_load(SWEEP_WF.read_text())
	on = workflow.get("on", workflow.get(True))
	crons = [entry["cron"] for entry in on["schedule"]]
	assert "*/30 * * * *" in crons and "17 * * * *" in crons
	sweep_job = workflow["jobs"]["sweep"]
	assert "17 * * * *" in sweep_job["if"]
	job = workflow["jobs"]["claude-pr-catch-all"]
	assert "17 * * * *" in job["if"] and "workflow_dispatch" in job["if"]
	step = job["steps"][-1]
	assert "python3 scripts/claude_pr_sweep.py" in step["run"]
	env = step["env"]
	assert env["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	assert env["CLAUDE_ISSUE_ROUTINE_ID"] == "${{ vars.CLAUDE_ISSUE_ROUTINE_ID || '' }}"
	assert env["CLAUDE_ISSUE_ROUTINE_TOKEN"] == "${{ secrets.CLAUDE_ISSUE_ROUTINE_TOKEN }}"
	assert env["CLAUDE_PR_SWEEP_MIN_AGE_HOURS"] == "${{ vars.CLAUDE_PR_SWEEP_MIN_AGE_HOURS || '2' }}"
	assert env["CLAUDE_FIX_CLAIM_LEASE_HOURS"] == "${{ vars.CLAUDE_FIX_CLAIM_LEASE_HOURS || '3' }}"
	assert env["CLAUDE_FIX_HAND_BACK_CAP"] == "${{ vars.CLAUDE_FIX_HAND_BACK_CAP || '3' }}"
	assert "${{" not in step["run"]
