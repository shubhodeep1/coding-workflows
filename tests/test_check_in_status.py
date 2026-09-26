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
	# A conflict needs no check-run read: pulls, commit, queued, in_progress.
	assert len(calls) == 4


def test_failed_check_on_old_head_without_active_run_is_stuck(monkeypatch, capsys):
	runs = [
		{"name": "lint", "status": "completed", "conclusion": "failure"},
		{"name": "superseded", "status": "completed", "conclusion": "cancelled"},
	]
	calls = _stub(monkeypatch, _stuck_responses(_pr(mergeable_state="unstable"), OLD, check_runs=runs))
	_, out = _run(["--pr", "7"], capsys)
	assert out["done"] is True and "lint" in out["reason"] and "superseded" not in out["reason"]
	assert len(calls) <= 5


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
		f"repos/o/r/actions/runs?branch={FIXER_REF}&status=pending&per_page=1"]


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
	{"head_sha": "b" * 40},
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
