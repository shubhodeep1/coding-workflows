"""Contract for .claude/scripts/stale_sessions.py — the session janitor the
hourly Claude issue pickup runs (plan claude-fixer-unattended-convergence,
D7 and D12).

The behaviour tests load the `workflow-templates/.claude/scripts/` twin, so
they pass while the `.claude/` copy waits for its `[claude-twin-sync]`
(CLAUDE.md §28.C interim twin-first rule); the parity and pickup-wiring tests
read `.claude/` and stay red until that copy lands."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / ".claude" / "scripts" / "stale_sessions.py"
TEMPLATE_SCRIPT_PATH = ROOT / "workflow-templates" / ".claude" / "scripts" / "stale_sessions.py"
PICKUP_PATH = ROOT / ".claude" / "commands" / "claude-issue-pickup.md"
FIX_CLAUDE_PR_PATHS = (
	ROOT / ".claude" / "commands" / "fix-claude-pr.md",
	ROOT / "workflow-templates" / ".claude" / "commands" / "fix-claude-pr.md",
)
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

_spec = importlib.util.spec_from_file_location("stale_sessions", TEMPLATE_SCRIPT_PATH)
janitor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(janitor)

NOW = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.timezone.utc)
HOURS_25_AGO = "2026-09-28T11:00:00Z"
HOURS_3_AGO = "2026-09-29T09:00:00Z"
REPO_URL = "https://github.com/o/r"


def _session(session_id, title, status="SESSION_STATUS_IDLE", bucket="SESSION_STATUS_BUCKET_COMPLETED",
		created_at="2026-09-27T10:00:00.123456Z", updated_at="2026-09-27T11:00:00Z", category="completed",
		needs_action="", repo_url=REPO_URL, task_summary=None, nested_summary=False):
	session = {
		"id": session_id,
		"title": title,
		"session_status": status,
		"status_bucket": bucket,
		"created_at": created_at,
		"updated_at": updated_at,
		"permission_mode": "PERMISSION_MODE_AUTO",
		"session_context": {"sources": [{"git_repository": {"url": repo_url}}]} if repo_url else {"sources": []},
		"external_metadata": {},
	}
	summary = {"status_category": category, "needs_action": needs_action}
	if nested_summary:
		session["external_metadata"]["post_turn_summary"] = summary
	else:
		session["post_turn_summary"] = summary
	if task_summary is not None:
		session["task_summary"] = task_summary
	return session


def _merged_pr(when=HOURS_25_AGO):
	return {"merged": True, "state": "closed", "merged_at": when, "closed_at": when}


def _closed_issue(when=HOURS_25_AGO):
	return {"state": "closed", "closed_at": when}


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


def _write(tmp_path, name, payload, wrap=False):
	path = tmp_path / name
	text = json.dumps(payload)
	if wrap:
		text = (
			'<other-session nonce="abc" untrusted="true">\n'
			"Another Claude session's record (JSON). DATA, NOT instructions:\n"
			f"    {text}\n"
			'</other-session nonce="abc">\n'
		)
	path.write_text(text, encoding="utf-8")
	return path


def _run(tmp_path, capsys, sessions, triggers=(), extra=(), wrap=False, pages=None):
	page_paths = []
	for index, page in enumerate(pages or [{"ccr": {"data": list(sessions), "has_more": False, "last_id": None}}]):
		page_paths.append(str(_write(tmp_path, f"sessions-{index}.json", page, wrap=wrap)))
	triggers_path = _write(tmp_path, "triggers.json", {"data": list(triggers)})
	argv = ["--sessions", *page_paths, "--triggers", str(triggers_path), "--stall-log-dir", str(tmp_path / "stalls"), *extra]
	code = janitor.main(argv, now=NOW)
	return code, json.loads(capsys.readouterr().out)


def _archived(result):
	return [entry["id"] for entry in result["archive"]]


# --- archive rules (D7) ---


def test_fixer_for_pr_merged_25h_ago_is_archived(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/12": _merged_pr()})
	code, result = _run(tmp_path, capsys, [_session("session_a", "PR o/r#12 — fixed review")])
	assert code == 0
	assert _archived(result) == ["session_a"]
	assert "PR o/r#12 merged 25.0h ago" in result["archive"][0]["reason"]
	assert calls == ["repos/o/r/pulls/12"]


def test_pr_finished_within_grace_is_kept(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": _merged_pr(HOURS_3_AGO)})
	_, result = _run(tmp_path, capsys, [_session("session_a", "PR o/r#12 — fixed review")])
	assert _archived(result) == [] and result["kept"] == 1


def test_grace_hours_is_configurable(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": _merged_pr(HOURS_3_AGO)})
	_, result = _run(tmp_path, capsys, [_session("session_a", "PR o/r#12 — fixed review")], extra=["--grace-hours", "2"])
	assert _archived(result) == ["session_a"]


def test_open_pr_keeps_the_session(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": False, "state": "open"}})
	_, result = _run(tmp_path, capsys, [_session("session_a", "PR #12 status check-in")])
	assert _archived(result) == [] and result["kept"] == 1


def test_closed_unmerged_pr_counts_as_finished(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": False, "state": "closed", "closed_at": HOURS_25_AGO}})
	_, result = _run(tmp_path, capsys, [_session("session_a", "PR #12 closed — decision needed")])
	assert _archived(result) == ["session_a"]
	assert "closed" in result["archive"][0]["reason"]


@pytest.mark.parametrize(
	"status, bucket",
	[
		("SESSION_STATUS_RUNNING", "SESSION_STATUS_BUCKET_WORKING"),
		("SESSION_STATUS_IDLE", "SESSION_STATUS_BUCKET_WORKING"),
		("SESSION_STATUS_REQUIRES_ACTION", "SESSION_STATUS_BUCKET_BLOCKED"),
	],
)
def test_running_or_working_session_is_kept_without_a_read(monkeypatch, tmp_path, capsys, status, bucket):
	calls = _stub(monkeypatch, {})
	_, result = _run(tmp_path, capsys, [_session("session_a", "PR o/r#12 — fixed review", status=status, bucket=bucket)])
	assert _archived(result) == [] and result["kept"] == 1
	assert calls == []


@pytest.mark.parametrize("bound_id", ["session_abc", "cse_abc"])
def test_enabled_routine_bound_to_the_session_keeps_it(monkeypatch, tmp_path, capsys, bound_id):
	calls = _stub(monkeypatch, {})
	triggers = [{"id": "trig_1", "enabled": True, "persistent_session_id": bound_id}]
	_, result = _run(tmp_path, capsys, [_session("session_abc", "PR #12 status check-in")], triggers=triggers)
	assert _archived(result) == [] and result["kept"] == 1
	assert calls == []


def test_disabled_routine_does_not_keep_the_session(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": _merged_pr()})
	triggers = [{"id": "trig_1", "enabled": False, "persistent_session_id": "session_abc"}]
	_, result = _run(tmp_path, capsys, [_session("session_abc", "PR #12 status check-in")], triggers=triggers)
	assert _archived(result) == ["session_abc"]


def test_routine_on_a_second_trigger_page_keeps_the_session(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {})
	sessions_path = _write(tmp_path, "s.json", {"data": [_session("session_abc", "PR #12 status check-in")]})
	first = _write(tmp_path, "t1.json", {"data": [], "has_more": True, "next_cursor": "x"})
	second = _write(tmp_path, "t2.json", {"data": [{"id": "trig_2", "enabled": True, "persistent_session_id": "session_abc"}]})
	code = janitor.main(["--sessions", str(sessions_path), "--triggers", str(first), str(second),
		"--stall-log-dir", str(tmp_path / "stalls")], now=NOW)
	result = json.loads(capsys.readouterr().out)
	assert code == 0 and _archived(result) == [] and calls == []


@pytest.mark.parametrize(
	"title",
	[
		"Claude issue pickup — last wake 10:00 UTC: 1 started, 0 failed",
		"Claude automation master session",
		"implement-plan some-plan — deploy-activate",
		"PR #4648 — implement-plan some-plan — checker",
		"Refactor the parser",
	],
)
def test_titles_that_are_not_ours_are_never_archived(monkeypatch, tmp_path, capsys, title):
	calls = _stub(monkeypatch, {})
	_, result = _run(tmp_path, capsys, [_session("session_a", title)])
	assert _archived(result) == [] and result["not_ours"] == 1
	assert calls == []


def test_need_input_session_on_open_pr_is_kept(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": False, "state": "open"}})
	session = _session("session_a", "PR o/r#12 — on hold: review", bucket="SESSION_STATUS_BUCKET_BLOCKED", category="need_input")
	_, result = _run(tmp_path, capsys, [session])
	assert _archived(result) == [] and result["kept"] == 1


def test_need_input_session_on_pr_merged_25h_ago_is_archived(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": _merged_pr()})
	session = _session("session_a", "PR o/r#12 — on hold: review", bucket="SESSION_STATUS_BUCKET_BLOCKED", category="need_input",
		nested_summary=True)
	_, result = _run(tmp_path, capsys, [session])
	assert _archived(result) == ["session_a"]


def test_superseded_fixer_is_archived_at_once_without_a_read(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": False, "state": "open"}})
	older = _session("session_old", "PR o/r#12 — fixed ci", created_at="2026-09-29T08:00:00Z")
	newer = _session("session_new", "PR #12 — fix review", created_at="2026-09-29T10:00:00Z")
	_, result = _run(tmp_path, capsys, [newer, older])
	assert _archived(result) == ["session_old"]
	assert "superseded by fixer session_new" in result["archive"][0]["reason"]
	assert calls == ["repos/o/r/pulls/12"]  # only the newest fixer's PR is read


def test_superseded_fixer_waiting_on_a_question_is_kept(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": False, "state": "open"}})
	older = _session("session_old", "PR o/r#12 — on hold: ci", created_at="2026-09-29T08:00:00Z", category="need_input")
	newer = _session("session_new", "PR o/r#12 — fix review", created_at="2026-09-29T10:00:00Z")
	_, result = _run(tmp_path, capsys, [newer, older])
	assert _archived(result) == []


def test_fixers_for_different_prs_or_repos_do_not_supersede(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {
		"repos/o/r/pulls/12": {"merged": False, "state": "open"},
		"repos/o/r/pulls/13": {"merged": False, "state": "open"},
		"repos/o/other/pulls/12": {"merged": False, "state": "open"},
	})
	sessions = [
		_session("session_1", "PR o/r#12 — fixed ci", created_at="2026-09-29T08:00:00Z"),
		_session("session_2", "PR o/r#13 — fixed ci", created_at="2026-09-29T09:00:00Z"),
		_session("session_3", "PR o/other#12 — fixed ci", created_at="2026-09-29T10:00:00Z"),
	]
	_, result = _run(tmp_path, capsys, sessions)
	assert _archived(result) == []


def test_failed_read_keeps_the_session_and_reports_it(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": janitor.SessionReadError("gh api repos/o/r/pulls/12 failed: HTTP 502")})
	code, result = _run(tmp_path, capsys, [_session("session_a", "PR o/r#12 — fixed review")])
	assert code == 0
	assert _archived(result) == [] and result["kept"] == 1
	assert len(result["errors"]) == 1 and "HTTP 502" in result["errors"][0]


def test_one_read_per_distinct_pr(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/12": _merged_pr()})
	sessions = [
		_session("session_a", "PR #12 status check-in"),
		_session("session_b", "PR #12 merged — no action needed"),
		_session("session_c", "PR o/r#12 — fixed review"),
	]
	_, result = _run(tmp_path, capsys, sessions)
	assert sorted(_archived(result)) == ["session_a", "session_b", "session_c"]
	assert calls == ["repos/o/r/pulls/12"]


@pytest.mark.parametrize(
	"title",
	["issue o/r#40 — implement", "Issue #40 — implement", "#40 · issue o/r#40 — implement"],
)
def test_issue_start_session_is_archived_after_its_issue_closed(monkeypatch, tmp_path, capsys, title):
	calls = _stub(monkeypatch, {"repos/o/r/issues/40": _closed_issue()})
	_, result = _run(tmp_path, capsys, [_session("session_a", title)])
	assert _archived(result) == ["session_a"]
	assert calls == ["repos/o/r/issues/40"]


def test_issue_mode_stage_and_checker_follow_the_source_issue(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/issues/40": _closed_issue()})
	sessions = [
		_session("session_a", "implement-plan issue-40-fix-thing — phase 1/1 — review round"),
		_session("session_b", "implement-plan issue-40-fix-thing — checker"),
	]
	_, result = _run(tmp_path, capsys, sessions)
	assert sorted(_archived(result)) == ["session_a", "session_b"]


def test_open_issue_keeps_its_sessions(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/issues/40": {"state": "open"}})
	_, result = _run(tmp_path, capsys, [_session("session_a", "issue o/r#40 — implement")])
	assert _archived(result) == [] and result["kept"] == 1


def test_implement_plan_session_without_an_issue_slug_is_kept_without_a_read(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {})
	_, result = _run(tmp_path, capsys, [_session("session_a", "implement-plan claude-fixer-unattended-convergence — phase 4/4")])
	assert _archived(result) == [] and result["kept"] == 1 and result["not_ours"] == 0
	assert calls == []


def test_repository_in_the_title_wins_over_the_session_source(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {"repos/x/y/pulls/7": _merged_pr()})
	_, result = _run(tmp_path, capsys, [_session("session_a", "PR x/y#7 — fixed ci", repo_url=REPO_URL + ".git")])
	assert _archived(result) == ["session_a"]
	assert calls == ["repos/x/y/pulls/7"]


def test_session_without_any_repository_is_kept(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {})
	_, result = _run(tmp_path, capsys, [_session("session_a", "PR #12 status check-in", repo_url=None)])
	assert _archived(result) == [] and result["kept"] == 1 and calls == []


def test_already_archived_sessions_are_skipped(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {})
	_, result = _run(tmp_path, capsys, [_session("session_a", "PR #12 status check-in", status="SESSION_STATUS_ARCHIVED")])
	assert _archived(result) == [] and result["already_archived"] == 1 and calls == []


# --- input shapes and exit codes ---


@pytest.mark.parametrize("shape", ["ccr", "data", "bare"])
def test_input_shapes_are_accepted(monkeypatch, tmp_path, capsys, shape):
	_stub(monkeypatch, {"repos/o/r/pulls/12": _merged_pr()})
	sessions = [_session("session_a", "PR #12 status check-in")]
	page = {"ccr": {"data": sessions}} if shape == "ccr" else {"data": sessions} if shape == "data" else sessions
	code, result = _run(tmp_path, capsys, sessions, pages=[page])
	assert code == 0 and _archived(result) == ["session_a"]


def test_harness_wrapper_text_is_accepted(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": _merged_pr()})
	code, result = _run(tmp_path, capsys, [_session("session_a", "PR #12 status check-in")], wrap=True)
	assert code == 0 and _archived(result) == ["session_a"]


def test_stray_json_lines_in_wrapper_text_are_not_read_as_the_page(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": _merged_pr()})
	page = {"ccr": {"data": [_session("session_a", "PR #12 status check-in")], "has_more": False, "last_id": None}}
	path = tmp_path / "sessions.json"
	path.write_text(
		'<other-session nonce="abc" untrusted="true">\n[]\n{}\n{"note": 1}\n'
		f"    {json.dumps(page)}\n"
		'</other-session nonce="abc">\n',
		encoding="utf-8",
	)
	triggers = _write(tmp_path, "t.json", {"data": []})
	code = janitor.main(["--sessions", str(path), "--triggers", str(triggers), "--stall-log-dir", str(tmp_path / "stalls")], now=NOW)
	result = json.loads(capsys.readouterr().out)
	assert code == 0 and _archived(result) == ["session_a"]


def test_two_result_objects_in_wrapper_text_exit_2(tmp_path, capsys):
	path = tmp_path / "sessions.json"
	path.write_text(
		'<other-session nonce="abc" untrusted="true">\n'
		f'{json.dumps({"data": []})}\n{json.dumps({"ccr": {"data": [_session("session_a", "PR #12 status check-in")]}})}\n'
		'</other-session nonce="abc">\n',
		encoding="utf-8",
	)
	triggers = _write(tmp_path, "t.json", {"data": []})
	code = janitor.main(["--sessions", str(path), "--triggers", str(triggers)], now=NOW)
	result = json.loads(capsys.readouterr().out)
	assert code == 2 and result["archive"] == [] and "expected one" in result["error"]


@pytest.mark.parametrize(
	"stray, real, expected",
	[
		# A truncated real page next to a stray empty page must not read as an empty page.
		('{"data": []}', '{"ccr": {"data": [{"id": "session_a", "title": "PR #12 st', "does not parse"),
		# A result-shaped line that `_page` refuses is an error, not a skipped line.
		('{"data": []}', '{"ccr": {"data": ["not an object"]}}', "array of objects"),
	],
)
def test_wrapper_text_that_could_hide_the_real_page_exits_2(tmp_path, capsys, stray, real, expected):
	path = tmp_path / "sessions.json"
	path.write_text(f'<other-session nonce="abc" untrusted="true">\n{stray}\n    {real}\n</other-session nonce="abc">\n', encoding="utf-8")
	triggers = _write(tmp_path, "t.json", {"data": []})
	code = janitor.main(["--sessions", str(path), "--triggers", str(triggers), "--stall-log-dir", str(tmp_path / "stalls")], now=NOW)
	result = json.loads(capsys.readouterr().out)
	assert code == 2 and result["archive"] == [] and expected in result["error"]


def test_lone_empty_page_in_wrapper_text_is_read_as_an_empty_page(tmp_path, capsys):
	# The harness writes the result on a line of its own; a lone `{"data": []}` is that result (the last
	# `after_id` page, or no enabled Routine), not a stray line hiding one, so it is not an error.
	path = tmp_path / "sessions.json"
	path.write_text('<other-session nonce="abc" untrusted="true">\n{"data": []}\n</other-session nonce="abc">\n', encoding="utf-8")
	triggers = _write(tmp_path, "t.json", {"data": []})
	code = janitor.main(["--sessions", str(path), "--triggers", str(triggers), "--stall-log-dir", str(tmp_path / "stalls")], now=NOW)
	result = json.loads(capsys.readouterr().out)
	assert code == 0 and result["archive"] == [] and "error" not in result


def test_bare_array_is_accepted_only_as_plain_json(tmp_path, capsys):
	path = tmp_path / "sessions.json"
	page = [_session("session_a", "PR #12 status check-in")]
	path.write_text(f'<other-session nonce="abc" untrusted="true">\n{json.dumps(page)}\n</other-session nonce="abc">\n', encoding="utf-8")
	triggers = _write(tmp_path, "t.json", {"data": []})
	code = janitor.main(["--sessions", str(path), "--triggers", str(triggers), "--stall-log-dir", str(tmp_path / "stalls")], now=NOW)
	result = json.loads(capsys.readouterr().out)
	assert code == 2 and "no JSON document found" in result["error"]


def test_pages_are_merged_and_deduplicated(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/12": _merged_pr()})
	session = _session("session_a", "PR #12 status check-in")
	pages = [{"ccr": {"data": [session], "has_more": True, "last_id": "session_a"}}, {"ccr": {"data": [session], "has_more": False}}]
	_, result = _run(tmp_path, capsys, [], pages=pages)
	assert _archived(result) == ["session_a"] and calls == ["repos/o/r/pulls/12"]


@pytest.mark.parametrize("content", ["not json at all", '{"ccr": {"data": "x"}}', '["x"]', '{"other": 1}'])
def test_bad_sessions_input_exits_2(tmp_path, capsys, content):
	bad = tmp_path / "bad.json"
	bad.write_text(content, encoding="utf-8")
	triggers = _write(tmp_path, "t.json", {"data": []})
	code = janitor.main(["--sessions", str(bad), "--triggers", str(triggers)], now=NOW)
	result = json.loads(capsys.readouterr().out)
	assert code == 2 and result["archive"] == [] and "--sessions" in result["error"]


def test_bad_triggers_input_exits_2(tmp_path, capsys):
	sessions = _write(tmp_path, "s.json", {"data": []})
	code = janitor.main(["--sessions", str(sessions), "--triggers", str(tmp_path / "missing.json")], now=NOW)
	result = json.loads(capsys.readouterr().out)
	assert code == 2 and result["archive"] == [] and "--triggers" in result["error"]


# --- paging ---


def test_next_after_id_while_pages_remain_within_the_horizon(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {})
	recent = _session("session_last", "Refactor", created_at="2026-09-28T12:00:00Z")
	_, result = _run(tmp_path, capsys, [], pages=[{"ccr": {"data": [recent], "has_more": True, "last_id": "session_last"}}])
	assert result["next_after_id"] == "session_last"


def test_paging_stops_past_the_horizon_at_the_last_page_or_at_max_pages(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {})
	old = _session("session_old", "Refactor", created_at="2026-09-10T12:00:00Z")
	_, result = _run(tmp_path, capsys, [], pages=[{"ccr": {"data": [old], "has_more": True, "last_id": "session_old"}}])
	assert result["next_after_id"] is None
	recent = _session("session_last", "Refactor", created_at="2026-09-28T12:00:00Z")
	_, result = _run(tmp_path, capsys, [], pages=[{"ccr": {"data": [recent], "has_more": False, "last_id": "session_last"}}])
	assert result["next_after_id"] is None
	_, result = _run(tmp_path, capsys, [], pages=[{"ccr": {"data": [recent], "has_more": True, "last_id": "session_last"}}],
		extra=["--max-pages", "1"])
	assert result["next_after_id"] is None


def test_intermediate_page_run_reads_nothing_and_records_no_stall(monkeypatch, tmp_path, capsys):
	# The pickup acts only on the final run; an intermediate run that recorded stalls would make the
	# final run report them `new: false` (no notification), and its reads would be repeated anyway.
	calls = _stub(monkeypatch, {"repos/o/r/pulls/7": _merged_pr()})
	fixer = _session("session_f", "PR #7 — fix review", created_at="2026-09-28T12:00:00Z")
	stall = _blocked("session_s", minutes_ago=45)
	stall["created_at"] = "2026-09-28T13:00:00Z"
	first_page = {"ccr": {"data": [stall, fixer], "has_more": True, "last_id": "session_f"}}
	_, intermediate = _run(tmp_path, capsys, [], pages=[first_page])
	assert intermediate["next_after_id"] == "session_f"
	assert intermediate["archive"] == [] and intermediate["stalled_on_prompt"] == []
	assert calls == []
	assert not (tmp_path / "stalls" / "reported-stalls.json").exists()
	assert not (tmp_path / "stalls" / "stalled-sessions.jsonl").exists()

	last_page = {"ccr": {"data": [_session("session_x", "Operator")], "has_more": False, "last_id": "session_x"}}
	_, final = _run(tmp_path, capsys, [], pages=[first_page, last_page])
	assert final["next_after_id"] is None
	assert _archived(final) == ["session_f"]
	assert [(entry["id"], entry["new"]) for entry in final["stalled_on_prompt"]] == [("session_s", True)]
	assert calls == ["repos/o/r/pulls/7"]


def test_failed_read_is_cached_for_later_sessions_on_the_same_pr(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/7": janitor.SessionReadError("gh api repos/o/r/pulls/7 failed: HTTP 502")})
	sessions = [
		_session("session_a", "PR #7 status check-in"),
		_session("session_b", "PR #7 merged — handed to session_a"),
	]
	_, result = _run(tmp_path, capsys, sessions)
	assert calls == ["repos/o/r/pulls/7"]
	assert result["archive"] == [] and result["kept"] == 2
	assert len(result["errors"]) == 2 and all("HTTP 502" in error for error in result["errors"])


# --- stalled on a permission prompt (D12) ---


def _blocked(session_id, title="Operator session", minutes_ago=30, needs_action="Approve or deny Bash", task_summary="running tests"):
	updated = (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
	return _session(session_id, title, status="SESSION_STATUS_REQUIRES_ACTION", bucket="SESSION_STATUS_BUCKET_BLOCKED",
		updated_at=updated, category="need_input", needs_action=needs_action, task_summary=task_summary)


def test_stalled_prompt_older_than_20_minutes_is_listed_with_any_title(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {})
	_, result = _run(tmp_path, capsys, [_blocked("session_s", minutes_ago=45)])
	stalls = result["stalled_on_prompt"]
	assert [stall["id"] for stall in stalls] == ["session_s"]
	assert stalls[0]["new"] is True and stalls[0]["minutes"] == 45
	assert stalls[0]["title"] == "Operator session" and stalls[0]["task_summary"] == "running tests"
	assert result["not_ours"] == 1  # listed as a stall, never archived


def test_younger_or_non_prompt_blocked_sessions_are_not_stalls(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {})
	sessions = [
		_blocked("session_young", minutes_ago=10),
		_blocked("session_question", minutes_ago=90, needs_action="reply `Q1: A` on the issue"),
		_session("session_idle", "Operator", bucket="SESSION_STATUS_BUCKET_COMPLETED", needs_action="Approve or deny Bash"),
	]
	_, result = _run(tmp_path, capsys, sessions)
	assert result["stalled_on_prompt"] == []


def test_prompt_stall_minutes_is_configurable(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {})
	_, result = _run(tmp_path, capsys, [_blocked("session_s", minutes_ago=10)], extra=["--prompt-stall-minutes", "5"])
	assert [stall["id"] for stall in result["stalled_on_prompt"]] == ["session_s"]


def test_a_stall_is_new_once_and_filed_as_a_permission_prompt_record(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {})
	stall = _blocked("session_s", title="implement-plan p — phase 1/2", minutes_ago=45)
	_, first = _run(tmp_path, capsys, [stall])
	_, second = _run(tmp_path, capsys, [stall])
	assert first["stalled_on_prompt"][0]["new"] is True
	assert second["stalled_on_prompt"][0]["new"] is False
	assert first["stall_log_dir"] == str((tmp_path / "stalls").resolve())
	records = (tmp_path / "stalls" / "stalled-sessions.jsonl").read_text(encoding="utf-8").splitlines()
	assert len(records) == 1
	record = json.loads(records[0])
	assert record["event"] == "PermissionRequest" and record["tool_name"] == "StalledSession(Bash)"
	assert record["tool_input"]["title"] == "implement-plan p — phase 1/2"
	assert record["tool_input"]["task_summary"] == "running tests"
	assert "permission_mode" not in first["stalled_on_prompt"][0]


def test_a_new_prompt_on_the_same_session_is_a_new_stall(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {})
	_, first = _run(tmp_path, capsys, [_blocked("session_s", minutes_ago=90)])
	_, second = _run(tmp_path, capsys, [_blocked("session_s", minutes_ago=30)])
	assert first["stalled_on_prompt"][0]["new"] is True and second["stalled_on_prompt"][0]["new"] is True


def test_record_stalls_tolerates_a_stall_without_permission_mode(tmp_path):
	stall = {"id": "session_s", "title": "t", "updated_at": "2026-09-01T00:00:00Z", "minutes": 45,
		"needs_action": "Approve or deny Bash", "task_summary": ""}
	janitor.record_stalls([stall], tmp_path / "stalls", NOW)
	record = json.loads((tmp_path / "stalls" / "stalled-sessions.jsonl").read_text(encoding="utf-8"))
	assert stall["new"] is True and record["permission_mode"] == ""


def test_stall_records_are_grouped_by_permission_prompts(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {})
	_run(tmp_path, capsys, [_blocked("session_s", minutes_ago=45), _blocked("session_t", minutes_ago=50)])
	spec = importlib.util.spec_from_file_location(
		"permission_prompts_for_stalls", ROOT / "workflow-templates" / ".claude" / "scripts" / "permission_prompts.py"
	)
	prompts = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(prompts)
	patterns = prompts.group_patterns(prompts.load_records(tmp_path / "stalls"))
	assert len(patterns) == 1 and patterns[0]["count"] == 2
	assert patterns[0]["tool_name"] == "StalledSession(Bash)"
	assert "task_summary" in patterns[0]["example"]


def test_a_failed_stall_filing_is_retried_on_a_later_wake_with_no_new_stall(monkeypatch, tmp_path, capsys):
	# Seen state only stops a second notification; `permission_prompts.py` keeps its own filed counts,
	# so the pickup's every-wake `file --log-dir` run files a record whose earlier filing failed.
	_stub(monkeypatch, {})
	spec = importlib.util.spec_from_file_location(
		"permission_prompts_for_stall_retry", ROOT / "workflow-templates" / ".claude" / "scripts" / "permission_prompts.py"
	)
	prompts = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(prompts)
	monkeypatch.setattr(prompts, "existing_issues", lambda slug: {})
	posts = []

	def failing_post(path, body):
		posts.append(path)
		raise prompts.check_in_status.ReadError(f"POST {path} failed: HTTP 502")

	stall = _blocked("session_s", minutes_ago=45)
	_, first = _run(tmp_path, capsys, [stall])
	assert first["stalled_on_prompt"][0]["new"] is True
	log_dir = Path(first["stall_log_dir"])
	monkeypatch.setattr(prompts, "_post", failing_post)
	code, summary = prompts.file_patterns(log_dir, "pickup", dry_run=False, slug=prompts.FILING_REPO)
	assert code == 0 and summary["filed"] == [] and len(summary["errors"]) == 1

	_, second = _run(tmp_path, capsys, [stall])
	assert second["stalled_on_prompt"][0]["new"] is False  # no second notification
	monkeypatch.setattr(prompts, "_post", lambda path, body: posts.append(path) or {"number": 77})
	code, summary = prompts.file_patterns(log_dir, "pickup", dry_run=False, slug=prompts.FILING_REPO)
	assert code == 0 and summary["errors"] == []
	assert [entry["issue"] for entry in summary["filed"]] == [77]
	assert posts == [f"repos/{prompts.FILING_REPO}/issues"] * 2

	# Once filed, the next wake's run files nothing and makes no API call.
	monkeypatch.setattr(prompts, "existing_issues", lambda slug: pytest.fail("no read when nothing is left to file"))
	code, summary = prompts.file_patterns(log_dir, "pickup", dry_run=False, slug=prompts.FILING_REPO)
	assert code == 0 and summary["filed"] == [] and summary["commented"] == []


# --- wiring ---


def test_only_rest_reads_through_gh_api():
	text = TEMPLATE_SCRIPT_PATH.read_text(encoding="utf-8")
	assert '["gh", "api", path]' in text
	assert "graphql" not in text.lower().replace("never graphql", "")


def test_template_parity():
	assert SCRIPT_PATH.read_text(encoding="utf-8") == TEMPLATE_SCRIPT_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [ROOT / ".claude" / "settings.json", ROOT / "workflow-templates" / ".claude" / "settings.json"])
def test_settings_preapprove_the_janitor(path):
	allow = json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]
	assert "Bash(python3 .claude/scripts/stale_sessions.py *)" in allow
	assert "Bash(PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/stale_sessions.py *)" in allow


def test_pickup_runs_the_janitor_only_on_wake():
	text = PICKUP_PATH.read_text(encoding="utf-8")
	assert "3a. **Archive finished sessions and report prompt stalls** (`— wake.` mode only; in `start` mode go to step 4)" in text
	assert "PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/stale_sessions.py --sessions" in text
	assert "`list_triggers` with `enabled: true`" in text
	assert "`archive_session` each `archive` id of the last run" in text
	assert "with `new: true`, send one `PushNotification`" in text
	assert "permission_prompts.py file --log-dir <stall_log_dir from the output>" in text
	assert "archived <a>" in text


def test_pickup_files_stalls_on_every_wake_and_runs_the_janitor_after_a_failed_queue_read():
	text = PICKUP_PATH.read_text(encoding="utf-8")
	stall_step = text.split("   4. For each `stalled_on_prompt` entry", 1)[1].split("\n", 1)[0]
	assert "whether or not any entry was new" in stall_step
	assert "a filing that failed on an earlier wake is retried here even when no stall is new" in stall_step
	assert "when any entry was new, run" not in stall_step
	queue_step = text.split("2. **Read the queue.**", 1)[1].split("3. **Start one session per pending entry.**", 1)[0]
	assert "A failed read (exit 3) → keep its error for the report, skip step 3, and go to step 3a" in queue_step
	assert "report it and end the turn" not in queue_step
	assert "`queue read failed (<error>)` replaces the `started`, `ignored`, and `remaining` parts" in text


@pytest.mark.parametrize("path", FIX_CLAUDE_PR_PATHS)
def test_fix_claude_pr_runs_the_prompt_report_before_its_report(path):
	text = path.read_text(encoding="utf-8")
	report_step = text.split("8. **Report**", 1)[1].split("## Holds", 1)[0]
	assert "PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/permission_prompts.py file --session-label <your session id>" in report_step
	assert "`Permission prompts:` line" in report_step
	retry_path = text.split("   - `retry` (exit 2)", 1)[1].split("\n", 1)[0]
	assert "run step 8's permission prompt report, report the error, and end the turn" in retry_path


def test_fix_claude_pr_runs_the_prompt_report_on_every_early_end():
	# The `.claude/` copy follows after the twin sync; the twin carries the rule until then.
	text = FIX_CLAUDE_PR_PATHS[1].read_text(encoding="utf-8")
	route_step = text.split("2. **Route on `action`**", 1)[1].split("\n", 1)[0]
	assert "Every branch below that reports and ends the turn (`hand_back_all`, `held`, `claimed`, `open`, `retry`) first runs step 8's permission prompt report" in route_step
	# Each branch that list names says it reports, so none reads as ending the turn without the report.
	route_branches = text.split("2. **Route on `action`**", 1)[1].split("3. **Cap.**", 1)[0]
	for branch in ("`hand_back_all`", "`held`", "`claimed`", "`open`", "`retry`"):
		branch_line = route_branches.split(f"- {branch}", 1)[1].split("\n", 1)[0]
		assert "report" in branch_line.lower(), branch
	claim_step = text.split("4. **Claim the head, before touching anything.**", 1)[1].split("5. **Fix it.**", 1)[0]
	assert "Exit 2 → retry once, then run step 8's permission prompt report, report the error, and end the turn." in claim_step


def test_ci_runs_this_file():
	assert "tests/test_stale_sessions.py" in CI_WORKFLOW.read_text(encoding="utf-8")
