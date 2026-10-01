"""Contract for the CLAUDE.md §26 hand-back verdict
(`.claude/scripts/check_in_status.py --hand-back`) and the claim marker
writer (`.claude/scripts/claude_fix_claim.py`)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / ".claude" / "scripts"
TEMPLATE_SCRIPTS = ROOT / "workflow-templates" / ".claude" / "scripts"


def _load(name):
	spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


checker = _load("check_in_status")
claimer = _load("claude_fix_claim")

REPO = "o/r"
NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.timezone.utc)
HEAD = "b" * 40
OTHER_HEAD = "c" * 40
REF = "claude/fix-something"
PLAN_REF = "claude/implement-plan-demo-phase-1"
RUN_ID = 42
RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN_ID}"
PR_AUTHOR = "pr-author"


def _pr(ref=REF, **overrides):
	pr = {
		"merged": False,
		"state": "open",
		"labels": [],
		"mergeable_state": "clean",
		"updated_at": "2026-09-26T08:00:00Z",
		"head": {"sha": HEAD, "ref": ref},
		"user": {"login": PR_AUTHOR, "type": "User"},
	}
	pr.update(overrides)
	return pr


DISPATCH_RUNS = "repos/o/r/actions/workflows/internal-review.yml/runs?event=workflow_dispatch&per_page=100"


def _runs(ref=REF, queued=0, in_progress=0, pending=0, dispatched=()):
	return {
		f"repos/o/r/actions/runs?branch={ref}&status=queued&per_page=1": {"total_count": queued},
		f"repos/o/r/actions/runs?branch={ref}&status=in_progress&per_page=1": {"total_count": in_progress},
		f"repos/o/r/actions/runs?branch={ref}&status=pending&per_page=1": {"total_count": pending},
		DISPATCH_RUNS: {"workflow_runs": list(dispatched)},
	}


def _check_runs(*runs):
	return {f"repos/o/r/commits/{HEAD}/check-runs?per_page=100&page=1": {"check_runs": list(runs)}}


def _failed(name="lint", completed_at="2026-09-26T09:00:00Z"):
	return {"name": name, "status": "completed", "conclusion": "failure", "completed_at": completed_at}


def _commit(date):
	return {f"repos/o/r/commits/{HEAD}": {"commit": {"committer": {"date": date}}}}


def _claim(head=HEAD, kind="ci", by="session_01x", created_at="2026-09-26T11:00:00Z", association="OWNER", comment_id=None,
	login=PR_AUTHOR):
	body = claimer.claim_body(head, kind, by)
	comment = {"body": body, "author_association": association, "created_at": created_at, "user": {"login": login, "type": "User"}}
	if comment_id is not None:
		comment["id"] = comment_id
	return comment


def _handoff(kind="findings", created_at="2026-09-26T09:30:00Z", head=HEAD):
	if kind == "findings":
		intro = ("## Review round 1: findings handed to the Claude session\n\n"
			f"Reviewed head: `{head}` ([workflow run]({RUN_URL})).")
		ledger = f"\n<!-- ai:claude-fixer-handoff:v2 head={head} round=1 ledger={'a' * 64} -->"
	else:
		intro = ("## Review round 1: merge conflict, handed to the Claude session\n\n"
			f"The head `{head}` conflicts with its base branch, so the reviewer panel did not run ([workflow run]({RUN_URL})).")
		ledger = ""
	body = f"{intro}\n<!-- ai:claude-fixer-handoff:v1 kind={kind} head={head} round=1 -->{ledger}"
	return {"body": body, "author_association": "OWNER", "created_at": created_at, "user": {"login": "workflow-bot", "type": "User"}}


def _review_run(ref=REF, **overrides):
	run = {
		"id": RUN_ID, "html_url": RUN_URL, "repository": {"full_name": REPO},
		"path": ".github/workflows/ai-review.yml@stable", "head_sha": HEAD, "head_branch": ref,
		"status": "completed", "conclusion": "success",
	}
	run.update(overrides)
	return {f"repos/o/r/actions/runs/{RUN_ID}": run}


def _compare(run_head, status):
	return {f"repos/o/r/compare/{run_head}...{HEAD}?per_page=1": {"status": status}}


def _stub(monkeypatch, responses, comments=()):
	calls = []
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "workflow-bot")
	monkeypatch.delenv("CLAUDE_FIX_CLAIM_LEASE_HOURS", raising=False)
	monkeypatch.delenv("CLAUDE_FIX_HAND_BACK_CAP", raising=False)
	listed = [dict(comment) for comment in comments]
	for index, comment in enumerate(listed, 1):
		comment.setdefault("id", index)

	def fake(path):
		calls.append(path)
		if path not in responses:
			raise AssertionError(f"unexpected gh api call: {path}")
		return responses[path]

	def fake_list(path):
		calls.append(path)
		assert path == "repos/o/r/issues/7/comments"
		return listed

	monkeypatch.setattr(checker, "gh_api", fake)
	monkeypatch.setattr(checker, "gh_api_list", fake_list)
	return calls


def _run(capsys, *extra):
	code = checker.main(["--repo", REPO, "--pr", "7", "--hand-back", *extra], now=NOW)
	return code, json.loads(capsys.readouterr().out)


def test_merged_is_terminal_with_one_call(monkeypatch, capsys):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/7": _pr(merged=True, merged_at="t", merge_commit_sha="m")})
	code, out = _run(capsys)
	assert code == 0 and out["done"] is True and out["state"] == "merged"
	assert calls == ["repos/o/r/pulls/7"]


def test_non_claude_head_only_hands_back_terminal(monkeypatch, capsys):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/7": _pr(ref="ai/issue-9", mergeable_state="dirty")})
	_, out = _run(capsys)
	assert out["done"] is False and out["state"] == "open"
	assert calls == ["repos/o/r/pulls/7"]


def test_clean_claude_pr_is_open(monkeypatch, capsys):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/7": _pr(), **_check_runs()})
	_, out = _run(capsys)
	assert out["done"] is False and out["state"] == "open"
	assert out["claim"] == {"state": "none"} and out["hand_backs"] == 0 and out["cap"] == 3
	assert calls == ["repos/o/r/pulls/7", "repos/o/r/issues/7/comments", f"repos/o/r/commits/{HEAD}/check-runs?per_page=100&page=1"]


def test_failed_check_with_nothing_running_is_due_without_an_age_window(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(), **_check_runs(_failed(completed_at="2026-09-26T11:50:00Z")), **_runs()})
	_, out = _run(capsys)
	assert out["done"] is True and out["state"] == "ci-failed" and out["kind"] == "ci"
	assert out["since"] == "2026-09-26T11:50:00Z" and out["head_sha"] == HEAD


def test_failed_check_waits_while_a_run_is_pending(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(), **_check_runs(_failed()), **_runs(pending=1)})
	_, out = _run(capsys)
	assert out["done"] is False and "pending" in out["reason"]


def test_implement_plan_head_keeps_its_stuck_window(monkeypatch, capsys):
	responses = {"repos/o/r/pulls/7": _pr(ref=PLAN_REF), **_check_runs(_failed()), **_commit("2026-09-26T09:00:00Z")}
	_stub(monkeypatch, responses)
	_, out = _run(capsys)
	assert out["done"] is False and "waits 6h" in out["reason"]


def test_implement_plan_head_is_due_after_its_stuck_window(monkeypatch, capsys):
	responses = {"repos/o/r/pulls/7": _pr(ref=PLAN_REF), **_check_runs(_failed()), **_commit("2026-09-26T01:00:00Z"), **_runs(ref=PLAN_REF)}
	_stub(monkeypatch, responses)
	_, out = _run(capsys)
	assert out["done"] is True and out["state"] == "ci-failed"


def test_block_label_is_due(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])})
	_, out = _run(capsys)
	assert out["done"] is True and out["state"] == "blocked" and out["kind"] == "blocked"
	assert out["since"] == "2026-09-26T08:00:00Z"


def test_review_handoff_is_due_with_the_comment_time(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(), **_review_run(), **_runs()}, [_handoff()])
	_, out = _run(capsys)
	assert out["done"] is True and out["state"] == "review-round" and out["kind"] == "review"
	assert out["since"] == "2026-09-26T09:30:00Z"


def test_review_handoff_on_the_run_head_needs_no_compare_read(monkeypatch, capsys):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/7": _pr(), **_review_run(), **_runs()}, [_handoff()])
	_, out = _run(capsys)
	assert out["state"] == "review-round"
	assert not any("/compare/" in call for call in calls)


def test_review_handoff_from_a_run_triggered_by_an_older_push_is_due(monkeypatch, capsys):
	# PR #4594: pushes a59fc87 then d1c6f92; run 36290049170 was triggered by
	# a59fc87 (its head_sha) but reviewed d1c6f92, the head its hand-off names.
	responses = {"repos/o/r/pulls/7": _pr(), **_review_run(head_sha=OTHER_HEAD), **_compare(OTHER_HEAD, "ahead"), **_runs()}
	calls = _stub(monkeypatch, responses, [_handoff()])
	code, out = _run(capsys)
	assert code == 0 and out["done"] is True and out["state"] == "review-round" and out["kind"] == "review"
	assert out["head_sha"] == HEAD and out["since"] == "2026-09-26T09:30:00Z"
	assert calls[:4] == ["repos/o/r/pulls/7", "repos/o/r/issues/7/comments", f"repos/o/r/actions/runs/{RUN_ID}",
		f"repos/o/r/compare/{OTHER_HEAD}...{HEAD}?per_page=1"]


def test_conflict_handoff_from_a_run_triggered_by_an_older_push_is_due(monkeypatch, capsys):
	responses = {"repos/o/r/pulls/7": _pr(), **_review_run(head_sha=OTHER_HEAD), **_compare(OTHER_HEAD, "ahead"), **_runs()}
	_stub(monkeypatch, responses, [_handoff(kind="conflict")])
	_, out = _run(capsys)
	assert out["done"] is True and out["state"] == "conflict" and out["kind"] == "conflict"


@pytest.mark.parametrize("run_change", [
	{"head_branch": "claude/other-branch"},
	{"status": "in_progress", "conclusion": None},
	{"status": "queued", "conclusion": None},
	{"conclusion": "failure"},
	{"conclusion": "cancelled"},
	{"path": ".github/workflows/unrelated.yml@main"},
	{"repository": {"full_name": "other/repo"}},
	{"head_sha": "not-a-sha"},
])
def test_review_handoff_with_an_unverified_run_fails_closed(monkeypatch, capsys, run_change):
	# The run is triggered by an older push, so only the other checks decide;
	# every one of them still fails closed before any compare read.
	responses = {"repos/o/r/pulls/7": _pr(), **_review_run(**{"head_sha": OTHER_HEAD, **run_change}),
		**_compare(OTHER_HEAD, "ahead"), **_runs()}
	calls = _stub(monkeypatch, responses, [_handoff()])
	_, out = _run(capsys)
	assert out["done"] is False and out["state"] == "open" and "waiting for verified completed review run" in out["reason"]
	assert calls[-1] == f"repos/o/r/actions/runs/{RUN_ID}"


@pytest.mark.parametrize("status", ["diverged", "behind"])
def test_review_handoff_from_a_run_off_the_head_history_fails_closed(monkeypatch, capsys, status):
	responses = {"repos/o/r/pulls/7": _pr(), **_review_run(head_sha=OTHER_HEAD), **_compare(OTHER_HEAD, status), **_runs()}
	calls = _stub(monkeypatch, responses, [_handoff()])
	_, out = _run(capsys)
	assert out["done"] is False and out["state"] == "open" and "waiting for verified completed review run" in out["reason"]
	assert calls[-1] == f"repos/o/r/compare/{OTHER_HEAD}...{HEAD}?per_page=1"


@pytest.mark.parametrize("reviewed_head", [None, "", "not-a-sha", HEAD.upper(), HEAD[:39]])
def test_run_head_helper_rejects_a_malformed_reviewed_head_without_a_read(monkeypatch, reviewed_head):
	monkeypatch.setattr(checker, "gh_api", lambda path: pytest.fail(f"unexpected read: {path}"))
	assert checker._review_run_head_on_branch_history(REPO, OTHER_HEAD, reviewed_head) is False


def test_review_handoff_for_a_head_that_is_not_the_pr_head_is_ignored(monkeypatch, capsys):
	# The hand-off reviewed OTHER_HEAD, but the PR has moved on to HEAD.
	responses = {"repos/o/r/pulls/7": _pr(), **_review_run(head_sha=OTHER_HEAD), **_check_runs()}
	calls = _stub(monkeypatch, responses, [_handoff(head=OTHER_HEAD)])
	_, out = _run(capsys)
	assert out["done"] is False and out["state"] == "open"
	assert f"repos/o/r/actions/runs/{RUN_ID}" not in calls and not any("/compare/" in call for call in calls)


def test_conflict_without_handoff_uses_the_head_commit_time(monkeypatch, capsys):
	responses = {"repos/o/r/pulls/7": _pr(mergeable_state="dirty"), **_runs(), **_commit("2026-09-25T12:00:00Z")}
	_stub(monkeypatch, responses)
	_, out = _run(capsys)
	assert out["done"] is True and out["state"] == "conflict" and out["since"] == "2026-09-25T12:00:00Z"


def test_live_claim_on_the_head_is_not_done(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, [_claim(created_at="2026-09-26T10:30:00Z")])
	_, out = _run(capsys)
	assert out["done"] is False and out["state"] == "claimed" and out["claim"]["state"] == "live"
	assert out["claim"]["by"] == "session_01x"


def test_expired_claim_lets_the_fix_through(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, [_claim(created_at="2026-09-26T08:30:00Z")])
	_, out = _run(capsys)
	assert out["done"] is True and out["state"] == "blocked" and out["claim"]["state"] == "expired"


def test_lease_hours_come_from_the_environment(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, [_claim(created_at="2026-09-26T08:30:00Z")])
	monkeypatch.setenv("CLAUDE_FIX_CLAIM_LEASE_HOURS", "5")
	_, out = _run(capsys)
	assert out["state"] == "claimed"


def test_claim_on_an_older_head_does_not_block(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, [_claim(head=OTHER_HEAD)])
	_, out = _run(capsys)
	assert out["done"] is True and out["claim"] == {"state": "none"} and out["hand_backs"] == 1


def test_hold_never_expires_on_the_same_head(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, [_claim(kind="hold", created_at="2026-09-01T00:00:00Z")])
	_, out = _run(capsys)
	assert out["done"] is False and out["state"] == "held"


def test_hold_polls_at_the_held_backoff(monkeypatch, capsys):
	monkeypatch.delenv("CLAUDE_CHECK_IN_HELD_RETRY_MINUTES", raising=False)
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr()}, [_claim(kind="hold")])
	_, out = _run(capsys)
	assert out["state"] == "held" and out["action"] == "wait" and out["retry_after_minutes"] == 180
	assert "cap" not in out["reason"]


def test_a_later_claim_lifts_a_hold(monkeypatch, capsys):
	comments = [_claim(kind="hold", comment_id=1), _claim(kind="blocked", created_at="2026-09-26T11:30:00Z", comment_id=2)]
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, comments)
	_, out = _run(capsys)
	assert out["state"] == "claimed"


def test_untrusted_claims_are_ignored(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, [_claim(association="NONE")])
	_, out = _run(capsys)
	assert out["done"] is True and out["claim"] == {"state": "none"} and out["hand_backs"] == 0


def test_a_collaborators_forged_hold_is_ignored(monkeypatch, capsys):
	# Issue #4622: a collaborator who is neither the PR's author nor the
	# workflow account cannot park the PR with a hold marker.
	forged = _claim(kind="hold", by="session_01x", association="COLLABORATOR", login="mallory", created_at="2026-09-01T00:00:00Z")
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, [forged])
	_, out = _run(capsys)
	assert out["done"] is True and out["state"] == "blocked" and out["claim"] == {"state": "none"}


def test_a_collaborators_forged_claims_do_not_reach_the_cap(monkeypatch, capsys):
	forged = [
		_claim(head=head, kind=kind, association="COLLABORATOR", login="mallory", comment_id=index)
		for index, (head, kind) in enumerate((("d" * 40, "ci"), ("e" * 40, "conflict"), ("f" * 40, "blocked"), (HEAD, "ci")), 1)
	]
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, forged)
	_, out = _run(capsys)
	assert out["done"] is True and out["claim"] == {"state": "none"}
	assert out["hand_backs"] == 0 and out["cap_reached"] is False


def test_the_workflow_accounts_sweep_reservation_counts(monkeypatch, capsys):
	reservation = _claim(kind="blocked", by="sweep-run-77", login="workflow-bot")
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, [reservation])
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "workflow-bot")
	_, out = _run(capsys)
	assert out["state"] == "claimed" and out["claim"]["by"] == "sweep-run-77" and out["hand_backs"] == 1


def test_claim_logins_match_case_insensitively(monkeypatch, capsys):
	comments = [_claim(kind="hold", login="PR-Author", comment_id=1)]
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, comments)
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "Workflow-Bot")
	_, out = _run(capsys)
	assert out["state"] == "held"
	assert checker._fix_claim_trusted_logins({"user": {"login": "PR-Author"}}) == ("pr-author", "workflow-bot")


def test_without_a_pr_author_only_the_workflow_account_counts(monkeypatch, capsys):
	comments = [_claim(kind="hold", comment_id=1), _claim(kind="ci", by="sweep-run-5", login="workflow-bot", comment_id=2)]
	pr = _pr(labels=[{"name": "ai:review-blocked"}])
	del pr["user"]
	_stub(monkeypatch, {"repos/o/r/pulls/7": pr}, comments)
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "workflow-bot")
	_, out = _run(capsys)
	assert out["state"] == "claimed" and out["claim"]["by"] == "sweep-run-5"


def test_trusted_logins_tolerate_a_non_object_pr(monkeypatch):
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "Workflow-Bot")
	for pr in (None, [], "PR-Author"):
		assert checker._fix_claim_trusted_logins(pr) == ("workflow-bot",)
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "")
	assert checker._fix_claim_trusted_logins(None) == ()


def test_no_trusted_login_counts_no_claim(monkeypatch, capsys):
	pr = _pr(labels=[{"name": "ai:review-blocked"}])
	pr["user"] = None
	_stub(monkeypatch, {"repos/o/r/pulls/7": pr}, [_claim(kind="hold"), _claim(kind="ci", login="workflow-bot")])
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "")
	_, out = _run(capsys)
	assert out["done"] is True and out["claim"] == {"state": "none"} and out["hand_backs"] == 0
	assert checker.read_fix_claims([_claim(kind="hold")], HEAD, NOW)["claim"] == {"state": "none"}


def test_hand_backs_count_distinct_head_and_kind(monkeypatch, capsys):
	comments = [
		_claim(head=OTHER_HEAD, kind="ci", by="sweep-run-1", comment_id=1),
		_claim(head=OTHER_HEAD, kind="ci", by="session_01x", comment_id=2),
		_claim(head="d" * 40, kind="conflict", comment_id=3),
		_claim(head="e" * 40, kind="review", comment_id=4),
		_claim(head="f" * 40, kind="blocked", comment_id=5),
	]
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, comments)
	_, out = _run(capsys)
	assert out["hand_backs"] == 3 and out["cap_reached"] is True


def test_min_age_keeps_a_young_fix_waiting(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(), **_check_runs(_failed(completed_at="2026-09-26T11:00:00Z")), **_runs()})
	_, out = _run(capsys, "--min-age-hours", "2")
	assert out["done"] is False and out["state"] == "open" and out["due_state"] == "ci-failed"


def test_min_age_passes_an_old_fix(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(), **_check_runs(_failed(completed_at="2026-09-26T09:00:00Z")), **_runs()})
	_, out = _run(capsys, "--min-age-hours", "2")
	assert out["done"] is True and out["state"] == "ci-failed"


def test_hand_back_needs_a_pr(capsys):
	code = checker.main(["--repo", REPO, "--run", "5", "--hand-back"], now=NOW)
	assert code == 2 and "--hand-back needs --pr" in json.loads(capsys.readouterr().out)["error"]


def test_hand_back_and_terminal_only_are_exclusive():
	with pytest.raises(SystemExit):
		checker.build_parser().parse_args(["--repo", REPO, "--pr", "7", "--hand-back", "--terminal-only"])


def test_claim_body_marker_matches_the_reader():
	for kind in claimer.CLAIM_KINDS:
		body = claimer.claim_body(HEAD, kind, "sweep-run-12")
		markers = [line for line in body.splitlines() if checker.FIX_CLAIM_RE.fullmatch(line)]
		assert markers == [f"<!-- ai:claude-fix-claim:v1 head={HEAD} kind={kind} by=sweep-run-12 -->"]


@pytest.mark.parametrize("head, kind, by", [("abc", "ci", "s"), (HEAD, "bogus", "s"), (HEAD, "ci", "bad by"), (HEAD, "ci", "")])
def test_claim_body_rejects_bad_input(head, kind, by):
	with pytest.raises(ValueError):
		claimer.claim_body(head, kind, by)


def test_hold_without_reason_and_reason_cap_keep_the_cap_sentence(monkeypatch):
	monkeypatch.delenv("CLAUDE_FIX_HAND_BACK_CAP", raising=False)
	legacy = claimer.claim_body(HEAD, "hold", "session_01x")
	assert "reached the cap of 3 Claude hand-backs" in legacy
	assert claimer.claim_body(HEAD, "hold", "session_01x", "cap") == legacy


def test_hold_reason_replaces_the_cap_sentence_and_keeps_the_marker():
	body = claimer.claim_body(HEAD, "hold", "session_01x", "all findings rejected; no verdict path")
	assert body.startswith(f"**Claude fixes on hold:** all findings rejected; no verdict path at head `{HEAD[:12]}`.")
	assert "cap of" not in body
	markers = [line for line in body.splitlines() if checker.FIX_CLAIM_RE.fullmatch(line)]
	assert markers == [f"<!-- ai:claude-fix-claim:v1 head={HEAD} kind=hold by=session_01x -->"]


def test_hold_reason_is_one_clean_line_of_at_most_300_chars():
	reason = "line one\n`code` <!-- ai:claude-fix-claim:v1 head=" + HEAD + " kind=hold by=x --> " + "z" * 400
	body = claimer.claim_body(HEAD, "hold", "session_01x", reason)
	first = body.splitlines()[0]
	cleaned = first[len("**Claude fixes on hold:** "):first.index(f" at head `{HEAD[:12]}`")]
	assert "`" not in cleaned and "<!--" not in cleaned and len(cleaned) <= 300
	assert cleaned.startswith("line one code ")
	assert len([line for line in body.splitlines() if checker.FIX_CLAIM_RE.fullmatch(line)]) == 1


def _load_template(name):
	# The twin is edited first (CLAUDE.md §28.C twin-first); template parity keeps the two equal.
	spec = importlib.util.spec_from_file_location(f"{name}_template", TEMPLATE_SCRIPTS / f"{name}.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


@pytest.mark.parametrize("reason", ["x <!<!---- y", "x <!<!<!------ y", "<!-<!---- z"])
def test_hold_reason_strips_nested_comment_openers(reason):
	cleaned = _load_template("claude_fix_claim").clean_hold_reason(reason)
	assert "<!--" not in cleaned


def test_held_retry_minutes_survive_an_infinite_value(monkeypatch):
	template_checker = _load_template("check_in_status")
	for raw in ("inf", "-inf", "1e999"):
		monkeypatch.setenv("CLAUDE_CHECK_IN_HELD_RETRY_MINUTES", raw)
		assert template_checker.retry_after_minutes({"state": "held"}) == 180


@pytest.mark.parametrize("kind", ["conflict", "ci", "review", "blocked"])
def test_reason_with_a_non_hold_kind_is_rejected(kind, capsys):
	with pytest.raises(ValueError):
		claimer.claim_body(HEAD, kind, "session_01x", "because")
	code = claimer.main(["body", "--head", HEAD, "--kind", kind, "--by", "session_01x", "--reason", "because"])
	assert code == 1 and "only with --kind hold" in json.loads(capsys.readouterr().out)["error"]


def test_blank_reason_is_rejected():
	with pytest.raises(ValueError):
		claimer.claim_body(HEAD, "hold", "session_01x", " `<!-- ")


def test_post_passes_the_reason(monkeypatch, capsys):
	monkeypatch.setattr(claimer.check_in_status, "gh_api", lambda path: {"state": "open", "head": {"sha": HEAD}})
	posted = {}

	def fake_gh(args):
		posted["body"] = json.loads(Path(args[-1]).read_text())["body"]
		return '{"id": 5}'

	monkeypatch.setattr(claimer, "_gh", fake_gh)
	code = claimer.main(["post", "--repo", REPO, "--pr", "7", "--head", HEAD, "--kind", "hold", "--by", "session_01x",
		"--reason", "conflict needs a side decision"])
	assert code == 0 and posted["body"].startswith("**Claude fixes on hold:** conflict needs a side decision at head")


def test_post_refuses_a_moved_head(monkeypatch, capsys):
	monkeypatch.setattr(claimer.check_in_status, "gh_api", lambda path: {"state": "open", "head": {"sha": OTHER_HEAD}})
	monkeypatch.setattr(claimer, "_gh", lambda args: pytest.fail("must not post"))
	code = claimer.main(["post", "--repo", REPO, "--pr", "7", "--head", HEAD, "--kind", "ci", "--by", "session_01x"])
	out = json.loads(capsys.readouterr().out)
	assert code == 1 and out["posted"] is False and "head moved" in out["reason"]


def test_post_refuses_a_closed_pr(monkeypatch, capsys):
	monkeypatch.setattr(claimer.check_in_status, "gh_api", lambda path: {"state": "closed", "head": {"sha": HEAD}})
	monkeypatch.setattr(claimer, "_gh", lambda args: pytest.fail("must not post"))
	code = claimer.main(["post", "--repo", REPO, "--pr", "7", "--head", HEAD, "--kind", "ci", "--by", "session_01x"])
	assert code == 1 and json.loads(capsys.readouterr().out)["posted"] is False


def test_post_posts_one_comment(monkeypatch, capsys):
	posted = []
	monkeypatch.setattr(claimer.check_in_status, "gh_api", lambda path: {"state": "open", "head": {"sha": HEAD}})

	def fake_gh(args):
		posted.append(args[:4])
		body = json.loads(Path(args[-1]).read_text())["body"]
		assert f"head={HEAD} kind=conflict by=session_01x" in body
		return json.dumps({"id": 99})

	monkeypatch.setattr(claimer, "_gh", fake_gh)
	code = claimer.main(["post", "--repo", REPO, "--pr", "7", "--head", HEAD, "--kind", "conflict", "--by", "session_01x"])
	out = json.loads(capsys.readouterr().out)
	assert code == 0 and out == {"posted": True, "comment_id": 99, "reason": f"claimed PR #7 head {HEAD[:12]} for conflict as session_01x"}
	assert posted == [["api", "-X", "POST", "repos/o/r/issues/7/comments"]]


def test_post_reports_a_read_failure(monkeypatch, capsys):
	def boom(path):
		raise claimer.check_in_status.ReadError("HTTP 502")

	monkeypatch.setattr(claimer.check_in_status, "gh_api", boom)
	code = claimer.main(["post", "--repo", REPO, "--pr", "7", "--head", HEAD, "--kind", "ci", "--by", "session_01x"])
	assert code == 2 and "HTTP 502" in json.loads(capsys.readouterr().out)["error"]


def test_template_copies_match():
	for name in ("check_in_status.py", "claude_fix_claim.py"):
		assert (SCRIPTS / name).read_bytes() == (TEMPLATE_SCRIPTS / name).read_bytes(), name


def test_a_fixer_looks_past_its_own_and_its_sweep_reservation(monkeypatch, capsys):
	comments = [_claim(kind="ci", by="sweep-run-77", comment_id=1), _claim(kind="ci", by="session_01me", comment_id=2)]
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, comments)
	_, out = _run(capsys, "--ignore-claim-by", "session_01me", "--ignore-claim-by", "sweep-run-77")
	assert out["done"] is True and out["state"] == "blocked" and out["claim"] == {"state": "none"}
	assert out["hand_backs"] == 1


def test_ignoring_one_claimant_still_respects_another(monkeypatch, capsys):
	comments = [_claim(kind="ci", by="session_01other", comment_id=1), _claim(kind="ci", by="sweep-run-77", comment_id=2)]
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, comments)
	_, out = _run(capsys, "--ignore-claim-by", "sweep-run-77")
	assert out["state"] == "claimed" and out["claim"]["by"] == "session_01other"


# Issue #4618: review_autofix_sweep.yml dispatches internal-review.yml from the
# default branch, so the run's head_branch is `main` and its head_sha a main
# commit. The exact run title binds it to the PR instead.
DEFAULT_BASE = {"repo": {"default_branch": "main"}}
DISPATCH_TITLE = "Internal: AI Review & Autofix [pr:7]"


def _dispatched_run(**overrides):
	run = {"path": ".github/workflows/internal-review.yml", "event": "workflow_dispatch",
		"head_branch": "main", "head_sha": OTHER_HEAD, "display_title": DISPATCH_TITLE}
	run.update(overrides)
	return _review_run(**run)


def test_review_handoff_from_a_sweep_dispatch_on_the_default_branch_is_due(monkeypatch, capsys):
	responses = {"repos/o/r/pulls/7": _pr(base=DEFAULT_BASE), **_dispatched_run(), **_runs()}
	calls = _stub(monkeypatch, responses, [_handoff()])
	code, out = _run(capsys)
	assert code == 0 and out["done"] is True and out["state"] == "review-round"
	assert not any("/compare/" in call for call in calls)


def test_conflict_handoff_from_a_sweep_dispatch_on_the_default_branch_is_due(monkeypatch, capsys):
	responses = {"repos/o/r/pulls/7": _pr(base=DEFAULT_BASE, mergeable_state="dirty"), **_dispatched_run(), **_runs()}
	_stub(monkeypatch, responses, [_handoff(kind="conflict")])
	_, out = _run(capsys)
	assert out["done"] is True and out["state"] == "conflict"


@pytest.mark.parametrize("pr_base, run_change", [
	(DEFAULT_BASE, {"display_title": "Internal: AI Review & Autofix [pr:8]"}),
	(DEFAULT_BASE, {"display_title": "Internal: AI Review & Autofix [pr:7] "}),
	(DEFAULT_BASE, {"head_branch": "claude/other-branch"}),
	(DEFAULT_BASE, {"event": "pull_request"}),
	(DEFAULT_BASE, {"path": ".github/workflows/review_autofix.yml"}),
	(DEFAULT_BASE, {"conclusion": "failure"}),
	(None, {}),
	({"repo": {"default_branch": ""}}, {}),
])
def test_sweep_dispatch_that_does_not_bind_to_the_pr_fails_closed(monkeypatch, capsys, pr_base, run_change):
	pr = _pr(base=pr_base) if pr_base is not None else _pr()
	responses = {"repos/o/r/pulls/7": pr, **_dispatched_run(**run_change), **_compare(OTHER_HEAD, "diverged"), **_runs()}
	_stub(monkeypatch, responses, [_handoff()])
	_, out = _run(capsys)
	assert out["done"] is False and out["state"] == "open" and "waiting for verified completed review run" in out["reason"]


@pytest.mark.parametrize("status", ["queued", "in_progress", "pending"])
def test_active_sweep_dispatch_for_the_pr_keeps_the_handoff_waiting(monkeypatch, capsys, status):
	active = [{"status": status, "display_title": DISPATCH_TITLE}]
	responses = {"repos/o/r/pulls/7": _pr(), **_review_run(), **_runs(dispatched=active)}
	_stub(monkeypatch, responses, [_handoff()])
	_, out = _run(capsys)
	assert out["done"] is False and out["state"] == "open" and "still queued or running" in out["reason"]


def test_sweep_dispatch_for_another_pr_or_finished_does_not_count(monkeypatch, capsys):
	other = [{"status": "in_progress", "display_title": "Internal: AI Review & Autofix [pr:8]"},
		{"status": "completed", "display_title": DISPATCH_TITLE}]
	responses = {"repos/o/r/pulls/7": _pr(), **_review_run(), **_runs(dispatched=other)}
	_stub(monkeypatch, responses, [_handoff()])
	_, out = _run(capsys)
	assert out["done"] is True and out["state"] == "review-round"


def test_dispatch_listing_404_counts_zero_and_other_errors_raise(monkeypatch):
	def missing(path):
		if "internal-review.yml" in path:
			raise checker.ReadError(f"gh api {path} failed: gh: Not Found (HTTP 404)")
		return {"total_count": 0}
	monkeypatch.setattr(checker, "gh_api", missing)
	assert checker._active_run_count(REPO, REF, include_pending=True, pr_number=7) == 0

	def broken(path):
		if "internal-review.yml" in path:
			raise checker.ReadError(f"gh api {path} failed: HTTP 502")
		return {"total_count": 0}
	monkeypatch.setattr(checker, "gh_api", broken)
	with pytest.raises(checker.ReadError):
		checker._active_run_count(REPO, REF, include_pending=True, pr_number=7)


def test_branch_active_run_skips_the_dispatch_listing(monkeypatch):
	calls = []

	def fake(path):
		calls.append(path)
		return {"total_count": 1}
	monkeypatch.setattr(checker, "gh_api", fake)
	assert checker._active_run_count(REPO, REF, include_pending=True, pr_number=7) == 3
	assert not any("internal-review.yml" in call for call in calls)


# --- Routing: `action` for the CLAUDE.md §26 checker ---------------------------
# The §26 checker branches on `action` only: `wait`, `hand_back_fixer` (a due
# Claude fix goes to the fixer subscriber), `hand_back_all` (terminal, every
# subscriber), or `retry` (exit 2).

def test_merged_hands_back_to_all(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(merged=True, merged_at="t", merge_commit_sha="m")})
	_, out = _run(capsys)
	assert out["action"] == "hand_back_all" and "next_stage" not in out


def test_closed_hands_back_to_all(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(state="closed", closed_at="t")})
	_, out = _run(capsys)
	assert out["state"] == "closed" and out["action"] == "hand_back_all"


def test_ci_failed_hands_back_to_the_fixer(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(), **_check_runs(_failed()), **_runs()})
	_, out = _run(capsys)
	assert out["state"] == "ci-failed" and out["action"] == "hand_back_fixer"
	# Additive: every existing field is still present.
	for key in ("done", "state", "reason", "kind", "head_sha", "claim", "hand_backs", "cap", "cap_reached", "since"):
		assert key in out, key


def test_block_label_hands_back_to_the_fixer(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:needs-human"}])})
	_, out = _run(capsys)
	assert out["state"] == "blocked" and out["action"] == "hand_back_fixer"


def test_review_handoff_hands_back_to_the_fixer(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(), **_review_run(), **_runs()}, [_handoff()])
	_, out = _run(capsys)
	assert out["state"] == "review-round" and out["action"] == "hand_back_fixer"


def test_conflict_hands_back_to_the_fixer(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(mergeable_state="dirty"), **_runs(), **_commit("2026-09-25T12:00:00Z")})
	_, out = _run(capsys)
	assert out["state"] == "conflict" and out["action"] == "hand_back_fixer"


@pytest.mark.parametrize("pr, comments, state", [
	(_pr(), (), "open"),
	(_pr(labels=[{"name": "ai:review-blocked"}]), (_claim(created_at="2026-09-26T10:30:00Z"),), "claimed"),
	(_pr(labels=[{"name": "ai:review-blocked"}]), (_claim(kind="hold", created_at="2026-09-01T00:00:00Z"),), "held"),
])
def test_open_claimed_and_held_wait(monkeypatch, capsys, pr, comments, state):
	_stub(monkeypatch, {"repos/o/r/pulls/7": pr, **_check_runs()}, comments)
	_, out = _run(capsys)
	assert out["done"] is False and out["state"] == state and out["action"] == "wait"


def test_non_claude_head_waits(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(ref="ai/issue-9", mergeable_state="dirty")})
	_, out = _run(capsys)
	assert out["action"] == "wait"


def test_a_young_due_fix_under_min_age_waits(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(), **_check_runs(_failed(completed_at="2026-09-26T11:00:00Z")), **_runs()})
	_, out = _run(capsys, "--min-age-hours", "2")
	assert out["due_state"] == "ci-failed" and out["action"] == "wait"


def test_read_failure_is_retry(monkeypatch, capsys):
	def boom(path):
		raise checker.ReadError("HTTP 503")

	_stub(monkeypatch, {})
	monkeypatch.setattr(checker, "gh_api", boom)
	code, out = _run(capsys)
	assert code == 2 and out == {"done": False, "error": "HTTP 503", "action": "retry", "retry_after_minutes": 60}


def test_hand_back_without_pr_is_retry(capsys):
	code = checker.main(["--repo", REPO, "--run", "5", "--hand-back"], now=NOW)
	assert code == 2 and json.loads(capsys.readouterr().out)["action"] == "retry"


def test_direct_callers_get_the_verdict_unchanged(monkeypatch):
	# scripts/claude_pr_sweep.py calls check_pr_hand_back directly; routing is
	# added only by main(), so that caller's verdict shape does not change.
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])})
	verdict = checker.check_pr_hand_back(REPO, 7, checker.DEFAULT_STUCK_HOURS, 0.0, NOW)
	assert verdict["state"] == "blocked" and "action" not in verdict


def _flat_text(path):
	return " ".join(path.read_text(encoding="utf-8").split())


def test_claude_md_26c_routes_on_action():
	text = _flat_text(ROOT / "CLAUDE.md")
	section = text[text.index("### C) What each check-in does"):text.index("### D) What the pushing session does")]
	assert "**Route on `action` only, never on `state`**" in section
	for row in (
		"| `open`, `claimed`, `held`, or waiting on a run | `wait` | 2 |",
		"| read failed (exit 2) | `retry` | 3 |",
		"| `conflict`, `review-round`, `ci-failed`, `blocked` | `hand_back_fixer` | 4 |",
		"| `merged`, `closed` | `hand_back_all` | 4 |",
	):
		assert row in section, row
	assert "2. **`action` is `wait`**" in section
	assert "3. **`action` is `retry`**" in section
	assert "4. **`action` is `hand_back_fixer`**" in section
	assert "**`action` is `hand_back_all`** (terminal: `merged` / `closed`)" in section
	assert "**Due fix** (`state` is" not in section
	pushing = text[text.index("### D) What the pushing session does"):text.index("### E) Enforcement")]
	assert "Then, by `action` (§26.C step 1):" in pushing


def test_fix_claude_pr_routes_on_action():
	for path in (ROOT / ".claude" / "commands" / "fix-claude-pr.md",
		ROOT / "workflow-templates" / ".claude" / "commands" / "fix-claude-pr.md"):
		text = _flat_text(path)
		assert "2. **Route on `action`**" in text
		assert "- `hand_back_fixer` (`conflict`, `review-round`, `ci-failed`, `blocked`) → continue." in text
		assert "- `hand_back_all` (`merged` / `closed`) → nothing to fix." in text
		assert "- `retry` (exit 2) → the read failed" in text
		assert "2. **Route on `state`.**" not in text
	assert ((ROOT / ".claude" / "commands" / "fix-claude-pr.md").read_bytes()
		== (ROOT / "workflow-templates" / ".claude" / "commands" / "fix-claude-pr.md").read_bytes())
