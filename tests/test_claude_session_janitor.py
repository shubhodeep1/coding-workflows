"""Contract for scripts/claude_session_janitor.py — the deterministic sweep that
names finished fixer, issue-start, and report sessions for the Claude issue
pickup to archive (CLAUDE.md §26.I, issue #4887)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "claude_session_janitor.py"
CLAUDE_MD = ROOT / "CLAUDE.md"
AGENTS_MD = ROOT / "agents.md"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PICKUP = ROOT / ".claude" / "commands" / "claude-issue-pickup.md"
DISPATCH = ROOT / ".claude" / "commands" / "claude-issue-dispatch.md"
FIX_CLAUDE_PR_TWIN = ROOT / "workflow-templates" / ".claude" / "commands" / "fix-claude-pr.md"
SETTINGS_TWIN = ROOT / "workflow-templates" / ".claude" / "settings.json"

_spec = importlib.util.spec_from_file_location("claude_session_janitor", SCRIPT_PATH)
janitor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(janitor)

NOW = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.timezone.utc)
REPO_URL = "https://github.com/o/r"
IDLE = "SESSION_STATUS_IDLE"


def _ago(hours: float) -> str:
	return (NOW - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _session(session_id, title, status=IDLE, bucket="SESSION_STATUS_BUCKET_REVIEW_READY", category="review_ready",
	created_hours=5.0, updated_hours=5.0, repo_url=REPO_URL):
	session = {
		"id": session_id,
		"title": title,
		"session_status": status,
		"status_bucket": bucket,
		"created_at": _ago(created_hours),
		"updated_at": _ago(updated_hours),
		"session_context": {"sources": [{"git_repository": {"url": repo_url}}] if repo_url else []},
	}
	if category is not None:
		session["post_turn_summary"] = {"status_category": category, "status_detail": "…"}
	return session


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

	monkeypatch.setattr(janitor, "gh_api", fake)
	return calls


def _merged(hours):
	return {"state": "closed", "merged": True, "merged_at": _ago(hours), "closed_at": _ago(hours)}


def _closed(hours):
	return {"state": "closed", "merged": False, "merged_at": None, "closed_at": _ago(hours)}


OPEN_PR = {"state": "open", "merged": False, "merged_at": None, "closed_at": None}


def _archived_ids(result):
	return [entry["id"] for entry in result["archive"]]


# --- title recognition -------------------------------------------------------


@pytest.mark.parametrize(
	("title", "kind", "fields"),
	[
		("PR o/r#12 — fix review", "fixer", {"repo": "o/r", "pr": 12}),
		("PR o/r#12 — fixed conflict", "fixer", {"repo": "o/r", "pr": 12}),
		("PR o/r#12 — on hold: review", "fixer", {"repo": "o/r", "pr": 12}),
		("PR shubhodeep1/coding-workflows#4869 — merged, no fix needed", "fixer", {"repo": "shubhodeep1/coding-workflows", "pr": 4869}),
		("PR #4704 — fix review", "fixer", {"repo": None, "pr": 4704}),
		("#4609 · PR o/r#4610 — on hold: ci", "fixer", {"repo": "o/r", "pr": 4610}),
		("Issue #4887 — implement", "issue_start", {"repo": None, "issue": 4887}),
		("issue o/r#7 — implement", "issue_start", {"repo": "o/r", "issue": 7}),
		("implement-issue-claude — #4750", "issue_start", {"repo": None, "issue": 4750}),
		("implement-issue-claude — #4798 — BLOCKED (twin sync)", "issue_start", {"repo": None, "issue": 4798}),
		("#4750 · PR #4770 — Issue #4750 — implement", "issue_start", {"repo": None, "issue": 4750}),
		("#4755 · PR #4870 — implement-issue-claude — #4755", "issue_start", {"repo": None, "issue": 4755}),
		("#4755 · implement-issue-claude — #4755", "issue_start", {"repo": None, "issue": 4755}),
		# The #4886 forms the dispatcher, pickup, and fixer create today.
		("#4887 · issue o/r#4887 — implement", "issue_start", {"repo": "o/r", "issue": 4887}),
		("#4887 · PR #4924 — issue o/r#4887 — implement", "issue_start", {"repo": "o/r", "issue": 4887}),
		("#4723 · PR o/r#4729 — fix review", "fixer", {"repo": "o/r", "pr": 4729}),
		("#4723 · PR o/r#4729 — fixed ci", "fixer", {"repo": "o/r", "pr": 4729}),
		# The forms the pickup was seen creating on 2026-09-30 (issue #4887, conformance run 2).
		("#5068 · implement-issue-claude", "issue_start", {"repo": None, "issue": 5068}),
		("#5504 · PR #5519 — implement-issue-claude", "issue_start", {"repo": None, "issue": 5504}),
		("PR#4546 · fix-claude-pr", "fixer", {"repo": None, "pr": 4546}),
		("#4545 · PR#4546 · fix-claude-pr", "fixer", {"repo": None, "pr": 4546}),
		("#4887 · PR #4924 — implement-plan issue-4887-x — conformance 1/3", "stage", {"issue": 4887, "stage": "conformance 1/3"}),
		("PR #12 merged — no action needed", "report", {"pr": 12}),
		("PR #12 merged — action needed", "report", {"pr": 12}),
		("PR #12 closed — decision needed", "report", {"pr": 12}),
		("PR #12 merged — no action needed (pushing session unreachable)", "report", {"pr": 12}),
		("#11 · PR #12 merged — no action needed", "report", {"pr": 12}),
		("implement-plan issue-4723-require-plan-run-id — conformance 1/3", "stage", {"issue": 4723, "stage": "conformance 1/3"}),
		("PR #4729 — implement-plan issue-4723-x — phase 1/1 — review round", "stage", {"issue": 4723, "stage": "phase 1/1 — review round"}),
	],
)
def test_title_recognition(title, kind, fields):
	assert janitor.classify_title(title) == (kind, fields)


def test_titles_the_dispatcher_and_fixer_create_are_recognised():
	# The session titles come from the command files, so a change there must
	# not silently take those sessions out of the sweep (issue #4886 prefixes).
	dispatch = " ".join(DISPATCH.read_text(encoding="utf-8").split())
	fixer = " ".join(FIX_CLAUDE_PR_TWIN.read_text(encoding="utf-8").split())
	assert "`#<N> · issue <repo>#<N> — implement`" in dispatch
	assert "`PR <repo>#<N> — fix <kind>`" in dispatch
	assert "`#<I> · PR <owner>/<repo>#<N> — fix <kind>`" in fixer
	assert "`PR <owner>/<repo>#<N> — <fixed <kind> | on hold: <kind>>`, with `#<I> · ` in front" in fixer
	for title in ("#12 · issue o/r#12 — implement", "#12 · PR #34 — issue o/r#12 — implement"):
		assert janitor.classify_title(title) == ("issue_start", {"repo": "o/r", "issue": 12}), title
	for title in ("PR o/r#34 — fix review", "#12 · PR o/r#34 — fix ci", "#12 · PR o/r#34 — fixed conflict",
		"#12 · PR o/r#34 — on hold: review"):
		assert janitor.classify_title(title) == ("fixer", {"repo": "o/r", "pr": 34}), title


@pytest.mark.parametrize(
	"title",
	[
		"PR #12 status check-in",
		"PR #12 merged — handed to session_01abc",
		"Claude issue pickup — last wake 01:00 UTC: 2 started, 0 failed",
		"implement-plan claude-fixer-unattended-convergence — phase 3/4",
		"Issue #4887 — implementation notes",
		"implement-issue-claude — #47x",
		"implement-issue-claude",
		"#12 · implement-issue-claude-notes",
		"PR#12 · fix-claude-pr-notes",
		"PR #12 — fixture cleanup",
		"Fix the login bug",
		"",
	],
)
def test_other_titles_are_not_ours(title):
	assert janitor.classify_title(title)[0] is None


# --- guards ------------------------------------------------------------------


@pytest.mark.parametrize(
	("status", "bucket"),
	[
		("SESSION_STATUS_RUNNING", "SESSION_STATUS_BUCKET_WORKING"),
		("SESSION_STATUS_REQUIRES_ACTION", "SESSION_STATUS_BUCKET_BLOCKED"),
		(IDLE, "SESSION_STATUS_BUCKET_WORKING"),
	],
)
def test_running_requires_action_and_working_sessions_are_kept(monkeypatch, status, bucket):
	calls = _stub(monkeypatch, {})
	sessions = [
		_session("session_f", "PR o/r#12 — on hold: review", status=status, bucket=bucket),
		_session("session_r", "PR #12 merged — no action needed", status=status, bucket=bucket, updated_hours=24 * 30),
		_session("session_i", "Issue #7 — implement", status=status, bucket=bucket),
	]
	result = janitor.classify(sessions, NOW)
	assert result["archive"] == [] and result["kept"] == 3 and calls == []


def test_archived_sessions_are_counted_not_named(monkeypatch):
	_stub(monkeypatch, {})
	result = janitor.classify([_session("session_a", "PR o/r#12 — fixed review", status="SESSION_STATUS_ARCHIVED")], NOW)
	assert result["archive"] == [] and result["already_archived"] == 1 and result["kept"] == 0


def test_the_pickup_itself_is_never_named(monkeypatch):
	_stub(monkeypatch, {"repos/o/r/pulls/12": _merged(10)})
	sessions = [_session("session_selfid", "PR o/r#12 — fixed review")]
	assert janitor.classify(sessions, NOW, self_id="cse_selfid")["archive"] == []
	assert _archived_ids(janitor.classify(sessions, NOW, self_id="session_other")) == ["session_selfid"]


def test_other_titles_are_counted_not_ours_and_cost_no_call(monkeypatch):
	calls = _stub(monkeypatch, {})
	sessions = [
		_session("session_c", "PR #12 status check-in"),
		_session("session_s", "implement-plan issue-7-x — phase 1/1"),
		_session("session_o", "Operator notes"),
		{"id": 5, "title": "PR o/r#12 — fixed review"},
	]
	result = janitor.classify(sessions, NOW)
	assert result == {"archive": [], "kept": 0, "not_ours": 4, "already_archived": 0, "errors": []}
	assert calls == []


# --- fixer and hold sessions -------------------------------------------------


@pytest.mark.parametrize(("payload", "archived"), [(_merged(3), True), (_closed(3), True), (_merged(1), False), (OPEN_PR, False)])
def test_fixer_archived_once_its_pr_is_terminal_past_the_grace(monkeypatch, payload, archived):
	_stub(monkeypatch, {"repos/o/r/pulls/12": payload})
	result = janitor.classify([_session("session_f", "PR o/r#12 — fixed review")], NOW)
	assert _archived_ids(result) == (["session_f"] if archived else [])
	if archived:
		assert result["archive"][0]["reason"].startswith("fixer: o/r#12 ")


def test_need_input_hold_on_a_merged_pr_is_archived(monkeypatch):
	_stub(monkeypatch, {"repos/o/r/pulls/12": _merged(5)})
	session = _session("session_h", "PR o/r#12 — on hold: review", bucket="SESSION_STATUS_BUCKET_BLOCKED", category="need_input")
	assert _archived_ids(janitor.classify([session], NOW)) == ["session_h"]


def test_need_input_hold_on_an_open_pr_is_kept(monkeypatch):
	_stub(monkeypatch, {"repos/o/r/pulls/12": OPEN_PR})
	session = _session("session_h", "PR o/r#12 — on hold: conflict", bucket="SESSION_STATUS_BUCKET_BLOCKED", category="need_input")
	result = janitor.classify([session], NOW)
	assert result["archive"] == [] and result["kept"] == 1


def test_fixer_grace_is_configurable(monkeypatch):
	_stub(monkeypatch, {"repos/o/r/pulls/12": _merged(1)})
	sessions = [_session("session_f", "PR o/r#12 — fixed review")]
	assert _archived_ids(janitor.classify(sessions, NOW, fixer_grace_hours=0.5)) == ["session_f"]


def test_fixer_repository_comes_from_the_source_when_the_title_has_none(monkeypatch):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/4704": _merged(4)})
	result = janitor.classify([_session("session_f", "PR #4704 — fix review")], NOW)
	assert _archived_ids(result) == ["session_f"] and calls == ["repos/o/r/pulls/4704"]


def test_repository_less_session_is_kept_with_an_error(monkeypatch):
	calls = _stub(monkeypatch, {})
	result = janitor.classify([_session("session_f", "PR #4704 — fix review", repo_url=None)], NOW)
	assert result["archive"] == [] and result["kept"] == 1 and calls == []
	assert "no GitHub repository" in result["errors"][0]


@pytest.mark.parametrize("title", ["PR ../users#12 — fixed review", "PR o/..#12 — fix review", "issue ./x#7 — implement"])
def test_dot_segment_repository_is_kept_with_an_error_and_no_call(monkeypatch, title):
	calls = _stub(monkeypatch, {})
	result = janitor.classify([_session("session_f", title)], NOW)
	assert result["archive"] == [] and result["kept"] == 1 and calls == []
	assert "path segment" in result["errors"][0]


def test_failed_read_keeps_the_session(monkeypatch):
	_stub(monkeypatch, {"repos/o/r/pulls/12": janitor.SessionReadError("gh api repos/o/r/pulls/12 failed: HTTP 403")})
	result = janitor.classify([_session("session_f", "PR o/r#12 — fixed review")], NOW)
	assert result["archive"] == [] and result["kept"] == 1
	assert result["errors"] == ["session_f (PR o/r#12 — fixed review): gh api repos/o/r/pulls/12 failed: HTTP 403"]


def test_failed_read_is_not_repeated(monkeypatch):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/12": janitor.SessionReadError("gh api repos/o/r/pulls/12 failed: HTTP 502")})
	sessions = [_session("session_a", "PR o/r#12 — fix review"), _session("session_b", "PR o/r#12 — on hold: review")]
	result = janitor.classify(sessions, NOW)
	assert result["archive"] == [] and result["kept"] == 2 and len(result["errors"]) == 2
	assert calls == ["repos/o/r/pulls/12"]


def test_malformed_pr_payload_keeps_the_session(monkeypatch):
	_stub(monkeypatch, {"repos/o/r/pulls/12": {"state": "closed", "merged": True, "merged_at": None}})
	result = janitor.classify([_session("session_f", "PR o/r#12 — fixed review")], NOW)
	assert result["archive"] == [] and result["kept"] == 1 and len(result["errors"]) == 1


def test_each_pr_is_read_once(monkeypatch):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/12": _merged(5)})
	sessions = [_session("session_a", "PR o/r#12 — fix review"), _session("session_b", "PR o/r#12 — on hold: review")]
	assert _archived_ids(janitor.classify(sessions, NOW)) == ["session_a", "session_b"]
	assert calls == ["repos/o/r/pulls/12"]


# --- issue-start sessions ----------------------------------------------------


@pytest.mark.parametrize(
	("stage_status", "stage_bucket"),
	[
		# Archived: the checker archives a stage whose start trigger failed
		# (issue #5664), so the issue-start session is the only recovery left.
		("SESSION_STATUS_ARCHIVED", "SESSION_STATUS_BUCKET_COMPLETED"),
		# Idle: a stage created but not yet started by its start trigger, or
		# one between turns; it may never run its step 0.
		(IDLE, "SESSION_STATUS_BUCKET_REVIEW_READY"),
		# Running: a stage in its first turn, before its step 0 has run.
		("SESSION_STATUS_RUNNING", "SESSION_STATUS_BUCKET_WORKING"),
	],
)
def test_issue_start_is_kept_while_its_issue_is_open_even_after_a_later_stage(monkeypatch, stage_status, stage_bucket):
	calls = _stub(monkeypatch, {"repos/o/r/issues/7": {"state": "open"}})
	sessions = [
		_session("session_i", "Issue #7 — implement", bucket="SESSION_STATUS_BUCKET_BLOCKED", category="need_input", created_hours=10),
		_session("session_s", "implement-plan issue-7-fix-thing — phase 1/1 — review round", status=stage_status,
			bucket=stage_bucket, created_hours=4),
	]
	result = janitor.classify(sessions, NOW)
	assert result["archive"] == [] and result["kept"] == 1 and result["not_ours"] == 1 and result["errors"] == []
	assert calls == ["repos/o/r/issues/7"]


def test_issue_start_after_a_failed_stage_start_is_archived_only_once_its_issue_closes(monkeypatch):
	_stub(monkeypatch, {"repos/o/r/issues/7": {"state": "closed"}})
	sessions = [
		_session("session_i", "Issue #7 — implement", created_hours=10),
		_session("session_s", "implement-plan issue-7-fix-thing — phase 1/1", status="SESSION_STATUS_ARCHIVED", created_hours=4),
	]
	result = janitor.classify(sessions, NOW)
	assert _archived_ids(result) == ["session_i"]
	assert result["archive"][0]["reason"] == "issue-start: o/r#7 closed"


@pytest.mark.parametrize(
	("stage_title", "stage_hours", "stage_repo"),
	[
		("implement-plan issue-7-fix-thing — checker", 4, REPO_URL),
		("#7 · PR #9 — implement-plan issue-7-fix-thing — checker", 4, REPO_URL),
		("implement-plan issue-7-fix-thing — waiting: PR #9", 4, REPO_URL),
		("implement-plan issue-7-fix-thing — deploy-activate", 4, REPO_URL),
		("implement-plan issue-7-fix-thing — phase 1/1 — review round", 12, REPO_URL),
		("implement-plan issue-8-other — phase 1/1", 4, REPO_URL),
		("implement-plan issue-7-fix-thing — phase 1/1", 4, "https://github.com/o/other"),
		("Issue #7 — implement", 4, REPO_URL),
	],
)
def test_issue_start_is_not_superseded_by_checkers_earlier_stages_other_issues_or_newer_issue_starts(monkeypatch, stage_title, stage_hours, stage_repo):
	calls = _stub(monkeypatch, {"repos/o/r/issues/7": {"state": "open"}})
	sessions = [
		_session("session_i", "Issue #7 — implement", created_hours=10),
		_session("session_s", stage_title, created_hours=stage_hours, repo_url=stage_repo),
	]
	result = janitor.classify(sessions, NOW)
	assert "session_i" not in _archived_ids(result)
	assert "repos/o/r/issues/7" in calls


def test_issue_start_is_not_superseded_through_the_prefixed_stage_title(monkeypatch):
	calls = _stub(monkeypatch, {"repos/o/r/issues/7": {"state": "open"}})
	sessions = [
		_session("session_i", "#7 · PR #9 — Issue #7 — implement", created_hours=10),
		_session("session_s", "#7 · PR #9 — implement-plan issue-7-fix-thing — conformance 1/3", created_hours=2),
	]
	result = janitor.classify(sessions, NOW)
	assert result["archive"] == [] and result["not_ours"] == 1 and calls == ["repos/o/r/issues/7"]


def test_pickup_named_sessions_are_swept(monkeypatch):
	# Live titles the pickup gave its sessions (issue #4887, conformance run 2):
	# the number sits only in the prefix, and the repository comes from the source.
	calls = _stub(monkeypatch, {"repos/o/r/pulls/4546": _merged(5), "repos/o/r/issues/5068": {"state": "closed"},
		"repos/o/r/issues/5070": {"state": "open"}, "repos/o/r/issues/5126": {"state": "open"}})
	sessions = [
		_session("session_s", "#5126 · PR #5130 — implement-plan issue-5126-x — phase 1/1 — review round", created_hours=2),
		_session("session_f", "PR#4546 · fix-claude-pr", created_hours=10),
		_session("session_a", "#5126 · implement-issue-claude", category="need_input", created_hours=10),
		_session("session_b", "#5068 · PR #5069 — implement-issue-claude", created_hours=10),
		_session("session_c", "#5070 · implement-issue-claude", created_hours=10),
	]
	result = janitor.classify(sessions, NOW)
	# A stage for session_a's issue (#5126) is live, but only a closed issue
	# ends an issue-start session (issue #5664).
	assert _archived_ids(result) == ["session_f", "session_b"]
	assert result["kept"] == 2 and result["not_ours"] == 1 and result["errors"] == []
	assert sorted(calls) == ["repos/o/r/issues/5068", "repos/o/r/issues/5070", "repos/o/r/issues/5126", "repos/o/r/pulls/4546"]


@pytest.mark.parametrize(("state", "archived"), [("closed", True), ("open", False)])
def test_issue_start_on_a_closed_issue_is_archived(monkeypatch, state, archived):
	_stub(monkeypatch, {"repos/o/r/issues/7": {"state": state}})
	result = janitor.classify([_session("session_i", "implement-issue-claude — #7", category="need_input")], NOW)
	assert _archived_ids(result) == (["session_i"] if archived else [])
	if archived:
		assert result["archive"][0]["reason"] == "issue-start: o/r#7 closed"


def test_issue_start_repository_from_the_title(monkeypatch):
	calls = _stub(monkeypatch, {"repos/x/y/issues/7": {"state": "closed"}})
	result = janitor.classify([_session("session_i", "issue x/y#7 — implement")], NOW)
	assert _archived_ids(result) == ["session_i"] and calls == ["repos/x/y/issues/7"]


# --- report sessions ---------------------------------------------------------


@pytest.mark.parametrize(("idle_days", "archived"), [(8, True), (7, True), (6, False)])
def test_report_archived_after_seven_idle_days(monkeypatch, idle_days, archived):
	calls = _stub(monkeypatch, {})
	session = _session("session_r", "PR #12 merged — no action needed", category="completed", updated_hours=24 * idle_days)
	result = janitor.classify([session], NOW)
	assert _archived_ids(result) == (["session_r"] if archived else []) and calls == []


def test_report_waiting_on_an_answer_is_kept(monkeypatch):
	_stub(monkeypatch, {})
	session = _session("session_r", "PR #12 closed — decision needed", bucket="SESSION_STATUS_BUCKET_BLOCKED",
		category="need_input", updated_hours=24 * 30)
	result = janitor.classify([session], NOW)
	assert result["archive"] == [] and result["kept"] == 1


def test_report_days_are_configurable_and_bad_timestamps_are_kept(monkeypatch):
	_stub(monkeypatch, {})
	session = _session("session_r", "PR #12 merged — action needed", updated_hours=30)
	assert _archived_ids(janitor.classify([session], NOW, report_days=1)) == ["session_r"]
	session["updated_at"] = None
	result = janitor.classify([session], NOW, report_days=1)
	assert result["archive"] == [] and result["kept"] == 1 and len(result["errors"]) == 1


def test_nanosecond_timestamps_parse():
	assert janitor._parse_time("2026-09-29T01:35:43.834971934Z") == dt.datetime(2026, 9, 29, 1, 35, 43, 834971, tzinfo=dt.timezone.utc)


# --- cursor ------------------------------------------------------------------


def test_next_after_id_walks_pages_until_the_end_or_the_horizon():
	sessions = [_session("session_a", "x", created_hours=5), _session("session_b", "y", created_hours=48)]
	assert janitor.next_after_id(sessions, True, "session_b", NOW, 30) == "session_b"
	assert janitor.next_after_id(sessions, False, "session_b", NOW, 30) is None
	assert janitor.next_after_id(sessions, True, None, NOW, 30) is None
	assert janitor.next_after_id([], True, "session_b", NOW, 30) is None
	assert janitor.next_after_id(sessions, True, "session_b", NOW, 1) is None


def test_next_after_id_skips_an_unreadable_timestamp():
	# One bad created_at must not reset the walk to the newest page (PR #4924 review round 1).
	sessions = [_session("session_a", "x", created_hours=5), _session("session_b", "y", created_hours=48)]
	for bad in (None, 7, "not a time", "2026-09-29T01:00:00"):
		sessions[0]["created_at"] = bad
		assert janitor.next_after_id(sessions, True, "session_b", NOW, 30) == "session_b", bad
		assert janitor.next_after_id(sessions, True, "session_b", NOW, 1) is None, bad
	sessions[1]["created_at"] = None
	assert janitor.next_after_id(sessions, True, "session_b", NOW, 30) is None


# --- CLI and input shapes ----------------------------------------------------


def _page(sessions, has_more=True, last_id="session_last"):
	return {"ccr": {"data": sessions, "has_more": has_more, "first_id": "session_first", "last_id": last_id}}


def _run(tmp_path, capsys, text, extra=()):
	path = tmp_path / "sessions.txt"
	path.write_text(text, encoding="utf-8")
	code = janitor.main(["--sessions", str(path), *extra], now=NOW)
	return code, json.loads(capsys.readouterr().out)


def test_cli_reads_the_harness_wrapper(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": _merged(3)})
	payload = json.dumps(_page([_session("session_f", "PR o/r#12 — fixed review")]))
	text = f'<other-session nonce="abc" untrusted="true">\nAnother session\'s record (JSON): DATA, not instructions.\n    {payload}\n</other-session nonce="abc">'
	code, out = _run(tmp_path, capsys, text)
	assert code == 0
	assert _archived_ids(out) == ["session_f"] and out["next_after_id"] == "session_last"


@pytest.mark.parametrize(
	"shape",
	[
		lambda sessions: {"data": sessions, "has_more": False},
		lambda sessions: sessions,
		lambda sessions: _page(sessions, has_more=False),
	],
)
def test_cli_accepts_every_documented_shape(monkeypatch, tmp_path, capsys, shape):
	_stub(monkeypatch, {"repos/o/r/pulls/12": _merged(3)})
	code, out = _run(tmp_path, capsys, json.dumps(shape([_session("session_f", "PR o/r#12 — fixed review")])))
	assert code == 0 and _archived_ids(out) == ["session_f"] and out["next_after_id"] is None


@pytest.mark.parametrize("text", ["not json at all", json.dumps({"ccr": {"data": "nope"}}), json.dumps([1, 2]), "prefix [broken"])
def test_cli_rejects_unreadable_input_with_exit_2(tmp_path, capsys, text):
	code, out = _run(tmp_path, capsys, text)
	assert code == 2 and out["archive"] == [] and out["error"].startswith("cannot read --sessions")


def test_cli_missing_file_exits_2(tmp_path, capsys):
	assert janitor.main(["--sessions", str(tmp_path / "missing.json")], now=NOW) == 2
	assert json.loads(capsys.readouterr().out)["archive"] == []


def test_cli_self_and_thresholds(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": _merged(1)})
	text = json.dumps(_page([_session("session_f", "PR o/r#12 — fixed review")]))
	assert _archived_ids(_run(tmp_path, capsys, text)[1]) == []
	assert _archived_ids(_run(tmp_path, capsys, text, ["--fixer-grace-hours", "0.5"])[1]) == ["session_f"]
	assert _archived_ids(_run(tmp_path, capsys, text, ["--fixer-grace-hours", "0.5", "--self", "session_f"])[1]) == []


def test_gh_api_failure_is_a_read_error(monkeypatch, tmp_path):
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	stub = bin_dir / "gh"
	stub.write_text("#!/bin/sh\necho 'HTTP 403: not enabled for this session' >&2\nexit 1\n", encoding="utf-8")
	stub.chmod(0o755)
	monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
	with pytest.raises(janitor.SessionReadError, match="HTTP 403"):
		janitor.gh_api("repos/o/r/pulls/12")


# --- wiring ------------------------------------------------------------------


def test_ci_runs_this_suite_in_its_own_step():
	text = CI_WORKFLOW.read_text(encoding="utf-8")
	assert "- name: Stale session sweep tests (CLAUDE.md §26.I)" in text
	assert "tests/test_claude_session_janitor.py" in text


def test_claude_md_documents_the_sweep():
	text = " ".join(CLAUDE_MD.read_text(encoding="utf-8").split())
	section = text[text.index("### I) Stale session sweep"):text.index("## §27.")]
	for needle in ("scripts/claude_session_janitor.py", "`— wake.`", "`next_after_id`", "`SESSION_STATUS_IDLE`",
		"`REQUIRES_ACTION`", "2 hours", "7 days", "`need_input`", "one REST read per distinct", "`get_session`",
		"`PR#<n> · fix-claude-pr`", "`#<n> · implement-issue-claude`", "`#<n> · PR #<pr> — implement-issue-claude`"):
		assert needle in section, needle
	pushing = text[text.index("### D) What the pushing session does"):text.index("### E) Enforcement")]
	assert "archives it 7 days after the report (§26.I)" in pushing
	assert "`/fix-claude-pr` session" in pushing and "archives itself" in pushing


def test_agents_md_lists_the_pickup_title_forms():
	text = " ".join(AGENTS_MD.read_text(encoding="utf-8").split())
	section = text[text.index("Stale session sweep (CLAUDE.md §26.I, #4887)"):]
	for needle in ("`PR#<n> · fix-claude-pr`", "`#<n> · implement-issue-claude`", "`#<n> · PR #<pr> — implement-issue-claude`"):
		assert needle in section[:1500], needle


def test_fix_claude_pr_twin_archives_the_fixer_on_a_terminal_pr():
	text = " ".join(FIX_CLAUDE_PR_TWIN.read_text(encoding="utf-8").split())
	assert "- `hand_back_all` (`merged` / `closed`) → nothing to fix." in text
	assert "`archive_session` on this session as the last action" in text
	assert "Never archive yourself: the report is what the user opens." not in text
	assert "CLAUDE.md §26.I" in text


def test_settings_twin_allows_the_janitor():
	allow = json.loads(SETTINGS_TWIN.read_text(encoding="utf-8"))["permissions"]["allow"]
	assert "Bash(python3 scripts/claude_session_janitor.py *)" in allow
	assert "Bash(PYTHONDONTWRITEBYTECODE=1 python3 scripts/claude_session_janitor.py *)" in allow


def test_pickup_runs_the_sweep_on_wake():
	# .claude/commands/claude-issue-pickup.md has no twin, so this reads the
	# root pickup file directly.
	text = " ".join(PICKUP.read_text(encoding="utf-8").split())
	assert "3a. **Archive finished sessions** (`— wake.` mode only" in text
	assert "PYTHONDONTWRITEBYTECODE=1 python3 scripts/claude_session_janitor.py --sessions <file> --self <your session id>" in text
	assert "`next_after_id`" in text and "`after_id`" in text
	assert "archived <a>" in text
