"""Contract for .claude/scripts/stale_routines.py — the deterministic sweep
that names the check-in flows' stale Routines for deletion (CLAUDE.md §26.G,
`/implement-plan-claude` Check-in Loop)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / ".claude" / "scripts" / "stale_routines.py"
TEMPLATE_SCRIPT_PATH = ROOT / "workflow-templates" / ".claude" / "scripts" / "stale_routines.py"
CLAUDE_MD = ROOT / "CLAUDE.md"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

_spec = importlib.util.spec_from_file_location("stale_routines", SCRIPT_PATH)
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)

NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone.utc)
LONG_AGO = "2026-09-23T12:00:00Z"  # 48h before NOW
RECENT = "2026-09-25T09:00:00Z"  # 3h before NOW


PR_URL = "https://github.com/o/r/pull/12"
SECTION_26_HAND_BACK_PROMPT = f"CLAUDE.md §26 hand-back for PR #12 ({PR_URL}): no verdict is attached."
IMPLEMENT_PLAN_HAND_BACK_PROMPT = f"Hand-back for /implement-plan-claude docs/plans/s-plan.md, PR #12 ({PR_URL}): no verdict."


def _routine(routine_id, name, enabled=True, ended_reason="", prompt=""):
	return {
		"id": routine_id,
		"name": name,
		"enabled": enabled,
		"ended_reason": ended_reason,
		"derived_state": {"prompt": prompt},
	}


def _hand_back(routine_id="trig_h", name="PR #12 hand-back", prompt=SECTION_26_HAND_BACK_PROMPT):
	return _routine(routine_id, name, prompt=prompt)


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

	monkeypatch.setattr(sweep, "gh_api", fake)
	return calls


def _run(tmp_path, capsys, payload, extra=()):
	path = tmp_path / "triggers.json"
	path.write_text(json.dumps(payload), encoding="utf-8")
	code = sweep.main(["--triggers", str(path), *extra], now=NOW)
	return code, json.loads(capsys.readouterr().out)


def _deleted_ids(result):
	return [entry["id"] for entry in result["delete"]]


@pytest.mark.parametrize(
	"name",
	[
		"PR #4438 status check-in",
		"PR #4438 status check-in: instructions",
		"implement-plan heal-autofix: check-in",
		"implement-plan heal-autofix: safety net",
		"implement-plan heal-autofix: hand-back",
		"implement-plan heal-deterministic-autofix-failures: safety…",
		"PR #12 hand-back",
		"PR #12 status check-in: subscriber",
		"PR #12 status check-in: fixer start",
		"dispatch shubhodeep1/coding-workflows#4539: start",
	],
)
def test_ended_routines_of_ours_are_deleted(monkeypatch, tmp_path, capsys, name):
	calls = _stub(monkeypatch, {})
	code, result = _run(tmp_path, capsys, {"data": [_routine("trig_a", name, enabled=False, ended_reason="run_once_fired")]})
	assert code == 0
	assert _deleted_ids(result) == ["trig_a"]
	assert result["delete"][0]["reason"] == "ended: run_once_fired"
	assert calls == []


def test_session_gone_routine_is_deleted(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {})
	routine = _routine("trig_b", "PR #4366 status check-in", enabled=False, ended_reason="auto_disabled_session_gone")
	_, result = _run(tmp_path, capsys, [routine])
	assert _deleted_ids(result) == ["trig_b"]


@pytest.mark.parametrize(
	"name",
	[
		"Auto-release/heal loop check (3-hourly)",
		"PR #4366 trial check-in",
		"e2e-dummy run — 3h check-in",
		"probe: routine wake repro",
		"dispatch nightly: start",
		"Swap heal phase-4 checker to Sonnet",
	],
)
def test_routines_the_flows_do_not_create_are_never_deleted(monkeypatch, tmp_path, capsys, name):
	calls = _stub(monkeypatch, {})
	_, result = _run(tmp_path, capsys, [_routine("trig_x", name, enabled=False, ended_reason="run_once_fired")])
	assert result["delete"] == []
	assert result["not_ours"] == 1
	assert calls == []


def test_user_paused_routine_is_kept(monkeypatch, tmp_path, capsys):
	"""Disabled with no ended_reason means the user paused it."""
	calls = _stub(monkeypatch, {})
	_, result = _run(tmp_path, capsys, [_routine("trig_p", "PR #7 status check-in", enabled=False)])
	assert result["delete"] == [] and result["kept"] == 1
	assert calls == []


def test_pending_check_in_reminder_is_kept_without_api_calls(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {})
	_, result = _run(tmp_path, capsys, [_routine("trig_c", "PR #7 status check-in")])
	assert result["delete"] == [] and result["kept"] == 1
	assert calls == []


def test_hand_back_for_long_merged_pr_is_deleted(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": True, "state": "closed", "merged_at": LONG_AGO}})
	_, result = _run(tmp_path, capsys, [_hand_back()])
	assert _deleted_ids(result) == ["trig_h"]
	assert "o/r#12 finished 48.0h ago" in result["delete"][0]["reason"]
	assert calls == ["repos/o/r/pulls/12"]


def test_hand_back_for_long_closed_pr_is_deleted(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": False, "state": "closed", "closed_at": LONG_AGO}})
	_, result = _run(tmp_path, capsys, [_hand_back(name="implement-plan s: hand-back", prompt=IMPLEMENT_PLAN_HAND_BACK_PROMPT)])
	assert _deleted_ids(result) == ["trig_h"]


def test_hand_back_within_grace_is_kept(monkeypatch, tmp_path, capsys):
	"""The checker may not have handed the verdict back yet."""
	_stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": True, "state": "closed", "merged_at": RECENT}})
	_, result = _run(tmp_path, capsys, [_hand_back()])
	assert result["delete"] == [] and result["kept"] == 1


def test_grace_hours_is_configurable(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": True, "state": "closed", "merged_at": RECENT}})
	_, result = _run(tmp_path, capsys, [_hand_back()], extra=("--grace-hours", "2"))
	assert _deleted_ids(result) == ["trig_h"]


def test_hand_back_for_open_pr_is_kept(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": False, "state": "open"}})
	_, result = _run(tmp_path, capsys, [_hand_back()])
	assert result["delete"] == [] and result["kept"] == 1


def test_failed_pr_read_keeps_the_routine_and_reports_it(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": sweep.RoutineReadError("gh api repos/o/r/pulls/12 failed: HTTP 403")})
	code, result = _run(tmp_path, capsys, [_hand_back()])
	assert code == 0
	assert result["delete"] == [] and result["kept"] == 1
	assert "HTTP 403" in result["errors"][0]


def test_one_read_per_distinct_hand_back_pr(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": False, "state": "open"}})
	routines = [_hand_back("trig_1"), _hand_back("trig_2", "implement-plan s: hand-back", IMPLEMENT_PLAN_HAND_BACK_PROMPT)]
	_run(tmp_path, capsys, routines)
	assert calls == ["repos/o/r/pulls/12"]


def test_mixed_listing(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": True, "state": "closed", "merged_at": LONG_AGO}})
	routines = [
		_routine("trig_fired", "PR #3 status check-in", enabled=False, ended_reason="run_once_fired"),
		_routine("trig_live", "PR #4 status check-in"),
		_hand_back("trig_hand"),
		_routine("trig_user", "Auto-release/heal loop check (3-hourly)"),
	]
	_, result = _run(tmp_path, capsys, {"data": routines})
	assert sorted(_deleted_ids(result)) == ["trig_fired", "trig_hand"]
	assert result["kept"] == 1 and result["not_ours"] == 1


@pytest.mark.parametrize("content", ["not json", "{\"data\": 5}", "[1, 2]"])
def test_bad_input_exits_2(tmp_path, capsys, content):
	path = tmp_path / "triggers.json"
	path.write_text(content, encoding="utf-8")
	code = sweep.main(["--triggers", str(path)], now=NOW)
	out = json.loads(capsys.readouterr().out)
	assert code == 2 and out["delete"] == [] and "cannot read --triggers" in out["error"]


def test_missing_input_exits_2(tmp_path, capsys):
	code = sweep.main(["--triggers", str(tmp_path / "absent.json")], now=NOW)
	assert code == 2 and json.loads(capsys.readouterr().out)["delete"] == []


def test_only_rest_reads_through_gh_api():
	text = SCRIPT_PATH.read_text(encoding="utf-8")
	assert '"graphql"' not in text.lower()
	assert '["gh", "api", path]' in text


def test_template_parity():
	assert TEMPLATE_SCRIPT_PATH.read_text(encoding="utf-8") == SCRIPT_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [ROOT / ".claude" / "settings.json", ROOT / "workflow-templates" / ".claude" / "settings.json"])
def test_settings_preapprove_the_sweep(path):
	allow = json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]
	assert "Bash(PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/stale_routines.py *)" in allow
	for tool in ("list_triggers", "delete_trigger", "update_trigger", "get_trigger"):
		assert f"mcp__Claude_Code_Remote__{tool}" in allow
		assert f"mcp__bf7c680d-5fdc-5ef4-b4a0-abadb619bf0a__{tool}" in allow


def test_claude_md_documents_the_sweep():
	joined = " ".join(CLAUDE_MD.read_text(encoding="utf-8").split())
	assert "### G) Stale Routine sweep" in joined
	assert ".claude/scripts/stale_routines.py" in joined
	assert "tests/test_stale_routines.py" in joined


def test_ci_runs_this_file():
	assert "tests/test_stale_routines.py" in CI_WORKFLOW.read_text(encoding="utf-8")


def test_truncated_implement_plan_hand_back_is_still_recognised_by_its_prompt(monkeypatch, tmp_path, capsys):
	"""Routine names are capped at 60 characters; the PR comes from the prompt."""
	_stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": True, "state": "closed", "merged_at": LONG_AGO}})
	name = "implement-plan heal-deterministic-autofix-failures: hand-b…"
	_, result = _run(tmp_path, capsys, [_hand_back(name=name, prompt=IMPLEMENT_PLAN_HAND_BACK_PROMPT)])
	assert _deleted_ids(result) == ["trig_h"]


def test_hand_back_after_verdict_update_is_still_recognised(monkeypatch, tmp_path, capsys):
	"""The checker replaces the prompt with the verdict; it keeps the PR URL."""
	_stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": True, "state": "closed", "merged_at": LONG_AGO}})
	prompt = f'CLAUDE.md §26 hand-back for PR #12 ({PR_URL}). Verdict: {{"done": true}}. Checker session: session_x.'
	_, result = _run(tmp_path, capsys, [_hand_back(prompt=prompt)])
	assert _deleted_ids(result) == ["trig_h"]


def test_top_level_prompt_field_is_accepted(monkeypatch, tmp_path, capsys):
	_stub(monkeypatch, {"repos/o/r/pulls/12": {"merged": False, "state": "open"}})
	routine = {"id": "trig_h", "name": "PR #12 hand-back", "enabled": True, "ended_reason": "", "prompt": SECTION_26_HAND_BACK_PROMPT}
	calls_result = _run(tmp_path, capsys, [routine])[1]
	assert calls_result["kept"] == 1


def test_our_routine_without_a_hand_back_prompt_needs_no_read(monkeypatch, tmp_path, capsys):
	calls = _stub(monkeypatch, {})
	_, result = _run(tmp_path, capsys, [_routine("trig_s", "implement-plan s: safety net", prompt="Safety net for ...")])
	assert result["kept"] == 1 and calls == []


def test_raw_list_triggers_result_is_accepted(monkeypatch, tmp_path, capsys):
	"""The harness may save the whole list_triggers result to a file; pass it as is."""
	_stub(monkeypatch, {})
	raw = {"data": [dict(_routine("trig_a", "PR #3 status check-in", enabled=False, ended_reason="run_once_fired"), cron_expression="", persist_session=True)], "has_more": False}
	_, result = _run(tmp_path, capsys, raw)
	assert _deleted_ids(result) == ["trig_a"]
