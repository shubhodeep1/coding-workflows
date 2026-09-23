"""Contract for .claude/scripts/check_in_status.py — the deterministic
"is the wait over?" verdict the Haiku check-in sessions act on
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


def test_null_commit_time_exits_2_with_error(monkeypatch, capsys):
	_stub(monkeypatch, _stuck_responses(_pr(mergeable_state="dirty"), None))
	code, out = _run(["--pr", "7"], capsys)
	assert code == 2 and out["done"] is False and "timestamp must be a string" in out["error"]


def test_run_completed_and_pending(monkeypatch, capsys):
	_stub(monkeypatch, {"repos/o/r/actions/runs/9": {"status": "completed", "conclusion": "failure"}})
	_, out = _run(["--run", "9"], capsys)
	assert out["done"] is True and out["conclusion"] == "failure"
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
	# Allow rules cannot glob the server segment; an unanchored MCP glob would be skipped.
	assert not any(rule.startswith("mcp__*") for rule in allow)
