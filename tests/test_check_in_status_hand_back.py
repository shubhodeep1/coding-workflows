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


def _pr(ref=REF, **overrides):
	pr = {
		"merged": False,
		"state": "open",
		"labels": [],
		"mergeable_state": "clean",
		"updated_at": "2026-09-26T08:00:00Z",
		"head": {"sha": HEAD, "ref": ref},
	}
	pr.update(overrides)
	return pr


def _runs(ref=REF, queued=0, in_progress=0, pending=0):
	return {
		f"repos/o/r/actions/runs?branch={ref}&status=queued&per_page=1": {"total_count": queued},
		f"repos/o/r/actions/runs?branch={ref}&status=in_progress&per_page=1": {"total_count": in_progress},
		f"repos/o/r/actions/runs?branch={ref}&status=pending&per_page=1": {"total_count": pending},
	}


def _check_runs(*runs):
	return {f"repos/o/r/commits/{HEAD}/check-runs?per_page=100&page=1": {"check_runs": list(runs)}}


def _failed(name="lint", completed_at="2026-09-26T09:00:00Z"):
	return {"name": name, "status": "completed", "conclusion": "failure", "completed_at": completed_at}


def _commit(date):
	return {f"repos/o/r/commits/{HEAD}": {"commit": {"committer": {"date": date}}}}


def _claim(head=HEAD, kind="ci", by="session_01x", created_at="2026-09-26T11:00:00Z", association="OWNER", comment_id=None):
	body = claimer.claim_body(head, kind, by)
	comment = {"body": body, "author_association": association, "created_at": created_at, "user": {"login": "u", "type": "User"}}
	if comment_id is not None:
		comment["id"] = comment_id
	return comment


def _handoff(kind="findings", created_at="2026-09-26T09:30:00Z"):
	if kind == "findings":
		intro = ("## Review round 1: findings handed to the Claude session\n\n"
			f"Reviewed head: `{HEAD}` ([workflow run]({RUN_URL})).")
		ledger = f"\n<!-- ai:claude-fixer-handoff:v2 head={HEAD} round=1 ledger={'a' * 64} -->"
	else:
		intro = ("## Review round 1: merge conflict, handed to the Claude session\n\n"
			f"The head `{HEAD}` conflicts with its base branch, so the reviewer panel did not run ([workflow run]({RUN_URL})).")
		ledger = ""
	body = f"{intro}\n<!-- ai:claude-fixer-handoff:v1 kind={kind} head={HEAD} round=1 -->{ledger}"
	return {"body": body, "author_association": "OWNER", "created_at": created_at, "user": {"login": "workflow-bot", "type": "User"}}


def _review_run(ref=REF):
	return {f"repos/o/r/actions/runs/{RUN_ID}": {
		"id": RUN_ID, "html_url": RUN_URL, "repository": {"full_name": REPO},
		"path": ".github/workflows/ai-review.yml@stable", "head_sha": HEAD, "head_branch": ref,
		"status": "completed", "conclusion": "success",
	}}


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


def test_a_later_claim_lifts_a_hold(monkeypatch, capsys):
	comments = [_claim(kind="hold", comment_id=1), _claim(kind="blocked", created_at="2026-09-26T11:30:00Z", comment_id=2)]
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, comments)
	_, out = _run(capsys)
	assert out["state"] == "claimed"


def test_untrusted_claims_are_ignored(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/7": _pr(labels=[{"name": "ai:review-blocked"}])}, [_claim(association="NONE")])
	_, out = _run(capsys)
	assert out["done"] is True and out["claim"] == {"state": "none"} and out["hand_backs"] == 0


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
