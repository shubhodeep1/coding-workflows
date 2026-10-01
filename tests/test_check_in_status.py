"""Contract for .claude/scripts/check_in_status.py — the deterministic
"is the wait over?" verdict the Sonnet check-in sessions act on
(`/implement-plan-claude` Check-in Loop and CLAUDE.md §26)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / ".claude" / "scripts" / "check_in_status.py"
TEMPLATE_SCRIPT_PATH = ROOT / "workflow-templates" / ".claude" / "scripts" / "check_in_status.py"

_spec = importlib.util.spec_from_file_location("check_in_status", SCRIPT_PATH)
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)

REPO = "o/r"
NOW = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc)
OLD = "2026-09-23T01:00:00Z"  # 11h before NOW
YOUNG = "2026-09-23T10:00:00Z"  # 2h before NOW


def _pr(**overrides):
	pr = {
		"merged": False,
		"state": "open",
		"labels": [],
		"mergeable_state": "clean",
		"head": {"sha": "abc", "ref": "claude/x"},
	}
	pr.update(overrides)
	return pr


DISPATCH_RUNS = "repos/o/r/actions/workflows/internal-review.yml/runs?event=workflow_dispatch&per_page=100"


def _stub(monkeypatch, responses):
	"""Serve `gh_api(path)` from a {path: payload} map and record the calls."""
	calls = []

	def fake(path):
		calls.append(path)
		if path not in responses:
			raise AssertionError(f"unexpected gh api call: {path}")
		payload = responses[path]
		if isinstance(payload, Exception):
			raise payload
		return payload

	monkeypatch.setattr(checker, "gh_api", fake)
	return calls


def _run(argv, capsys):
	code = checker.main(["--repo", REPO, *argv], now=NOW)
	return code, json.loads(capsys.readouterr().out)


def _stuck_responses(pr, committed_at, queued=0, in_progress=0, check_runs=None):
	return {
		"repos/o/r/pulls/7": pr,
		"repos/o/r/commits/abc/check-runs?per_page=100&page=1": {"check_runs": check_runs or []},
		"repos/o/r/commits/abc": {"commit": {"committer": {"date": committed_at}}},
		"repos/o/r/actions/runs?branch=claude/x&status=queued&per_page=1": {"total_count": queued},
		"repos/o/r/actions/runs?branch=claude/x&status=in_progress&per_page=1": {"total_count": in_progress},
		DISPATCH_RUNS: {"workflow_runs": []},
	}


def test_merged_pr_is_done_with_one_call(monkeypatch, capsys):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/7": _pr(merged=True, merged_at="t", merge_commit_sha="m")})
	code, out = _run(["--pr", "7"], capsys)
	assert code == 0
	assert out["done"] is True and out["state"] == "merged" and out["merge_commit_sha"] == "m"
	assert calls == ["repos/o/r/pulls/7"]


def test_closed_unmerged_pr_is_done(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(state="closed", closed_at="t")})
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "closed"


@pytest.mark.parametrize("label", ["ai:review-blocked", "ai:review-autofix-failed", "ai:needs-human"])
def test_blocking_label_is_done(monkeypatch, capsys, label):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": label}])})
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "blocked" and label in out["reason"]


def test_terminal_only_ignores_blocking_labels_and_red_checks(monkeypatch, capsys):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:needs-human"}], mergeable_state="dirty")})
	_, out = _run(["--pr", "7", "--terminal-only"], capsys)
	assert out["done"] is False
	assert calls == ["repos/o/r/pulls/7"]


def test_healthy_open_pr_is_not_done(monkeypatch, capsys):
	calls = _stub(monkeypatch, {
		"repos/o/r/pulls/7": _pr(),
		"repos/o/r/commits/abc/check-runs?per_page=100&page=1": {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]},
	})
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False
	assert len(calls) == 2


def test_conflict_on_young_head_waits(monkeypatch, capsys):
	responses = _stuck_responses(_pr(mergeable_state="dirty"), YOUNG)
	_stub(monkeypatch, responses)
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False and "merge conflict" in out["reason"]


def test_conflict_on_old_head_with_active_run_waits(monkeypatch, capsys):
	_stub(monkeypatch, _stuck_responses(_pr(mergeable_state="dirty"), OLD, in_progress=1))
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False and "still queued or running" in out["reason"]


def test_conflict_on_old_head_without_active_run_is_stuck(monkeypatch, capsys):
	calls = _stub(monkeypatch, _stuck_responses(_pr(mergeable_state="dirty"), OLD))
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "stuck"
	# A conflict needs no check-run read: pulls, commit, queued, in_progress,
	# and the PR-named dispatch listing (issue #4618).
	assert calls == [
		"repos/o/r/pulls/7",
		"repos/o/r/commits/abc",
		"repos/o/r/actions/runs?branch=claude/x&status=queued&per_page=1",
		"repos/o/r/actions/runs?branch=claude/x&status=in_progress&per_page=1",
		DISPATCH_RUNS,
	]


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_old_head_with_active_sweep_dispatch_for_the_pr_waits(monkeypatch, capsys, status):
	# The sweep dispatches internal-review.yml from the default branch
	# (issue #4618), so its run is not on the head branch; check_pr's stuck
	# path must still count it by its `[pr:<N>]` title.
	responses = _stuck_responses(_pr(mergeable_state="dirty"), OLD)
	responses[DISPATCH_RUNS] = {"workflow_runs": [
		{"status": status, "display_title": "Internal: AI Review & Autofix [pr:7]"},
	]}
	_stub(monkeypatch, responses)
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False and out["state"] == "open" and "still queued or running" in out["reason"]


def test_old_head_with_sweep_dispatch_for_another_pr_or_finished_is_stuck(monkeypatch, capsys):
	responses = _stuck_responses(_pr(mergeable_state="dirty"), OLD)
	responses[DISPATCH_RUNS] = {"workflow_runs": [
		{"status": "in_progress", "display_title": "Internal: AI Review & Autofix [pr:8]"},
		{"status": "completed", "display_title": "Internal: AI Review & Autofix [pr:7]"},
		{"status": "pending", "display_title": "Internal: AI Review & Autofix [pr:7]"},
	]}
	_stub(monkeypatch, responses)
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "stuck"


def test_failed_check_on_old_head_without_active_run_is_stuck(monkeypatch, capsys):
	runs = [
		{"name": "lint", "status": "completed", "conclusion": "failure"},
		{"name": "superseded", "status": "completed", "conclusion": "cancelled"},
	]
	calls = _stub(monkeypatch, _stuck_responses(_pr(mergeable_state="unstable"), OLD, check_runs=runs))
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and "lint" in out["reason"] and "superseded" not in out["reason"]
	assert calls == [
		"repos/o/r/pulls/7",
		"repos/o/r/commits/abc/check-runs?per_page=100&page=1",
		"repos/o/r/commits/abc",
		"repos/o/r/actions/runs?branch=claude/x&status=queued&per_page=1",
		"repos/o/r/actions/runs?branch=claude/x&status=in_progress&per_page=1",
		DISPATCH_RUNS,
	]


def test_failed_check_on_second_page_is_detected(monkeypatch, capsys):
	first_page_runs = [
		{"name": f"ok-{index}", "status": "completed", "conclusion": "success"}
		for index in range(100)
	]
	responses = _stuck_responses(_pr(mergeable_state="unstable"), OLD, check_runs=first_page_runs)
	responses["repos/o/r/commits/abc/check-runs?per_page=100&page=1"]["total_count"] = 101
	responses["repos/o/r/commits/abc/check-runs?per_page=100&page=2"] = {
		"total_count": 101,
		"check_runs": [{"name": "late-failure", "status": "completed", "conclusion": "failure"}],
	}
	calls = _stub(monkeypatch, responses)
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and "late-failure" in out["reason"]
	assert "repos/o/r/commits/abc/check-runs?per_page=100&page=2" in calls


def test_pagination_page_limit_exits_2_with_error(monkeypatch, capsys):
	full_page = [{"name": f"ok-{index}"} for index in range(100)]
	monkeypatch.setattr(checker, "MAX_PAGINATED_API_PAGES", 2)
	_stub(monkeypatch, {
		"repos/o/r/pulls/7": _pr(),
		"repos/o/r/commits/abc/check-runs?per_page=100&page=1": {"check_runs": full_page},
		"repos/o/r/commits/abc/check-runs?per_page=100&page=2": {"check_runs": full_page},
	})
	code, out = _run(["--pr", "7"], capsys)
	assert code == 2 and out["done"] is False and "pagination exceeded 2 pages" in out["error"]


def test_null_commit_time_exits_2_with_error(monkeypatch, capsys):
	_stub(monkeypatch, _stuck_responses(_pr(mergeable_state="dirty"), None))
	code, out = _run(["--pr", "7"], capsys)
	assert code == 2 and out["done"] is False and "timestamp must be a string" in out["error"]


def test_empty_head_sha_exits_2_with_error(monkeypatch, capsys):
	pr = _pr(mergeable_state="dirty", head={"sha": "", "ref": "claude/x"})
	_stub(monkeypatch, {
		"repos/o/r/pulls/7": pr,
		"repos/o/r/commits/": {"commit": {"committer": {"date": ""}}},
	})
	code, out = _run(["--pr", "7"], capsys)
	assert code == 2 and out["done"] is False and out["error"]


FIXER_HEAD = "a" * 40
FIXER_REF = "claude/implement-plan-demo-phase-1"
FIXER_RUN_ID = 42
FIXER_RUN_URL = f"https://github.com/{REPO}/actions/runs/{FIXER_RUN_ID}"


def _fixer_pr(**overrides):
	return _pr(head={"sha": FIXER_HEAD, "ref": FIXER_REF}, **overrides)


def _comment(body, association="OWNER", login="workflow-bot", user_type="User"):
	return {"body": body, "author_association": association, "user": {"login": login, "type": user_type}}


def _handoff(kind="findings", head=FIXER_HEAD, round_number=1):
	if kind == "findings":
		intro = (f"## Review round {round_number}: findings handed to the Claude session\n\n"
			f"Reviewed head: `{head}` ([workflow run]({FIXER_RUN_URL})).")
		ledger = f"\n<!-- ai:claude-fixer-handoff:v2 head={head} round={round_number} ledger={'a' * 64} -->"
	else:
		intro = (f"## Review round {round_number}: merge conflict, handed to the Claude session\n\n"
			f"The head `{head}` conflicts with its base branch, so the reviewer panel did not run ([workflow run]({FIXER_RUN_URL})).")
		ledger = ""
	return f"{intro}\n<!-- ai:claude-fixer-handoff:v1 kind={kind} head={head} round={round_number} -->{ledger}"


def _fixer_responses(pr=None, **run_overrides):
	review_run = {
		"id": FIXER_RUN_ID, "html_url": FIXER_RUN_URL,
		"repository": {"full_name": REPO}, "path": ".github/workflows/ai-review.yml@stable",
		"head_sha": FIXER_HEAD, "head_branch": FIXER_REF,
		"status": "completed", "conclusion": "success",
	}
	review_run.update(run_overrides)
	return {
		"repos/o/r/pulls/7": pr or _fixer_pr(),
		f"repos/o/r/actions/runs/{FIXER_RUN_ID}": review_run,
		f"repos/o/r/commits/{FIXER_HEAD}/check-runs?per_page=100&page=1": {"check_runs": []},
		f"repos/o/r/actions/runs?branch={FIXER_REF}&status=queued&per_page=1": {"total_count": 0},
		f"repos/o/r/actions/runs?branch={FIXER_REF}&status=in_progress&per_page=1": {"total_count": 0},
		f"repos/o/r/actions/runs?branch={FIXER_REF}&status=pending&per_page=1": {"total_count": 0},
		DISPATCH_RUNS: {"workflow_runs": []},
	}


def _verdict(head=FIXER_HEAD):
	return (f"<!-- ai:claude-fixer-verdict:v1 head={head} -->\n"
		f"<!-- ai:claude-fixer-verdict:v2 head={head} round=1 ledger={'a' * 64} -->\nNothing left to fix.")


def _stub_fixer(monkeypatch, responses, comments):
	calls = _stub(monkeypatch, responses)
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "workflow-bot")
	for index, comment in enumerate(comments, 1):
		comment.setdefault("id", index)

	def fake_list(path):
		calls.append(path)
		assert path == "repos/o/r/issues/7/comments"
		return comments

	monkeypatch.setattr(checker, "gh_api_list", fake_list)
	return calls


def test_fixer_findings_handoff_for_current_head_is_a_review_round(monkeypatch, capsys):
	calls = _stub_fixer(monkeypatch, _fixer_responses(), [_comment(_handoff(round_number=2))])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "review-round" and out["round"] == 2
	assert calls == ["repos/o/r/pulls/7", "repos/o/r/issues/7/comments",
		f"repos/o/r/actions/runs/{FIXER_RUN_ID}",
		f"repos/o/r/actions/runs?branch={FIXER_REF}&status=queued&per_page=1",
		f"repos/o/r/actions/runs?branch={FIXER_REF}&status=in_progress&per_page=1",
		f"repos/o/r/actions/runs?branch={FIXER_REF}&status=pending&per_page=1",
		DISPATCH_RUNS]


def test_fixer_conflict_handoff_is_a_conflict_round(monkeypatch, capsys):
	_stub_fixer(monkeypatch, _fixer_responses(), [_comment(_handoff(kind="conflict"))])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "conflict"


@pytest.mark.parametrize("author", ["", "wrong-bot"])
def test_fixer_handoff_requires_configured_exact_author(monkeypatch, capsys, author):
	calls = _stub_fixer(monkeypatch, _fixer_responses(), [_comment(_handoff())])
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", author)
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False
	assert f"repos/o/r/actions/runs/{FIXER_RUN_ID}" not in calls


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_fixer_collaborator_cannot_forge_handoff(monkeypatch, capsys, association):
	calls = _stub_fixer(monkeypatch, _fixer_responses(),
		[_comment(_handoff(), association=association, login="attacker")])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False and f"repos/o/r/actions/runs/{FIXER_RUN_ID}" not in calls


@pytest.mark.parametrize("tamper", [
	lambda body: body.replace("## Review round 1:", "## Review round 2:"),
	lambda body: body.replace("kind=findings", "kind=conflict"),
	lambda body: body.replace("round=1 ledger=", "round=2 ledger="),
	lambda body: body.replace("ledger=" + "a" * 64, "ledger=broken"),
	lambda body: body.replace("Reviewed head: `" + FIXER_HEAD, "Reviewed head: `" + "b" * 40),
	lambda body: body.replace("<!-- ai:claude-fixer-handoff:v1", "> <!-- ai:claude-fixer-handoff:v1"),
	lambda body: body.replace("## Review round 1:", "> ## Review round 1:"),
	lambda body: body.replace("/actions/runs/42", "/actions/runs/not-a-run"),
])
def test_fixer_malformed_handoff_never_wakes(monkeypatch, capsys, tamper):
	calls = _stub_fixer(monkeypatch, _fixer_responses(), [_comment(tamper(_handoff()))])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False and f"repos/o/r/actions/runs/{FIXER_RUN_ID}" not in calls


def test_fixer_stale_or_forged_comment_does_not_supersede_or_answer(monkeypatch, capsys):
	monkeypatch.setenv("CLAUDE_FIXER_VERDICT_BOT_LOGIN", "dedicated-fixer[bot]")
	_stub_fixer(monkeypatch, _fixer_responses(), [
		_comment(_handoff()),
		_comment(_handoff(round_number=2), login="attacker", association="MEMBER"),
		_comment(_verdict(), login="dedicated-fixer[bot]", user_type="Bot"),
	])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False  # The bot answered round 1, not a forged round 2.


@pytest.mark.parametrize("run_change", [
	{"repository": {"full_name": "other/repo"}},
	{"path": ".github/workflows/unrelated.yml@main"},
	{"head_sha": None},
	{"head_sha": "not-a-sha"},
	{"head_branch": "claude/other"},
	{"id": 99},
	{"html_url": "https://github.com/o/r/actions/runs/99"},
	{"status": "in_progress"},
	{"status": "queued"},
	{"status": "completed", "conclusion": "failure"},
])
def test_fixer_handoff_waits_for_verified_successful_run(monkeypatch, capsys, run_change):
	calls = _stub_fixer(monkeypatch, _fixer_responses(**run_change), [_comment(_handoff())])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False and out["state"] == "open"
	assert calls[-1] == f"repos/o/r/actions/runs/{FIXER_RUN_ID}"


def _compare_path(run_head):
	return f"repos/o/r/compare/{run_head}...{FIXER_HEAD}?per_page=1"


def test_fixer_handoff_run_triggered_by_an_older_push_is_a_review_round(monkeypatch, capsys):
	# PR #4594: two quick pushes; the run triggered by the first reviewed the second.
	older = "b" * 40
	responses = {**_fixer_responses(head_sha=older), _compare_path(older): {"status": "ahead", "ahead_by": 1, "behind_by": 0}}
	calls = _stub_fixer(monkeypatch, responses, [_comment(_handoff())])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "review-round"
	assert calls[2:4] == [f"repos/o/r/actions/runs/{FIXER_RUN_ID}", _compare_path(older)]


@pytest.mark.parametrize("status", ["diverged", "behind", "identical", None])
def test_fixer_handoff_run_off_the_reviewed_history_keeps_waiting(monkeypatch, capsys, status):
	other = "b" * 40
	responses = {**_fixer_responses(head_sha=other), _compare_path(other): {"status": status}}
	calls = _stub_fixer(monkeypatch, responses, [_comment(_handoff())])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False and out["state"] == "open"
	assert calls[-1] == _compare_path(other)


def test_fixer_handoff_compare_read_failure_is_retryable(monkeypatch, capsys):
	older = "b" * 40
	responses = {**_fixer_responses(head_sha=older), _compare_path(older): checker.ReadError("HTTP 404")}
	_stub_fixer(monkeypatch, responses, [_comment(_handoff())])
	code, out = _run(["--pr", "7"], capsys)
	assert code == 2 and out["done"] is False and "404" in out["error"]


def test_fixer_handoff_run_read_failure_is_retryable(monkeypatch, capsys):
	responses = _fixer_responses()
	responses[f"repos/o/r/actions/runs/{FIXER_RUN_ID}"] = checker.ReadError("HTTP 503")
	_stub_fixer(monkeypatch, responses, [_comment(_handoff())])
	code, out = _run(["--pr", "7"], capsys)
	assert code == 2 and out["done"] is False and "503" in out["error"]


@pytest.mark.parametrize("status", ["queued", "in_progress", "pending"])
def test_fixer_handoff_waits_for_another_active_run(monkeypatch, capsys, status):
	responses = _fixer_responses()
	responses[f"repos/o/r/actions/runs?branch={FIXER_REF}&status={status}&per_page=1"] = {"total_count": 1}
	_stub_fixer(monkeypatch, responses, [_comment(_handoff())])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False and "still queued or running" in out["reason"]


def test_fixer_conflict_handoff_requires_completed_run(monkeypatch, capsys):
	_stub_fixer(monkeypatch, _fixer_responses(_fixer_pr(mergeable_state="dirty"), status="in_progress"),
		[_comment(_handoff(kind="conflict"))])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False


def test_fixer_no_handoff_conflict_remains_immediate(monkeypatch, capsys):
	_stub_fixer(monkeypatch, _fixer_responses(_fixer_pr(mergeable_state="dirty")), [])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "conflict"


def test_fixer_handoff_answered_by_verdict_keeps_waiting(monkeypatch, capsys):
	monkeypatch.setenv("CLAUDE_FIXER_VERDICT_BOT_LOGIN", "dedicated-fixer[bot]")
	_stub_fixer(
		monkeypatch,
		_fixer_responses(),
		[_comment(_handoff()), _comment(_verdict(), login="dedicated-fixer[bot]", user_type="Bot")],
	)
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False


def test_forged_or_stale_verdict_never_hides_handoff(monkeypatch, capsys):
	monkeypatch.setenv("CLAUDE_FIXER_VERDICT_BOT_LOGIN", "dedicated-fixer[bot]")
	for verdict in (
		_comment(_verdict(), login="dev", association="COLLABORATOR"),
		_comment(_verdict(), login="dedicated-fixer[bot]", user_type="User"),
		_comment(_verdict().replace("ledger=" + "a" * 64, "ledger=" + "b" * 64), login="dedicated-fixer[bot]", user_type="Bot"),
	):
		_stub_fixer(monkeypatch, _fixer_responses(), [_comment(_handoff()), verdict])
		_, out = _run(["--pr", "7"], capsys)
		assert out["done"] is True and out["state"] == "review-round"


def test_fixer_handoff_instruction_cannot_answer_its_own_round(monkeypatch, capsys):
	body = _handoff() + "\nReply with `" + _verdict().splitlines()[0] + "` when done."
	_stub_fixer(monkeypatch, _fixer_responses(), [_comment(body)])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "review-round"


def test_fixer_new_handoff_on_same_head_supersedes_old_verdict(monkeypatch, capsys):
	monkeypatch.setenv("CLAUDE_FIXER_VERDICT_BOT_LOGIN", "dedicated-fixer[bot]")
	_stub_fixer(monkeypatch, _fixer_responses(), [
		_comment(_handoff()),
		_comment(_verdict(), login="dedicated-fixer[bot]", user_type="Bot"),
		_comment(_handoff(round_number=2)),
	])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "review-round" and out["round"] == 2


def test_fixer_verdict_before_handoff_cannot_answer_it(monkeypatch, capsys):
	monkeypatch.setenv("CLAUDE_FIXER_VERDICT_BOT_LOGIN", "dedicated-fixer[bot]")
	_stub_fixer(monkeypatch, _fixer_responses(), [
		_comment(_verdict(), login="dedicated-fixer[bot]", user_type="Bot"),
		_comment(_handoff()),
	])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "review-round"


def test_fixer_handoff_for_an_older_head_is_ignored(monkeypatch, capsys):
	_stub_fixer(
		monkeypatch,
		_fixer_responses(),
		[_comment(_handoff(head="b" * 40))],
	)
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False


def test_fixer_handoff_from_configured_author_ignores_association(monkeypatch, capsys):
	_stub_fixer(
		monkeypatch,
		_fixer_responses(),
		[_comment(_handoff(), association="NONE")],
	)
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "review-round"


def test_fixer_conflict_without_active_run_is_immediate(monkeypatch, capsys):
	_stub_fixer(
		monkeypatch,
		_fixer_responses(_fixer_pr(mergeable_state="dirty")),
		[_comment(_handoff()), _comment(_verdict())],
	)
	_, out = _run(["--pr", "7"], capsys)
	# No 6-hour wait and no head-commit read: a verdict never hides a conflict.
	assert out["done"] is True and out["state"] == "conflict"


def test_fixer_conflict_with_active_run_waits(monkeypatch, capsys):
	_stub_fixer(
		monkeypatch,
		{**_fixer_responses(_fixer_pr(mergeable_state="dirty")),
			f"repos/o/r/actions/runs?branch={FIXER_REF}&status=queued&per_page=1": {"total_count": 1}},
		[],
	)
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False and "still queued or running" in out["reason"]


def test_fixer_blocking_label_still_wins(monkeypatch, capsys):
	_stub_fixer(monkeypatch, {"repos/o/r/pulls/7": _fixer_pr(labels=[{"name": "ai:review-blocked"}])}, [_comment(_handoff())])
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "blocked"


def test_non_fixer_pr_never_reads_comments(monkeypatch, capsys):
	calls = _stub(monkeypatch, {
		"repos/o/r/pulls/7": _pr(),
		"repos/o/r/commits/abc/check-runs?per_page=100&page=1": {"check_runs": []},
	})
	monkeypatch.setattr(checker, "gh_api_list", lambda path: pytest.fail("comments read for a non-fixer PR"))
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False and len(calls) == 2


def test_gh_api_list_paginates_and_rejects_objects(monkeypatch):
	pages = {
		"repos/o/r/issues/7/comments?per_page=100&page=1": [{"body": str(i)} for i in range(100)],
		"repos/o/r/issues/7/comments?per_page=100&page=2": [{"body": "last"}],
	}
	monkeypatch.setattr(checker, "_gh_api_json", lambda path: pages[path])
	assert len(checker.gh_api_list("repos/o/r/issues/7/comments")) == 101
	monkeypatch.setattr(checker, "_gh_api_json", lambda path: {"message": "x"})
	with pytest.raises(checker.ReadError, match="non-array page"):
		checker.gh_api_list("repos/o/r/issues/7/comments")


def test_run_completed_and_pending(monkeypatch, capsys):
	for conclusion in (
		"failure", "cancelled", "timed_out", "action_required",
		"startup_failure", "skipped", "neutral", "stale", None,
	):
		_stub(monkeypatch, {"repos/o/r/actions/runs/9": {"status": "completed", "conclusion": conclusion}})
		_, out = _run(["--run", "9"], capsys)
		assert out["done"] is True and out["conclusion"] == conclusion and out["state"] == "failed"
	_stub(monkeypatch, {"repos/o/r/actions/runs/9": {"status": "completed", "conclusion": "success"}})
	_, out = _run(["--run", "9"], capsys)
	assert out["done"] is True and out["state"] == "completed"
	_stub(monkeypatch, {"repos/o/r/actions/runs/9": {"status": "in_progress"}})
	_, out = _run(["--run", "9"], capsys)
	assert out["done"] is False


def test_issues_done_only_when_all_closed_or_merged(monkeypatch, capsys):
	_stub(monkeypatch, {
		"repos/o/r/issues/1": {"state": "closed", "labels": []},
		"repos/o/r/issues/2": {"state": "open", "labels": [{"name": "ai:merged"}]},
		"repos/o/r/issues/3": {"state": "open", "labels": []},
	})
	_, out = _run(["--issues", "1,#2,3"], capsys)
	assert out["done"] is False and "#3" in out["reason"]
	_, out = _run(["--issues", "1,2"], capsys)
	assert out["done"] is True


@pytest.mark.parametrize("label", checker.BLOCKING_LABELS)
def test_issues_blocked_label_on_open_issue_ends_the_wait(monkeypatch, capsys, label):
	_stub(monkeypatch, {
		"repos/o/r/issues/1": {"state": "closed", "labels": []},
		"repos/o/r/issues/2": {"state": "open", "labels": [{"name": "ai:security"}, {"name": label}]},
		"repos/o/r/issues/3": {"state": "open", "labels": []},
	})
	code, out = _run(["--issues", "1,2,3"], capsys)
	assert code == 0
	assert out["done"] is True and out["state"] == "blocked"
	assert f"#2 ({label})" in out["reason"] and "#3" not in out["reason"]


def test_issues_blocking_label_ignored_once_closed_or_merged(monkeypatch, capsys):
	_stub(monkeypatch, {
		"repos/o/r/issues/1": {"state": "closed", "labels": [{"name": "ai:review-blocked"}]},
		"repos/o/r/issues/2": {"state": "open", "labels": [{"name": "ai:merged"}, {"name": "ai:needs-human"}]},
	})
	_, out = _run(["--issues", "1,2"], capsys)
	assert out["done"] is True and out["state"] == "resolved"


def test_command_doc_describes_blocked_issue_wait():
	for path in (ROOT / ".claude" / "commands" / "implement-plan-claude.md",
		ROOT / "workflow-templates" / ".claude" / "commands" / "implement-plan-claude.md"):
		text = path.read_text(encoding="utf-8")
		assert "- *Issue list* — every issue is closed or labelled `ai:merged`; or an issue still open" in text
		assert "the checker reports a follow-up blocked (`state: blocked`" in text


def test_read_failure_exits_2_with_error(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": checker.ReadError("HTTP 403")})
	code, out = _run(["--pr", "7"], capsys)
	assert code == 2 and out["done"] is False and "403" in out["error"]


def test_non_object_api_payload_raises_read_error(monkeypatch):
	monkeypatch.setattr(
		checker.subprocess,
		"run",
		lambda command, **kwargs: checker.subprocess.CompletedProcess(command, 0, stdout="[]", stderr=""),
	)
	with pytest.raises(checker.ReadError, match="non-object JSON"):
		checker.gh_api("repos/o/r/pulls/7")


def test_bad_repo_exits_2(capsys):
	code = checker.main(["--repo", "nope", "--pr", "1"], now=NOW)
	out = json.loads(capsys.readouterr().out)
	assert code == 2 and "OWNER/REPO" in out["error"]


def test_only_rest_reads_through_gh_api():
	text = SCRIPT_PATH.read_text(encoding="utf-8")
	assert '"graphql"' not in text.lower()
	assert '["gh", "api", path]' in text


def test_template_parity():
	assert TEMPLATE_SCRIPT_PATH.read_text(encoding="utf-8") == SCRIPT_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [ROOT / ".claude" / "settings.json", ROOT / "workflow-templates" / ".claude" / "settings.json"])
def test_settings_preapprove_the_checker_tools(path):
	allow = json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]
	assert "Bash(PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/check_in_status.py *)" in allow
	for tool in ("create_session", "archive_session", "send_later", "get_session", "set_session_title"):
		assert f"mcp__Claude_Code_Remote__{tool}" in allow
	# Generated server name that create_session children see in this account's environment.
	assert "mcp__bf7c680d-5fdc-5ef4-b4a0-abadb619bf0a" in allow
	# Exact per-tool rules for the tools a woken checker calls, plus the
	# notification read a Routine wake queues.
	for tool in ("send_later", "set_session_title", "create_session", "archive_session", "get_session"):
		assert f"mcp__bf7c680d-5fdc-5ef4-b4a0-abadb619bf0a__{tool}" in allow
	assert "ReadNotifications" in allow
	# Allow rules cannot glob the server segment; an unanchored MCP glob would be skipped.
	assert not any(rule.startswith("mcp__*") for rule in allow)


# --- Routing: `action` / `next_stage` (route_verdict) -------------------------
# The checker model follows `action` only; the #4596 checker read
# `state: review-round` and wrongly handed the PR back instead of starting a
# review-round stage session.

@pytest.mark.parametrize("mode, state, expected", [
	("pr", "merged", {"action": "next_stage", "next_stage": "success"}),
	("pr", "review-round", {"action": "next_stage", "next_stage": "review"}),
	("pr", "conflict", {"action": "next_stage", "next_stage": "review"}),
	("pr", "blocked", {"action": "hand_back"}),
	("pr", "closed", {"action": "hand_back"}),
	("pr", "stuck", {"action": "hand_back"}),
	("run", "completed", {"action": "next_stage", "next_stage": "success"}),
	("run", "failed", {"action": "next_stage", "next_stage": "block"}),
	("issues", "resolved", {"action": "next_stage", "next_stage": "success"}),
	("issues", "blocked", {"action": "next_stage", "next_stage": "block"}),
	("hand_back", "conflict", {"action": "hand_back_fixer"}),
	("hand_back", "review-round", {"action": "hand_back_fixer"}),
	("hand_back", "ci-failed", {"action": "hand_back_fixer"}),
	("hand_back", "blocked", {"action": "hand_back_fixer"}),
	("hand_back", "merged", {"action": "hand_back_all"}),
	("hand_back", "closed", {"action": "hand_back_all"}),
])
def test_route_verdict_done_rows(mode, state, expected):
	assert checker.route_verdict({"done": True, "state": state}, mode) == expected


@pytest.mark.parametrize("mode, state", [
	("pr", "open"), ("pr", "review-round"), ("run", "in_progress"), ("run", "queued"),
	("issues", "open"), ("hand_back", "open"), ("hand_back", "claimed"), ("hand_back", "held"),
	("hand_back", "ci-failed"),
])
def test_route_verdict_not_done_is_always_wait(mode, state):
	assert checker.route_verdict({"done": False, "state": state}, mode) == {"action": "wait"}


def test_route_table_matches_the_documented_rows():
	# Every row of the table is exercised above; no mode gains a row silently.
	assert {mode: sorted(rows) for mode, rows in checker.CHECKER_ROUTE_TABLE.items()} == {
		"pr": sorted(["merged", "review-round", "conflict", "blocked", "closed", "stuck"]),
		"run": sorted(["completed", "failed"]),
		"issues": sorted(["resolved", "blocked"]),
		"hand_back": sorted(["conflict", "review-round", "ci-failed", "blocked", "merged", "closed"]),
	}


def test_review_round_and_conflict_never_hand_back():
	for state in ("review-round", "conflict"):
		assert checker.route_verdict({"done": True, "state": state}, "pr")["action"] != "hand_back"


@pytest.mark.parametrize("mode, state", [("pr", "ci-failed"), ("run", "resolved"), ("issues", "merged"), ("hand_back", "stuck"), ("bogus", "merged")])
def test_route_verdict_rejects_an_unmapped_done_state(mode, state):
	with pytest.raises(ValueError, match="no checker route"):
		checker.route_verdict({"done": True, "state": state}, mode)


def test_unmapped_done_state_exits_2_with_retry(monkeypatch, capsys):
	monkeypatch.setattr(checker, "check_run", lambda repo, run_id: {"done": True, "state": "mystery", "reason": "x"})
	code, out = _run(["--run", "9"], capsys)
	assert code == 2 and out == {"done": False, "error": "no checker route for mode 'run' state 'mystery'", "action": "retry"}


def test_main_adds_action_for_merged_pr(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(merged=True, merged_at="t", merge_commit_sha="m")})
	code, out = _run(["--pr", "7"], capsys)
	assert code == 0 and out["action"] == "next_stage" and out["next_stage"] == "success"
	# Additive: the existing fields are unchanged.
	assert out["done"] is True and out["state"] == "merged" and out["merge_commit_sha"] == "m"


@pytest.mark.parametrize("pr_overrides", [
	{"state": "closed", "closed_at": "t"},
	{"labels": [{"name": "ai:review-blocked"}]},
])
def test_main_hands_back_closed_and_blocked_prs(monkeypatch, capsys, pr_overrides):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(**pr_overrides)})
	_, out = _run(["--pr", "7"], capsys)
	assert out["action"] == "hand_back" and "next_stage" not in out


def test_main_hands_back_a_stuck_pr(monkeypatch, capsys):
	_stub(monkeypatch, _stuck_responses(_pr(mergeable_state="dirty"), OLD))
	_, out = _run(["--pr", "7"], capsys)
	assert out["state"] == "stuck" and out["action"] == "hand_back"


def test_main_open_pr_waits(monkeypatch, capsys):
	_stub(monkeypatch, _stuck_responses(_pr(mergeable_state="dirty"), YOUNG))
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is False and out["action"] == "wait" and "next_stage" not in out


def test_main_terminal_only_routes_like_plain_pr_mode(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(state="closed", closed_at="t")})
	_, out = _run(["--pr", "7", "--terminal-only"], capsys)
	assert out["action"] == "hand_back"
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:needs-human"}])})
	_, out = _run(["--pr", "7", "--terminal-only"], capsys)
	assert out["action"] == "wait"


def test_main_review_round_starts_the_review_stage_not_a_hand_back(monkeypatch, capsys):
	"""Regression for PR #4596: a review round is `next_stage` + `review`."""
	_stub_fixer(monkeypatch, _fixer_responses(), [_comment(_handoff(round_number=2))])
	_, out = _run(["--pr", "7"], capsys)
	assert out["state"] == "review-round"
	assert out["action"] == "next_stage" and out["next_stage"] == "review"


def test_main_conflict_starts_the_review_stage_not_a_hand_back(monkeypatch, capsys):
	_stub_fixer(monkeypatch, _fixer_responses(_fixer_pr(mergeable_state="dirty")), [])
	_, out = _run(["--pr", "7"], capsys)
	assert out["state"] == "conflict"
	assert out["action"] == "next_stage" and out["next_stage"] == "review"


@pytest.mark.parametrize("run, expected", [
	({"status": "completed", "conclusion": "success"}, {"action": "next_stage", "next_stage": "success"}),
	({"status": "completed", "conclusion": "failure"}, {"action": "next_stage", "next_stage": "block"}),
	({"status": "completed", "conclusion": "cancelled"}, {"action": "next_stage", "next_stage": "block"}),
	({"status": "in_progress"}, {"action": "wait"}),
])
def test_main_run_mode_actions(monkeypatch, capsys, run, expected):
	_stub(monkeypatch, {"repos/o/r/actions/runs/9": run})
	code, out = _run(["--run", "9"], capsys)
	assert code == 0 and {key: out[key] for key in expected} == expected
	assert ("next_stage" in out) == ("next_stage" in expected)


@pytest.mark.parametrize("issues, expected", [
	({"repos/o/r/issues/1": {"state": "closed", "labels": []}}, {"action": "next_stage", "next_stage": "success"}),
	({"repos/o/r/issues/1": {"state": "open", "labels": [{"name": "ai:review-blocked"}]}}, {"action": "next_stage", "next_stage": "block"}),
	({"repos/o/r/issues/1": {"state": "open", "labels": []}}, {"action": "wait"}),
])
def test_main_issue_mode_actions(monkeypatch, capsys, issues, expected):
	_stub(monkeypatch, issues)
	code, out = _run(["--issues", "1"], capsys)
	assert code == 0 and {key: out[key] for key in expected} == expected
	assert ("next_stage" in out) == ("next_stage" in expected)


@pytest.mark.parametrize("argv", [["--pr", "7"], ["--run", "9"], ["--issues", "1"]])
def test_read_failure_carries_retry_in_every_mode(monkeypatch, capsys, argv):
	_stub(monkeypatch, {
		"repos/o/r/pulls/7": checker.ReadError("HTTP 502"),
		"repos/o/r/actions/runs/9": checker.ReadError("HTTP 502"),
		"repos/o/r/issues/1": checker.ReadError("HTTP 502"),
	})
	code, out = _run(argv, capsys)
	assert code == 2 and out == {"done": False, "error": "HTTP 502", "action": "retry"}


def test_bad_repo_carries_retry(capsys):
	code = checker.main(["--repo", "nope", "--pr", "1"], now=NOW)
	out = json.loads(capsys.readouterr().out)
	assert code == 2 and out["action"] == "retry"


# --- Plain PR mode honours a hold on a Claude-fixer head (issue #5667) --------
# Twin-first (Q40 / #4948): the change lands in the workflow-templates twin
# first and reaches .claude/ by a [claude-twin-sync] commit, so these tests run
# against the twin; test_template_parity keeps the two copies identical.

_twin_spec = importlib.util.spec_from_file_location("check_in_status_twin", TEMPLATE_SCRIPT_PATH)
twin_checker = importlib.util.module_from_spec(_twin_spec)
_twin_spec.loader.exec_module(twin_checker)

HOLD_PR_AUTHOR = "pr-author"


def _held_fixer_pr(**overrides):
	return _fixer_pr(user={"login": HOLD_PR_AUTHOR}, **overrides)


def _claim(kind="hold", head=FIXER_HEAD, by="session_01Stage", login=HOLD_PR_AUTHOR, association="OWNER"):
	return {
		"body": f"**Claude fixes on hold.**\n<!-- ai:claude-fix-claim:v1 head={head} kind={kind} by={by} -->",
		"author_association": association,
		"user": {"login": login, "type": "User"},
		"created_at": YOUNG,
	}


def _stub_twin_fixer(monkeypatch, responses, comments):
	"""Serve the twin's `gh_api` / `gh_api_list` like `_stub_fixer` does."""
	calls = []

	def fake(path):
		calls.append(path)
		if path not in responses:
			raise AssertionError(f"unexpected gh api call: {path}")
		payload = responses[path]
		if isinstance(payload, Exception):
			raise payload
		return payload

	def fake_list(path):
		calls.append(path)
		assert path == "repos/o/r/issues/7/comments"
		return comments

	for index, comment in enumerate(comments, 1):
		comment.setdefault("id", index)
	monkeypatch.setattr(twin_checker, "gh_api", fake)
	monkeypatch.setattr(twin_checker, "gh_api_list", fake_list)
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "workflow-bot")
	return calls


def _twin_run(argv, capsys):
	code = twin_checker.main(["--repo", REPO, *argv], now=NOW)
	return code, json.loads(capsys.readouterr().out)


def test_held_head_with_a_later_handoff_waits_instead_of_a_review_round(monkeypatch, capsys):
	# The issue's case: the stage pushed a twin-first fix, held the head, and the
	# review workflow then handed that same head off before the twin sync.
	calls = _stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()),
		[_claim(), _comment(_handoff(round_number=2))])
	code, out = _twin_run(["--pr", "7"], capsys)
	assert code == 0
	assert out["done"] is False and out["state"] == "held" and out["action"] == "wait"
	assert "next_stage" not in out
	assert out["head_sha"] == FIXER_HEAD and out["claim"]["kind"] == "hold" and out["claim"]["by"] == "session_01Stage"
	# No review-run, check-run, or branch-run read: the PR and one comment listing.
	assert calls == ["repos/o/r/pulls/7", "repos/o/r/issues/7/comments"]


@pytest.mark.parametrize("pr_overrides", [
	{"mergeable_state": "dirty"},
	{"labels": [{"name": "ai:review-blocked"}]},
	{"labels": [{"name": "ai:needs-human"}], "mergeable_state": "dirty"},
])
def test_hold_outranks_conflict_and_blocking_labels(monkeypatch, capsys, pr_overrides):
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr(**pr_overrides)), [_claim()])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "held" and out["action"] == "wait"


def test_hold_posted_by_the_workflow_account_counts(monkeypatch, capsys):
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()),
		[_claim(login="workflow-bot", association="MEMBER"), _comment(_handoff())])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "held"


@pytest.mark.parametrize("pr_overrides", [
	{"merged": True, "merged_at": "t", "merge_commit_sha": "m"},
	{"state": "closed", "closed_at": "t"},
])
def test_merged_or_closed_outranks_a_hold_without_reading_comments(monkeypatch, capsys, pr_overrides):
	calls = _stub_twin_fixer(monkeypatch, {"repos/o/r/pulls/7": _held_fixer_pr(**pr_overrides)}, [_claim()])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] in ("merged", "closed")
	assert calls == ["repos/o/r/pulls/7"]


def test_a_push_lifts_the_hold(monkeypatch, capsys):
	# The hold names the old head; the twin sync pushed a new one, which the
	# workflow then handed off: that is a real review round.
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()),
		[_claim(head="b" * 40), _comment(_handoff())])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "review-round" and out["action"] == "next_stage" and out["next_stage"] == "review"


@pytest.mark.parametrize("claim", [
	_claim(login="attacker", association="COLLABORATOR"),
	_claim(association="NONE"),
	_claim(association="CONTRIBUTOR"),
])
def test_untrusted_hold_never_stops_a_review_round(monkeypatch, capsys, claim):
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()), [dict(claim), _comment(_handoff())])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "review-round"


@pytest.mark.parametrize("kind", ["review", "conflict", "ci", "blocked"])
def test_a_non_hold_claim_does_not_stop_the_project_checker(monkeypatch, capsys, kind):
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()), [_claim(kind=kind), _comment(_handoff())])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "review-round"


def test_an_older_hold_then_a_newer_claim_on_the_same_head_is_not_held(monkeypatch, capsys):
	# The latest trusted claim on the head decides, as in --hand-back mode.
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()),
		[_claim(), _claim(kind="review", by="session_01Resumed"), _comment(_handoff())])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "review-round"


def test_blocked_fixer_head_without_a_hold_reads_comments_once(monkeypatch, capsys):
	# The hold outranks a blocking label (AD-2), so a blocked fixer PR pays one
	# comment listing to rule the hold out; the changelog states that cost.
	calls = _stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr(labels=[{"name": "ai:review-blocked"}])),
		[_comment(_handoff())])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "blocked" and out["action"] == "hand_back"
	assert calls == ["repos/o/r/pulls/7", "repos/o/r/issues/7/comments"]


def test_fixer_handoff_reads_comments_once(monkeypatch, capsys):
	calls = _stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()), [_comment(_handoff())])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "review-round"
	assert calls.count("repos/o/r/issues/7/comments") == 1


def test_non_fixer_claude_head_ignores_a_hold(monkeypatch, capsys):
	# Plain mode reads comments only for claude/implement-plan- heads (AD-3).
	calls = _stub_twin_fixer(monkeypatch, {
		"repos/o/r/pulls/7": _pr(user={"login": HOLD_PR_AUTHOR}),
		"repos/o/r/commits/abc/check-runs?per_page=100&page=1": {"check_runs": []},
	}, [_claim(head="abc")])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "open" and "repos/o/r/issues/7/comments" not in calls


def test_held_fixer_head_with_malformed_sha_exits_2(monkeypatch, capsys):
	_stub_twin_fixer(monkeypatch, {"repos/o/r/pulls/7": _pr(head={"sha": "abc", "ref": FIXER_REF})}, [])
	code, out = _twin_run(["--pr", "7"], capsys)
	assert code == 2 and out["action"] == "retry" and "40 lowercase hex" in out["error"]


def test_hold_comment_read_failure_is_retryable(monkeypatch, capsys):
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()), [])

	def failing_list(path):
		raise twin_checker.ReadError("HTTP 502")

	monkeypatch.setattr(twin_checker, "gh_api_list", failing_list)
	code, out = _twin_run(["--pr", "7"], capsys)
	assert code == 2 and out == {"done": False, "error": "HTTP 502", "action": "retry"}


def test_route_verdict_held_in_plain_pr_mode_is_wait():
	assert twin_checker.route_verdict({"done": False, "state": "held"}, "pr") == {"action": "wait"}


def test_command_twin_documents_the_held_wait():
	text = " ".join((ROOT / "workflow-templates" / ".claude" / "commands" / "implement-plan-claude.md").read_text(encoding="utf-8").split())
	section = text[text.index("**What counts as \"done waiting\"**"):text.index("### Checker prompt")]
	assert "**Held**: a trusted `hold` claim on the current head" in section
	assert "is not done (`state: held`, `action: wait`)" in section
	assert "The hold goes on the head it pushed, in the same step as the blocker" in text


# --- A plain-mode hold is bounded by age (issue #5927) ------------------------
# A hold that waited CLAUDE_FIX_HOLD_MAX_HOURS (default 24) or longer, or one
# whose comment time cannot be read, is handed back as blocked instead of
# parking the project checker for good.

HOLD_EXACTLY_AT_LIMIT = "2026-09-22T12:00:00Z"  # 24h before NOW
HOLD_30_HOURS_OLD = "2026-09-22T06:00:00Z"
HOLD_23_HOURS_OLD = "2026-09-22T13:00:00Z"


def _aged_claim(created_at, **claim_kwargs):
	aged = _claim(**claim_kwargs)
	aged["created_at"] = created_at
	return aged


@pytest.mark.parametrize("created_at", [HOLD_23_HOURS_OLD, YOUNG])
def test_hold_younger_than_the_limit_still_waits(monkeypatch, capsys, created_at):
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr(labels=[{"name": "ai:needs-human"}])),
		[_aged_claim(created_at), _comment(_handoff())])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["done"] is False and out["state"] == "held" and out["action"] == "wait"


@pytest.mark.parametrize("created_at", [HOLD_EXACTLY_AT_LIMIT, HOLD_30_HOURS_OLD])
@pytest.mark.parametrize("pr_overrides,extra_comments", [
	({}, []),
	({}, [_comment(_handoff())]),
	({"mergeable_state": "dirty"}, []),
	({"labels": [{"name": "ai:review-blocked"}]}, [_comment(_handoff())]),
])
def test_stale_hold_is_handed_back_as_blocked(monkeypatch, capsys, created_at, pr_overrides, extra_comments):
	# The finding's case: a hold nobody answered no longer outranks a blocking
	# label, a hand-off, or a conflict; it routes to the blocked-PR hand-back.
	calls = _stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr(**pr_overrides)),
		[_aged_claim(created_at), *[dict(comment) for comment in extra_comments]])
	code, out = _twin_run(["--pr", "7"], capsys)
	assert code == 0
	assert out["done"] is True and out["state"] == "blocked" and out["action"] == "hand_back"
	assert "next_stage" not in out
	assert out["head_sha"] == FIXER_HEAD and out["claim"]["kind"] == "hold" and out["claim"]["by"] == "session_01Stage"
	assert "past the 24h limit" in out["reason"] and "session_01Stage" in out["reason"]
	# No new API call: the PR and the one comment listing it already read.
	assert calls == ["repos/o/r/pulls/7", "repos/o/r/issues/7/comments"]


def test_stale_hold_reason_names_the_blocking_labels(monkeypatch, capsys):
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr(labels=[{"name": "ai:needs-human"}, {"name": "other"}])),
		[_aged_claim(HOLD_30_HOURS_OLD)])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "blocked"
	assert "30.0h old" in out["reason"] and out["reason"].endswith("; labels: ai:needs-human")


@pytest.mark.parametrize("created_at", [None, "", "not-a-time", 12345])
def test_hold_with_an_unreadable_time_is_handed_back(monkeypatch, capsys, created_at):
	claim = _claim()
	if created_at is None:
		del claim["created_at"]
	else:
		claim["created_at"] = created_at
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()), [claim])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["done"] is True and out["state"] == "blocked" and out["action"] == "hand_back"
	assert "of unknown age" in out["reason"]


def test_hold_limit_is_configurable(monkeypatch, capsys):
	monkeypatch.setenv("CLAUDE_FIX_HOLD_MAX_HOURS", "48")
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()), [_aged_claim(HOLD_30_HOURS_OLD)])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "held"
	# A 2-hour limit hands back even the 2-hour-old hold.
	monkeypatch.setenv("CLAUDE_FIX_HOLD_MAX_HOURS", "2")
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()), [_aged_claim(YOUNG)])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "blocked" and "past the 2h limit" in out["reason"]


@pytest.mark.parametrize("bad_limit", ["0", "-1", "abc", ""])
def test_invalid_hold_limit_falls_back_to_24_hours(monkeypatch, capsys, bad_limit):
	monkeypatch.setenv("CLAUDE_FIX_HOLD_MAX_HOURS", bad_limit)
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()), [_aged_claim(HOLD_23_HOURS_OLD)])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "held"
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()), [_aged_claim(HOLD_EXACTLY_AT_LIMIT)])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "blocked"


def test_a_newer_hold_restarts_the_limit(monkeypatch, capsys):
	# The latest trusted claim on the head decides (AD-5 of issue #5927).
	_stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr()),
		[_aged_claim(HOLD_30_HOURS_OLD), _aged_claim(YOUNG, by="session_01Resumed")])
	_, out = _twin_run(["--pr", "7"], capsys)
	assert out["state"] == "held" and out["claim"]["by"] == "session_01Resumed"


def test_hand_back_mode_keeps_a_stale_hold(monkeypatch, capsys):
	# The bound is plain mode only; the §26.H cap hold is unchanged (AD-4).
	calls = _stub_twin_fixer(monkeypatch, _fixer_responses(_held_fixer_pr(labels=[{"name": "ai:review-blocked"}])),
		[_aged_claim(HOLD_30_HOURS_OLD)])
	_, out = _twin_run(["--pr", "7", "--hand-back"], capsys)
	assert out["done"] is False and out["state"] == "held" and out["action"] == "wait"
	assert calls == ["repos/o/r/pulls/7", "repos/o/r/issues/7/comments"]


def test_command_twin_documents_the_bounded_hold():
	text = " ".join((ROOT / "workflow-templates" / ".claude" / "commands" / "implement-plan-claude.md").read_text(encoding="utf-8").split())
	section = text[text.index("**What counts as \"done waiting\"**"):text.index("### Checker prompt")]
	assert "The wait is bounded: a hold at least `CLAUDE_FIX_HOLD_MAX_HOURS` old (default 24" in section
	assert "is done as `state: blocked` (`action: hand_back`)" in section
