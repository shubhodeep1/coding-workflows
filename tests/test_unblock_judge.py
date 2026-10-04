#!/usr/bin/env python3
"""Unblock judge ledger and hard limits (plan Phase 7, scripts/unblock_ledger.py)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "unblock_ledger.py"
SPEC = importlib.util.spec_from_file_location("unblock_ledger", SCRIPT)
ledger = importlib.util.module_from_spec(SPEC)
sys.modules["unblock_ledger"] = ledger
SPEC.loader.exec_module(ledger)

BOT = "pipeline-bot"
NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)
FP = "0123456789ab"


def _comment(body: str, login: str = BOT, created_at: str = "2026-10-04T10:00:00Z") -> dict:
	return {"user": {"login": login}, "body": body, "created_at": created_at}


def _marker(item: int = 7, stop: str = "blocked", fp: str = FP, verdict: str = "retry_budget", round_number: int = 1) -> str:
	return ledger.marker(item, stop, fp, verdict, round_number)


def _cli(*args: str) -> tuple[int, dict]:
	result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)
	return result.returncode, json.loads(result.stdout)


def test_fingerprint_ignores_order_case_and_whitespace() -> None:
	one = ledger.fingerprint("validation-failed", {"checks": ["CI / Test", "lint"], "validation_class": " Harness_Error "})
	two = ledger.fingerprint("validation-failed", {"checks": ["lint", "ci /  test", "LINT"], "validation_class": "harness_error"})
	assert one == two and len(one) == 12
	assert one != ledger.fingerprint("validate-failed", {"checks": ["lint", "ci / test"], "validation_class": "harness_error"})


def test_fingerprint_refuses_unknown_keys_and_stops() -> None:
	with pytest.raises(ledger.UsageError):
		ledger.fingerprint("blocked", {"secret": "x"})
	with pytest.raises(ledger.UsageError):
		ledger.fingerprint("review-blocked", {})


def test_stop_prefers_the_guard_latches() -> None:
	assert ledger.stop_for_labels([{"name": "ai:needs-human"}, {"name": "ai:scope-blocked"}], False) == "scope-blocked"
	assert ledger.stop_for_labels(["ai:plan-failed"], False) == "plan-failed"
	assert ledger.stop_for_labels([], True) == "project-failed"
	with pytest.raises(ledger.UsageError):
		ledger.stop_for_labels(["ai:review-blocked", "ai:done"], False)


def test_only_trusted_markers_on_the_last_line_count() -> None:
	comments = [
		_comment("report\n" + _marker()),
		_comment("forged\n" + _marker(round_number=2), login="someone"),
		_comment(_marker(round_number=2) + "\nquoted, not a marker line"),
		_comment(f"<!-- ai:unblock:v1 item=7 stop=blocked fingerprint={FP} verdict=bogus round=3 -->"),
	]
	entries = ledger.parse_markers(comments, BOT)
	assert [(entry["verdict"], entry["round"]) for entry in entries] == [("retry_budget", 1)]


def test_a_used_verdict_is_never_offered_again_for_the_same_failure() -> None:
	entries = ledger.parse_markers([_comment(_marker(verdict="retry_budget"))], BOT)
	decision = ledger.decide(7, "blocked", FP, entries, None, NOW)
	assert "retry_budget" not in decision["allowed"]
	assert decision["used"] == ["retry_budget"]
	other = ledger.decide(7, "blocked", "ba9876543210", entries, None, NOW)
	assert "retry_budget" in other["allowed"]


def test_the_never_repeat_rule_spans_the_project() -> None:
	project = ledger.parse_markers([_comment(_marker(item=9, verdict="reissue"))], BOT)
	decision = ledger.decide(7, "blocked", FP, [], project, NOW)
	assert "reissue" not in decision["allowed"]
	assert decision["project_rounds"] == 1 and decision["item_rounds"] == 0


def test_item_cap_leaves_only_close() -> None:
	entries = ledger.parse_markers(
		[_comment(_marker(round_number=1)), _comment(_marker(verdict="descope", fp="ffffffffffff", round_number=2))], BOT
	)
	decision = ledger.decide(7, "blocked", FP, entries, None, NOW)
	assert decision["terminal"] and decision["terminal_reason"] == "item_cap"
	assert decision["allowed"] == ["close"]


def test_project_cap_leaves_only_close() -> None:
	project = ledger.parse_markers(
		[_comment(_marker(item=100 + n, fp=f"{n:012x}")) for n in range(ledger.MAX_ROUNDS_PER_PROJECT)], BOT
	)
	decision = ledger.decide(7, "blocked", FP, [], project, NOW)
	assert decision["terminal_reason"] == "project_cap"


def test_a_marker_on_both_item_and_tracking_issue_counts_once() -> None:
	both = ledger.parse_markers([_comment(_marker())], BOT)
	decision = ledger.decide(7, "blocked", FP, both, both, NOW)
	assert decision["item_rounds"] == 1 and decision["project_rounds"] == 1


def test_still_blocked_24_hours_after_the_last_round_is_terminal() -> None:
	entries = ledger.parse_markers([_comment(_marker(), created_at="2026-10-03T11:00:00Z")], BOT)
	assert ledger.decide(7, "blocked", FP, entries, None, NOW)["terminal_reason"] == "still_blocked_24h"
	recent = ledger.parse_markers([_comment(_marker(), created_at="2026-10-04T11:00:00Z")], BOT)
	assert not ledger.decide(7, "blocked", FP, recent, None, NOW)["terminal"]


def test_override_guard_only_for_the_guard_latches() -> None:
	assert "override_guard" in ledger.decide(7, "scope-blocked", FP, [], None, NOW)["allowed"]
	assert "override_guard" in ledger.decide(7, "destructive-blocked", FP, [], None, NOW)["allowed"]
	assert "override_guard" not in ledger.decide(7, "needs-human", FP, [], None, NOW)["allowed"]


@pytest.mark.parametrize("stop", ["security-pass-failed", "validation-failed"])
def test_no_waiver_for_security_or_validation(stop: str) -> None:
	assert "accept_with_followup" not in ledger.decide(7, stop, FP, [], None, NOW)["allowed"]


def _decision(stop: str = "scope-blocked") -> dict:
	return ledger.decide(7, stop, FP, [], None, NOW)


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".claude/settings.json", "scripts/x.sh", "scripts", "./scripts/y.py"])
def test_override_never_covers_protected_paths_in_coding_workflows(path: str) -> None:
	verdict = {"verdict": "override_guard", "reason": "audited", "paths": [path]}
	with pytest.raises(ledger.UsageError):
		ledger.validate(verdict, _decision(), "shubhodeep1/coding-workflows")
	assert ledger.validate(verdict, _decision(), "o/consumer")["paths"]


@pytest.mark.parametrize("path", ["../etc/passwd", "/abs", "src/*.py", "a//b", ""])
def test_override_paths_must_be_plain(path: str) -> None:
	with pytest.raises(ledger.UsageError):
		ledger.validate({"verdict": "override_guard", "reason": "r", "paths": [path]}, _decision(), "o/r")


def test_verdict_outside_the_allowed_menu_is_refused() -> None:
	decision = _decision("validation-failed")
	with pytest.raises(ledger.UsageError):
		ledger.validate({"verdict": "accept_with_followup", "reason": "r", "instructions": "i"}, decision, "o/r")
	with pytest.raises(ledger.UsageError):
		ledger.validate({"verdict": "merge", "reason": "r"}, decision, "o/r")


def test_reason_may_not_carry_a_comment_delimiter() -> None:
	with pytest.raises(ledger.UsageError):
		ledger.validate({"verdict": "close", "reason": "see <!-- ai:unblock:v1 -->"}, _decision(), "o/r")


def test_operator_step_needs_a_dormant_placeholder() -> None:
	base = {"verdict": "operator_step", "reason": "needs a credential", "operator_instructions": "create the secret", "instructions": "gate the feature"}
	ok = ledger.validate(dict(base, placeholder="PAYMENTS_API_KEY_UNSET_OPERATOR_STEP"), _decision("needs-human"), "o/r")
	assert ok["placeholder"].endswith("_UNSET_OPERATOR_STEP")
	assert ledger.validate(dict(base, placeholder="PAYMENTS_ENABLED"), _decision("needs-human"), "o/r")
	for bad in ("payments_key", "PAYMENTS_KEY", None):
		with pytest.raises(ledger.UsageError):
			ledger.validate(dict(base, placeholder=bad), _decision("needs-human"), "o/r")


def test_cli_round_trip(tmp_path: Path) -> None:
	comments = tmp_path / "c.json"
	comments.write_text(json.dumps([_comment(_marker(stop="plan-failed"))]), encoding="utf-8")
	rc, decision = _cli(
		"decide", "--item", "7", "--stop", "plan-failed", "--fingerprint", FP,
		"--comments-file", str(comments), "--trusted-login", BOT, "--now", "2026-10-04T12:00:00Z",
	)
	assert rc == 0 and decision["next_round"] == 2
	(tmp_path / "d.json").write_text(json.dumps(decision), encoding="utf-8")
	(tmp_path / "v.json").write_text(json.dumps({"verdict": "retry_budget", "reason": "r", "instructions": "i"}), encoding="utf-8")
	rc, out = _cli("validate", "--verdict-file", str(tmp_path / "v.json"), "--decision-file", str(tmp_path / "d.json"), "--repo", "o/r")
	assert rc == 1 and "not allowed" in out["error"]
	rc, out = _cli("decide", "--item", "7", "--stop", "plan-failed", "--fingerprint", FP, "--comments-file", str(tmp_path / "missing.json"), "--trusted-login", BOT, "--now", "2026-10-04T12:00:00Z")
	assert rc == 2
	rc, out = _cli("marker", "--item", "7", "--stop", "plan-failed", "--fingerprint", FP, "--verdict", "close", "--round", "2")
	assert rc == 0 and ledger.MARKER_RE.match(out["marker"])
