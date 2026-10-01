"""`check_in_status.py` treats a model-provider outage as a wait (issue #5773).

The `.claude/` change ships twin-first (Q40), so these tests load the
`workflow-templates/.claude/scripts/` twin; the live copy follows with the
twin sync.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TWIN = ROOT / "workflow-templates" / ".claude" / "scripts" / "check_in_status.py"


def _load():
	spec = importlib.util.spec_from_file_location("check_in_status_twin_outage", TWIN)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


checker = _load()

REPO = "o/r"
HEAD = "c" * 40
WORKFLOW = "workflow-bot"
NOW = dt.datetime(2026, 9, 30, 23, 0, tzinfo=dt.timezone.utc)


def _pr(ref="claude/fix-x"):
	return {"merged": False, "state": "open", "labels": [], "mergeable_state": "clean", "updated_at": "2026-09-30T18:00:00Z",
		"head": {"sha": HEAD, "ref": ref}, "user": {"login": WORKFLOW, "type": "User"}, "base": {"repo": {"default_branch": "main"}}}


def _failure(comment_id, run, reason="provider_unavailable", login=WORKFLOW, head=HEAD):
	return {"id": comment_id, "user": {"login": login, "type": "User"}, "author_association": "OWNER",
		"created_at": "2026-09-30T18:00:00Z",
		"body": f"**AI review/autofix paused: model provider unavailable**\n\n<!-- review-autofix-failure:v1 head={head} reason={reason} fp={'f' * 64} degraded=0 run={run} -->"}


def _check(name, run, conclusion="failure"):
	return {"name": name, "status": "completed", "conclusion": conclusion, "completed_at": "2026-09-30T18:00:00Z",
		"details_url": f"https://github.com/{REPO}/actions/runs/{run}/job/9"}


@pytest.fixture
def wire(monkeypatch):
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", WORKFLOW)

	def apply(pr, comments, check_runs, active=0, commit_date="2026-09-30T10:00:00Z"):
		def gh_api(path):
			if path.startswith(f"repos/{REPO}/commits/"):
				return {"commit": {"committer": {"date": commit_date}}}
			return pr

		monkeypatch.setattr(checker, "gh_api", gh_api)
		monkeypatch.setattr(checker, "gh_api_list", lambda path: comments)
		monkeypatch.setattr(checker, "_gh_api_paginated_object", lambda path, key: {"check_runs": check_runs})
		monkeypatch.setattr(checker, "_active_run_count", lambda *args, **kwargs: active)

	return apply


def test_outage_only_failure_waits_in_hand_back_mode(wire):
	wire(_pr(), [_failure(1, 501)], [_check("review / codex-agent", 501), _check("lint", 7, "success")])
	verdict = checker.check_pr_hand_back(REPO, 12, 6.0, 0.0, NOW)
	assert (verdict["done"], verdict["state"]) == (False, "provider-unavailable")
	assert "review / codex-agent" in verdict["reason"] and verdict["hand_backs"] == 0
	assert checker.route_verdict(verdict, "hand_back") == {"action": "wait"}


def test_a_real_failure_next_to_an_outage_still_hands_back(wire):
	wire(_pr(), [_failure(1, 501)], [_check("review / codex-agent", 501), _check("tests", 77)])
	verdict = checker.check_pr_hand_back(REPO, 12, 6.0, 0.0, NOW)
	assert (verdict["done"], verdict["state"]) == (True, "ci-failed")
	assert "tests" in verdict["reason"] and "codex-agent" not in verdict["reason"]


@pytest.mark.parametrize(
	"comment",
	[
		_failure(1, 501, login="someone-else"),
		_failure(1, 501, reason="workflow_failure"),
		_failure(1, 501, head="d" * 40),
		_failure(1, 999),
	],
)
def test_untrusted_or_unrelated_markers_do_not_suppress_a_failed_check(wire, comment):
	wire(_pr(), [comment], [_check("review / codex-agent", 501)])
	verdict = checker.check_pr_hand_back(REPO, 12, 6.0, 0.0, NOW)
	assert verdict["state"] == "ci-failed"


def test_without_the_workflow_login_the_check_stays_failed(wire, monkeypatch):
	wire(_pr(), [_failure(1, 501)], [_check("review / codex-agent", 501)])
	monkeypatch.delenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN")
	assert checker.check_pr_hand_back(REPO, 12, 6.0, 0.0, NOW)["state"] == "ci-failed"


def test_plain_pr_mode_is_not_stuck_on_an_outage(wire):
	wire(_pr("claude/implement-plan-x-phase-1"), [_failure(1, 501)], [_check("review / codex-agent", 501)])
	verdict = checker.check_pr(REPO, 12, False, 6.0, NOW)
	assert (verdict["done"], verdict["state"]) == (False, "provider-unavailable")
	assert checker.route_verdict(verdict, "pr") == {"action": "wait"}


def test_plain_pr_mode_still_reports_a_real_stuck_failure(wire):
	wire(_pr("claude/implement-plan-x-phase-1"), [_failure(1, 501)], [_check("review / codex-agent", 501), _check("tests", 77)])
	verdict = checker.check_pr(REPO, 12, False, 6.0, NOW)
	assert (verdict["done"], verdict["state"]) == (True, "stuck")
	assert "tests" in verdict["reason"] and "codex-agent" not in verdict["reason"]


def test_split_and_marker_helpers_are_pure(monkeypatch):
	runs = [_check("a", 1), _check("b", 2), {"name": "c", "details_url": "https://example.com/x"}]
	remaining, outage_failed = checker.split_provider_outage_checks(runs, {"1"})
	assert [run["name"] for run in remaining] == ["b", "c"] and [run["name"] for run in outage_failed] == ["a"]
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", WORKFLOW)
	assert checker.provider_outage_run_ids([_failure(1, 5), _failure(2, 6, reason="other")], HEAD) == {"5"}
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", "")
	assert checker.provider_outage_run_ids([_failure(1, 5)], HEAD) == set()


def test_the_heal_marker_and_the_checker_share_the_outage_reason(monkeypatch):
	# `check_in_status.py` ships to consumer repos without
	# scripts/workflow_failure_heal.py, so it keeps its own copy of the reason;
	# this pins the copy to the heal module's constant and to the marker the
	# heal module actually renders.
	spec = importlib.util.spec_from_file_location("workflow_failure_heal_reason_parity",
		ROOT / "scripts" / "workflow_failure_heal.py")
	heal = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(heal)
	assert checker.PROVIDER_UNAVAILABLE_REASON == heal.PROVIDER_UNAVAILABLE_REASON
	marker = heal.render_failure_marker(HEAD, heal.PROVIDER_UNAVAILABLE_REASON, "a" * 64, False, run_id="36748847333")
	assert marker
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", WORKFLOW)
	comment = {"id": 1, "user": {"login": WORKFLOW}, "body": f"AI review/autofix paused.\n\n{marker}"}
	assert checker.provider_outage_run_ids([comment], HEAD) == {"36748847333"}
