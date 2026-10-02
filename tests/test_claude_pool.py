"""Contract for scripts/claude_pool.py — the Claude worker pool's account
selection, prompts and result classification — and its wiring in
.github/workflows/claude-pool-worker.yml
(docs/plans/claude-actions-worker-pool-plan.md, phase 1)."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("claude_pool", ROOT / "scripts" / "claude_pool.py")
pool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pool)

WORKER_WF = ROOT / ".github" / "workflows" / "claude-pool-worker.yml"
CONFIG_FILE = ROOT / ".github" / "ai" / "claude_pool.json"
DISPATCH_CMD = ROOT / ".claude" / "commands" / "claude-issue-dispatch.md"

# Spike evidence, copied from the claude-workers spike runs (plan S7, S15).
RATE_LIMIT_EVENT = {
	"type": "rate_limit_event",
	"rate_limit_info": {
		"status": "allowed",
		"resetsAt": 1790925000,
		"rateLimitType": "five_hour",
		"overageStatus": "rejected",
		"overageDisabledReason": "org_level_disabled",
		"isUsingOverage": False,
		"unifiedWindows": {
			"five_hour": {"utilization": 0.14, "resetsAt": 1790925000},
			"seven_day": {"utilization": 0.03, "resetsAt": 1791504000},
		},
	},
	"uuid": "1b70e556-a22d-443d-a5f1-004b97883f27",
	"session_id": "e6e9a7c6-7e37-4516-9b8b-105852186bfc",
}
AUTH_RESULT = {
	"type": "result",
	"subtype": "success",
	"is_error": True,
	"num_turns": 1,
	"duration_ms": 1940,
	"total_cost_usd": 0,
	"result": "Failed to authenticate. API Error: 401 OAuth access token is invalid.",
	"permission_denials": [],
	"modelUsage": {},
}
OK_RESULT = {
	"type": "result",
	"subtype": "success",
	"is_error": False,
	"num_turns": 1,
	"duration_ms": 1100,
	"total_cost_usd": 0.016,
	"result": "OK",
}
INIT_EVENT = {"type": "system", "subtype": "init", "model": "claude-haiku-4-5-20251001"}
REGISTRY = ["shubhodeep1/digital_pa", "shubhodeep1/coding-workflows"]
HEAD = "c" * 40


def _jsonl(*events, noise=True):
	lines = [json.dumps(event) for event in events]
	if noise:
		lines.insert(0, "npm warn something unrelated")
	return "\n".join(lines) + "\n"


def _rate_limit(five, seven, status="allowed", five_reset=1000, seven_reset=2000, reset=None):
	event = json.loads(json.dumps(RATE_LIMIT_EVENT))
	info = event["rate_limit_info"]
	info["status"] = status
	info["resetsAt"] = reset if reset is not None else five_reset
	info["unifiedWindows"]["five_hour"] = {"utilization": five, "resetsAt": five_reset}
	info["unifiedWindows"]["seven_day"] = {"utilization": seven, "resetsAt": seven_reset}
	return event


def _probe(account, five, seven, status="allowed", error=None, five_reset=1000, seven_reset=2000):
	return {
		"account": account,
		"five_hour": five,
		"seven_day": seven,
		"five_hour_resets_at": five_reset,
		"seven_day_resets_at": seven_reset,
		"resets_at": five_reset,
		"status": status,
		"error": error,
	}


def _b64(text):
	return base64.b64encode(text.encode("utf-8")).decode("ascii")


ISSUE_TEXT = (
	"claude_issue.v1\nrepo: shubhodeep1/digital_pa\nissue: 42\n"
	"url: https://github.com/shubhodeep1/digital_pa/issues/42\ntrigger: opened\nskip_security_pass: false\n"
)
PR_FIX_TEXT = (
	f"claude_pr_fix.v1\nrepo: shubhodeep1/digital_pa\npr: 7\nurl: https://github.com/shubhodeep1/digital_pa/pull/7\n"
	f"head: {HEAD}\nkind: ci\nclaim: sweep-run-123\n"
)


# --- tokens and accounts ---------------------------------------------------


def test_normalize_strips_the_terminal_wrap_seen_in_stored_tokens():
	# S3: a 108-character token stored with a line break at character 80.
	token = "sk-ant-oat01-" + "a" * 95
	wrapped = token[:80] + "\n" + token[80:] + "\n"
	assert len(wrapped.strip()) == 109
	assert pool.normalize_token(wrapped) == token
	assert len(pool.normalize_token(wrapped)) == 108
	assert pool.normalize_token(" \t" + token + "\r\n ") == token


def test_normalize_rejects_an_empty_token():
	with pytest.raises(pool.PoolError):
		pool.normalize_token(" \n\t")


def test_accounts_come_from_secret_names_only():
	names = ["GH_PAT", "github_token", "CLAUDE_POOL_TOKEN_TEST2", "CLAUDE_POOL_TOKEN_TEST1",
		"CLAUDE_POOL_TOKEN_lower", "CLAUDE_POOL_TOKEN_", "XCLAUDE_POOL_TOKEN_A", "CLAUDE_POOL_TOKEN_FUN-1"]
	assert pool.list_accounts(names) == {"accounts": ["TEST1", "TEST2"], "excluded": []}


def test_accounts_accept_the_secrets_object_and_drop_excludes():
	secrets = {"CLAUDE_POOL_TOKEN_A": "x", "CLAUDE_POOL_TOKEN_B": "y", "GH_PAT": "z"}
	assert pool.list_accounts(secrets, ["B", "ZZ"]) == {"accounts": ["A"], "excluded": ["B"]}


def test_a_new_secret_joins_the_pool_with_no_other_change():
	# G2: adding an account is adding one secret.
	before = pool.list_accounts(["CLAUDE_POOL_TOKEN_A", "CLAUDE_POOL_TOKEN_B"])["accounts"]
	after = pool.list_accounts(["CLAUDE_POOL_TOKEN_A", "CLAUDE_POOL_TOKEN_B", "CLAUDE_POOL_TOKEN_C3"])["accounts"]
	assert after == before + ["C3"]
	probes = [_probe("A", 0.5, 0.5), _probe("B", 0.4, 0.6), _probe("C3", 0.1, 0.2)]
	assert pool.choose_account(probes, 0.9)["account"] == "C3"


def test_parse_excludes():
	assert pool.parse_excludes("test1, TEST2 test1") == ["TEST1", "TEST2"]
	assert pool.parse_excludes("") == []
	with pytest.raises(pool.PoolError):
		pool.parse_excludes("A;rm -rf /")


def test_accounts_cli_reads_names_on_stdin(monkeypatch, capsys):
	monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(["CLAUDE_POOL_TOKEN_B", "CLAUDE_POOL_TOKEN_A"])))
	assert pool.main(["accounts", "--exclude", "a", "--lines"]) == 0
	assert capsys.readouterr().out == "B\n"


def test_accounts_cli_fails_on_a_bad_exclude(monkeypatch):
	monkeypatch.setattr("sys.stdin", io.StringIO("[]"))
	assert pool.main(["accounts", "--exclude", "a$b"]) == 2


def test_normalize_cli_writes_only_the_token(monkeypatch, capsys):
	monkeypatch.setattr("sys.stdin", io.StringIO("abc\ndef\n"))
	assert pool.main(["normalize"]) == 0
	captured = capsys.readouterr()
	assert captured.out == "abcdef"
	assert "abcdef" not in captured.err


# --- probes ----------------------------------------------------------------


def test_probe_parse_reads_the_spike_rate_limit_event():
	record = pool.parse_probe(_jsonl(INIT_EVENT, RATE_LIMIT_EVENT, OK_RESULT), "TEST1")
	assert record == {
		"account": "TEST1",
		"five_hour": 0.14,
		"seven_day": 0.03,
		"five_hour_resets_at": 1790925000,
		"seven_day_resets_at": 1791504000,
		"resets_at": 1790925000,
		"status": "allowed",
		"error": None,
	}


def test_probe_parse_uses_the_last_rate_limit_event():
	record = pool.parse_probe(_jsonl(_rate_limit(0.1, 0.1), _rate_limit(0.3, 0.2), OK_RESULT), "A")
	assert (record["five_hour"], record["seven_day"]) == (0.3, 0.2)


def test_probe_parse_classifies_a_rejected_token():
	# S15: exit 1, result is_error true with the 401 text.
	record = pool.parse_probe(_jsonl(AUTH_RESULT), "TEST2", exit_code=1)
	assert record["error"] == "auth_failed"
	assert record["five_hour"] is None


def test_probe_parse_classifies_a_broken_probe():
	assert pool.parse_probe("", "A", exit_code=124)["error"] == "probe_failed"
	failed = {**OK_RESULT, "is_error": True, "result": "API Error: 500 overloaded"}
	assert pool.parse_probe(_jsonl(_rate_limit(0.1, 0.1), failed), "A", exit_code=1)["error"] == "probe_failed"


def test_probe_parse_keeps_a_limited_account_as_a_gated_reading():
	limited = {**OK_RESULT, "is_error": True, "result": "Claude AI usage limit reached"}
	record = pool.parse_probe(_jsonl(_rate_limit(1.0, 0.4, status="rejected"), limited), "A", exit_code=1)
	assert record["error"] is None
	assert record["status"] == "rejected"
	gated = pool.choose_account([record], 0.9)
	assert gated["outcome"] == "all_gated"


def test_probe_parse_without_a_rate_limit_event_is_unknown_utilization():
	record = pool.parse_probe(_jsonl(OK_RESULT), "A")
	assert record["error"] is None
	assert record["five_hour"] is None and record["seven_day"] is None


# --- choose ----------------------------------------------------------------


def test_choose_picks_the_lowest_max_utilization():
	probes = [_probe("A", 0.10, 0.80), _probe("B", 0.50, 0.40), _probe("C", 0.60, 0.05)]
	verdict = pool.choose_account(probes, 0.9)
	assert verdict["outcome"] == "selected"
	assert verdict["account"] == "B"
	assert verdict["utilization"] == 0.5


def test_choose_breaks_ties_alphabetically():
	probes = [_probe("ZED", 0.2, 0.2), _probe("ALPHA", 0.2, 0.1), _probe("MID", 0.1, 0.2)]
	assert pool.choose_account(probes, 0.9)["account"] == "ALPHA"


def test_choose_gate_is_inclusive_at_ninety_percent():
	probes = [_probe("A", 0.90, 0.1), _probe("B", 0.1, 0.95)]
	verdict = pool.choose_account(probes, 0.9)
	assert verdict["outcome"] == "all_gated"
	assert verdict["gated"] == ["A", "B"]
	assert pool.choose_account([_probe("A", 0.89, 0.1)], 0.9)["account"] == "A"


def test_choose_all_gated_reports_the_earliest_usable_time():
	probes = [
		# A is held by both windows: usable only after the later reset.
		_probe("A", 0.95, 0.92, five_reset=100, seven_reset=900),
		# B is held by its 5-hour window only.
		_probe("B", 0.95, 0.10, five_reset=500, seven_reset=50),
		# C is rejected without a reading over the gate: its resetsAt counts.
		_probe("C", 0.5, 0.5, status="rejected", five_reset=700, seven_reset=800),
	]
	verdict = pool.choose_account(probes, 0.9)
	assert verdict["outcome"] == "all_gated"
	assert verdict["resets_at"] == 500


def test_choose_all_gated_falls_back_to_the_top_level_reset():
	# Review round 3: an allowed probe gated by a window that reported no reset
	# of its own still reports the event's top-level resetsAt.
	over = {**_probe("A", 0.95, 0.10, five_reset=None, seven_reset=None), "resets_at": 400}
	verdict = pool.choose_account([over], 0.9)
	assert verdict["outcome"] == "all_gated"
	assert verdict["resets_at"] == 400
	# A window-specific reset still wins over the top-level one.
	held = {**_probe("B", 0.95, 0.10, five_reset=700), "resets_at": 300}
	assert pool.choose_account([held], 0.9)["resets_at"] == 700
	# No reset anywhere stays unknown.
	assert pool.choose_account([{**over, "resets_at": None}], 0.9)["resets_at"] is None


def test_choose_skips_failed_probes():
	probes = [_probe("A", None, None, error="auth_failed"), _probe("B", 0.7, 0.7), _probe("C", None, None, error="probe_failed")]
	verdict = pool.choose_account(probes, 0.9)
	assert verdict["account"] == "B"
	assert verdict["auth_failed"] == ["A"]
	assert verdict["probe_failed"] == ["C"]


def test_choose_uses_unknown_utilization_only_as_a_last_resort():
	unknown = _probe("A", None, None)
	assert pool.choose_account([unknown, _probe("B", 0.8, 0.8)], 0.9)["account"] == "B"
	verdict = pool.choose_account([unknown, _probe("B", 0.95, 0.1)], 0.9)
	assert verdict["outcome"] == "selected"
	assert verdict["account"] == "A"
	assert verdict["utilization"] is None


def test_choose_gates_one_window_over_the_gate_when_the_other_is_missing():
	# Review round 2: a known over-gate window must not fall back to "unknown".
	five_only = _probe("A", 0.95, None, five_reset=300)
	seven_only = _probe("B", None, 0.91, seven_reset=600)
	verdict = pool.choose_account([five_only, seven_only], 0.9)
	assert verdict["outcome"] == "all_gated"
	assert verdict["gated"] == ["A", "B"]
	assert verdict["resets_at"] == 300
	# A partial reading under the gate is still only a last resort.
	partial = _probe("C", 0.2, None)
	assert pool.choose_account([partial, _probe("D", 0.5, 0.5)], 0.9)["account"] == "D"
	verdict = pool.choose_account([five_only, partial], 0.9)
	assert verdict["account"] == "C"
	assert verdict["utilization"] is None


def test_choose_verdicts_when_nothing_is_usable():
	assert pool.choose_account([], 0.9)["outcome"] == "no_accounts"
	auth = [_probe("A", None, None, error="auth_failed")]
	assert pool.choose_account(auth, 0.9)["outcome"] == "auth_failed"
	broken = [_probe("A", None, None, error="probe_failed")]
	assert pool.choose_account(broken, 0.9)["outcome"] == "crashed"
	mixed = auth + [_probe("B", 0.99, 0.1)]
	assert pool.choose_account(mixed, 0.9)["outcome"] == "all_gated"


def test_choose_cli_reads_probe_lines_and_the_config_gate(tmp_path, capsys):
	probes = tmp_path / "probes.jsonl"
	probes.write_text("\n".join([json.dumps(_probe("A", 0.85, 0.1)), "not json", json.dumps({"account": "bad name"})]) + "\n")
	config = tmp_path / "pool.json"
	config.write_text(json.dumps({"gate_utilization": 0.8}))
	assert pool.main(["choose", "--probes", str(probes), "--config", str(config)]) == 0
	verdict = json.loads(capsys.readouterr().out)
	assert verdict["outcome"] == "all_gated"
	assert [probe["account"] for probe in verdict["probes"]] == ["A"]


# --- prompts ---------------------------------------------------------------


def test_issue_prompt_matches_the_pickup_dispatch():
	data = pool.build_prompt("issue", ISSUE_TEXT, REGISTRY)
	assert data["repo"] == "shubhodeep1/digital_pa"
	lines = data["prompt"].splitlines()
	url = "https://github.com/shubhodeep1/digital_pa/issues/42"
	assert lines[0] == f"/implement-issue-claude {url}"
	dispatch = DISPATCH_CMD.read_text(encoding="utf-8")
	assert lines[1].replace(url, "<url>") in dispatch


def test_pr_fix_prompt_matches_the_pickup_dispatch():
	data = pool.build_prompt("pr_fix", PR_FIX_TEXT, REGISTRY)
	lines = data["prompt"].splitlines()
	assert lines[0] == (
		f"/fix-claude-pr https://github.com/shubhodeep1/digital_pa/pull/7 — kind ci — head {HEAD} — claim sweep-run-123"
	)
	assert lines[1] in DISPATCH_CMD.read_text(encoding="utf-8")


def test_prompts_refuse_unregistered_repos_and_bad_payloads():
	with pytest.raises(pool.PoolError, match="not registered"):
		pool.build_prompt("issue", ISSUE_TEXT, ["shubhodeep1/coding-workflows"])
	with pytest.raises(pool.PoolError, match="invalid issue payload"):
		pool.build_prompt("issue", ISSUE_TEXT + "extra: line\n", REGISTRY)
	with pytest.raises(pool.PoolError, match="invalid pr_fix payload"):
		pool.build_prompt("pr_fix", PR_FIX_TEXT.replace("kind: ci", "kind: anything"), REGISTRY)
	with pytest.raises(pool.PoolError, match="unknown item type"):
		pool.build_prompt("other", ISSUE_TEXT, REGISTRY)
	# The registry match is case-insensitive, like the pickup's.
	assert pool.build_prompt("issue", ISSUE_TEXT, ["Shubhodeep1/Digital_PA"])["repo"] == "shubhodeep1/digital_pa"


def test_stage_prompt_waits_for_phase_three(monkeypatch):
	monkeypatch.delattr(pool.claude_issue_route, "parse_stage_text", raising=False)
	with pytest.raises(pool.PoolError, match="not supported"):
		pool.build_prompt("stage", "claude_stage.v1\n", REGISTRY, "Stage: phase 2/3")


def test_stage_prompt_uses_the_resume_block(monkeypatch):
	def parse_stage_text(text):
		return {"repo": "shubhodeep1/digital_pa", "plan": "docs/plans/x-plan.md"}

	monkeypatch.setattr(pool.claude_issue_route, "parse_stage_text", parse_stage_text, raising=False)
	data = pool.build_prompt("stage", "claude_stage.v1\n", REGISTRY, "Stage: phase 2/3   Checker observed: merged\n")
	lines = data["prompt"].splitlines()
	assert lines[0] == "/implement-plan-claude docs/plans/x-plan.md — resume."
	assert lines[1] == "Stage: phase 2/3   Checker observed: merged"
	assert "implement-plan-claude.md" in lines[2]
	with pytest.raises(pool.PoolError, match="no resume block"):
		pool.build_prompt("stage", "claude_stage.v1\n", REGISTRY, "  ")


def test_smoke_prompt_needs_no_payload():
	assert pool.build_prompt("smoke", "", []) == {"repo": "shubhodeep1/coding-workflows", "prompt": pool.SMOKE_PROMPT}


def test_decode_payload_is_strict():
	assert pool.decode_payload(_b64(ISSUE_TEXT)) == ISSUE_TEXT
	for bad in ("", "not base64!", _b64("x" * (pool.PAYLOAD_MAX_BYTES + 1)), base64.b64encode(b"\xff\xfe").decode()):
		with pytest.raises(pool.PoolError):
			pool.decode_payload(bad)


def test_prompt_cli(tmp_path, capsys):
	registry = tmp_path / "consumer_repos.json"
	registry.write_text(json.dumps(["shubhodeep1/digital_pa"]))
	assert pool.main(["prompt", "--item-type", "issue", "--payload-b64", _b64(ISSUE_TEXT), "--registry", str(registry)]) == 0
	assert json.loads(capsys.readouterr().out)["repo"] == "shubhodeep1/digital_pa"
	assert pool.main(["prompt", "--item-type", "issue", "--payload-b64", "@@", "--registry", str(registry)]) == 2


# --- run names -------------------------------------------------------------


def test_run_name_round_trip():
	name = pool.build_run_name("pr_fix", 6101, 2)
	assert name == "pool pr_fix q6101 a2"
	assert pool.parse_run_name(name) == {"item_type": "pr_fix", "queue_issue": 6101, "attempt": 2}


@pytest.mark.parametrize("name", ["pool issue q1", "pool other q1 a1", "pool issue q1 a0", "Pool issue q1 a1", "pool issue q1 a1 x", ""])
def test_run_name_parse_rejects_other_names(name):
	assert pool.parse_run_name(name) is None


def test_run_name_build_rejects_bad_values():
	for args in (("other", 1, 1), ("issue", -1, 1), ("issue", 1, 0), ("issue", 1, 1000)):
		with pytest.raises(pool.PoolError):
			pool.build_run_name(*args)


def test_run_name_cli(capsys):
	assert pool.main(["run-name", "--item-type", "smoke", "--queue-issue", "0", "--attempt", "1"]) == 0
	assert capsys.readouterr().out.strip() == "pool smoke q0 a1"
	assert pool.main(["run-name", "--parse", "nope"]) == 1


# --- classify --------------------------------------------------------------

SELECTED = {"outcome": "selected", "account": "TEST1"}
EXIT_OK = {"exit_code": 0, "timed_out": False}
EXIT_FAIL = {"exit_code": 1, "timed_out": False}


def test_classify_success():
	verdict = pool.classify_run(_jsonl(INIT_EVENT, RATE_LIMIT_EVENT, OK_RESULT), EXIT_OK, SELECTED, "success")
	assert verdict["outcome"] == "success"
	assert verdict["total_cost_usd"] == 0.016
	assert verdict["duration_ms"] == 1100
	assert verdict["rate_limit_info"]["unifiedWindows"]["five_hour"]["utilization"] == 0.14


def test_classify_the_spike_auth_error():
	verdict = pool.classify_run(_jsonl(AUTH_RESULT), EXIT_FAIL, SELECTED, "success")
	assert verdict["outcome"] == "auth_failed"


def test_classify_a_simulated_usage_limit_rejection():
	# Q7: no real rejection has been seen; any is_error result whose last
	# rate-limit info is rejected, or at 100%, is a usage limit.
	error = {**OK_RESULT, "is_error": True, "subtype": "error_during_execution", "result": "API Error: 429"}
	rejected = pool.classify_run(_jsonl(_rate_limit(0.5, 0.5, status="rejected"), error), EXIT_FAIL, SELECTED)
	assert (rejected["outcome"], rejected["reason"]) == ("usage_limit", "rate_limit_rejected")
	full = pool.classify_run(_jsonl(_rate_limit(1.0, 0.2), error), EXIT_FAIL, SELECTED)
	assert full["outcome"] == "usage_limit"
	text = {**error, "result": "Claude AI usage limit reached|1790953200"}
	assert pool.classify_run(_jsonl(_rate_limit(0.5, 0.5), text), EXIT_FAIL, SELECTED)["outcome"] == "usage_limit"


def test_classify_crashes():
	error = {**OK_RESULT, "is_error": True, "subtype": "error_max_turns", "result": "stopped"}
	verdict = pool.classify_run(_jsonl(_rate_limit(0.2, 0.2), error), EXIT_FAIL, SELECTED)
	assert (verdict["outcome"], verdict["reason"]) == ("crashed", "result_error_max_turns")
	no_result = pool.classify_run(_jsonl(INIT_EVENT, _rate_limit(0.2, 0.2)), {"exit_code": 137, "timed_out": False}, SELECTED)
	assert (no_result["outcome"], no_result["reason"]) == ("crashed", "no_result")
	missing = pool.classify_run("", None, SELECTED, "failure")
	assert (missing["outcome"], missing["reason"]) == ("crashed", "no_transcript")
	# A clean result with a non-zero exit is not a success.
	assert pool.classify_run(_jsonl(OK_RESULT), EXIT_FAIL, SELECTED)["outcome"] == "crashed"


def test_classify_timeouts():
	timed = pool.classify_run(_jsonl(INIT_EVENT), {"exit_code": 124, "timed_out": True}, SELECTED, "success")
	assert (timed["outcome"], timed["reason"]) == ("timeout", "cli_timeout")
	cancelled = pool.classify_run(_jsonl(INIT_EVENT), None, SELECTED, "cancelled")
	assert (cancelled["outcome"], cancelled["reason"]) == ("timeout", "job_cancelled")


def test_classify_takes_the_selection_verdict_when_nothing_ran():
	gated = pool.classify_run("", None, {"outcome": "all_gated", "account": "", "resets_at": 5}, "skipped")
	assert (gated["outcome"], gated["reason"]) == ("all_gated", "select")
	for outcome in ("no_accounts", "auth_failed", "crashed"):
		assert pool.classify_run("", None, {"outcome": outcome}, "skipped")["outcome"] == outcome
	assert pool.classify_run("", None, None, "skipped")["reason"] == "no_selection"
	assert pool.classify_run("", None, {"outcome": "bogus"}, "skipped")["outcome"] == "crashed"


def test_classify_a_job_that_failed_after_a_clean_transcript():
	# A smoke check (or any later step) failed: not a success.
	verdict = pool.classify_run(_jsonl(OK_RESULT), EXIT_OK, SELECTED, "failure")
	assert (verdict["outcome"], verdict["reason"]) == ("crashed", "work_job_failure")


def test_classify_cli_writes_the_result_json(tmp_path, capsys):
	transcript = tmp_path / "transcript.jsonl"
	transcript.write_text(_jsonl(RATE_LIMIT_EVENT, OK_RESULT))
	exit_info = tmp_path / "exit.json"
	exit_info.write_text(json.dumps(EXIT_OK))
	selection = tmp_path / "selection.json"
	selection.write_text(json.dumps({**SELECTED, "resets_at": None, "probes": [_probe("TEST1", 0.1, 0.1)]}))
	args = ["classify", "--transcript", str(transcript), "--exit-info", str(exit_info), "--selection", str(selection),
		"--work-result", "success", "--queue-issue", "6101", "--attempt", "2", "--item-type", "pr_fix"]
	assert pool.main(args) == 0
	result = json.loads(capsys.readouterr().out)
	for key in ("queue_issue", "attempt", "account", "outcome", "rate_limit_info", "total_cost_usd", "duration_ms"):
		assert key in result
	assert (result["queue_issue"], result["attempt"], result["account"], result["outcome"]) == (6101, 2, "TEST1", "success")
	assert result["outcome"] in pool.OUTCOMES


def test_classify_cli_with_missing_files(tmp_path, capsys):
	assert pool.main(["classify", "--transcript", str(tmp_path / "none"), "--exit-info", str(tmp_path / "none"),
		"--selection", str(tmp_path / "none")]) == 0
	assert json.loads(capsys.readouterr().out)["outcome"] == "crashed"


# --- config ----------------------------------------------------------------


def test_shipped_config_keeps_the_pool_off():
	raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
	assert raw == pool.DEFAULT_CONFIG
	config, invalid = pool.load_config(CONFIG_FILE)
	assert invalid == []
	assert config["dispatch_types"] == []


def test_missing_or_unreadable_config_means_pool_off(tmp_path):
	config, invalid = pool.load_config(tmp_path / "missing.json")
	assert config == pool.DEFAULT_CONFIG and invalid == []
	bad = tmp_path / "bad.json"
	bad.write_text("{not json")
	config, invalid = pool.load_config(bad)
	assert config["dispatch_types"] == [] and invalid == ["<unreadable>"]


def test_config_values_fall_back_to_defaults_when_invalid():
	raw = {
		"dispatch_types": ["pr_fix", "bogus", "pr_fix", "issue"],
		"gate_utilization": 1.5,
		"max_attempts": True,
		"timeout_minutes": {"issue": 500, "pr_fix": 60},
		"worker_effort": "turbo",
		"cli_version": "latest; rm -rf /",
		"worker_model": "Claude Opus",
		"handoff_author_login": "shubhodeep1",
		"verdict_bot_login": "bad login!",
		"retire_pickup": "yes",
		"unknown_key": 1,
	}
	config, invalid = pool.normalize_config(raw)
	assert config["dispatch_types"] == ["pr_fix", "issue"]
	assert config["gate_utilization"] == 0.9
	assert config["max_attempts"] == 3
	assert config["timeout_minutes"] == {"issue": 350, "stage": 350, "pr_fix": 60}
	assert config["worker_effort"] == "high"
	assert config["cli_version"] == "latest"
	assert config["worker_model"] == "claude-opus-5-5"
	assert config["handoff_author_login"] == "shubhodeep1"
	assert config["verdict_bot_login"] == ""
	assert config["retire_pickup"] is False
	assert set(invalid) >= {"gate_utilization", "max_attempts", "timeout_minutes.issue", "worker_effort", "cli_version",
		"worker_model", "verdict_bot_login", "retire_pickup", "dispatch_types['bogus']", "dispatch_types['pr_fix']"}
	assert "unknown_key" not in config


def test_config_cli_adds_the_item_timeouts(tmp_path, capsys):
	assert pool.main(["config", "--config", str(tmp_path / "none.json"), "--item-type", "issue"]) == 0
	config = json.loads(capsys.readouterr().out)
	assert (config["item_timeout_minutes"], config["job_timeout_minutes"]) == (350, 360)
	assert pool.main(["config", "--config", str(tmp_path / "none.json"), "--item-type", "smoke"]) == 0
	config = json.loads(capsys.readouterr().out)
	assert (config["item_timeout_minutes"], config["job_timeout_minutes"]) == (
		pool.SMOKE_TIMEOUT_MINUTES,
		pool.SMOKE_TIMEOUT_MINUTES + pool.SMOKE_CHECKS_MINUTES + 10,
	)


def test_smoke_job_limit_covers_the_cli_and_the_smoke_checks():
	# Review round 3: the smoke checks run after the CLI, so the job limit must
	# cover the CLI's limit (plus its kill-after) and both check runs.
	steps = _workflow()["jobs"]["work"]["steps"]
	smoke = next(step for step in steps if step.get("name", "").startswith("Smoke checks"))
	check_seconds = sum(int(value) for value in re.findall(r"timeout (\d+) claude", smoke["run"]))
	assert check_seconds == 900
	assert check_seconds <= pool.SMOKE_CHECKS_MINUTES * 60
	run_claude = next(step for step in steps if step.get("name") == "Run Claude")
	assert "--kill-after=60" in run_claude["run"]
	job_limit = pool.job_timeout_minutes(pool.SMOKE_TIMEOUT_MINUTES + pool.SMOKE_CHECKS_MINUTES)
	assert job_limit * 60 > pool.SMOKE_TIMEOUT_MINUTES * 60 + 60 + check_seconds


# --- redaction -------------------------------------------------------------


def test_redact_replaces_tokens_in_every_form():
	token = "sk-ant-oat01-" + "z" * 95
	pat = "ghp_" + "Q" * 36
	header = base64.b64encode(f"x-access-token:{pat}".encode()).decode()
	text = f"env CLAUDE_CODE_OAUTH_TOKEN={token}\nGH_TOKEN={pat}\nextraheader = AUTHORIZATION: basic {header}\n"
	redacted, count = pool.redact_text(text, [token[:80] + "\n" + token[80:], pat, "", "env"])
	assert count == 3
	assert token not in redacted and pat not in redacted and header not in redacted
	# Values under REDACT_MIN_LENGTH would match ordinary text and are left alone.
	assert redacted.startswith("env CLAUDE_CODE_OAUTH_TOKEN=***")


def test_redact_cli_rewrites_files_from_env(tmp_path, monkeypatch, capsys):
	secret = "secret-value-1234567890"
	target = tmp_path / "transcript.jsonl"
	target.write_text(json.dumps({"result": f"printed {secret}"}) + "\n")
	untouched = tmp_path / "clean.txt"
	untouched.write_text("nothing here\n")
	monkeypatch.setenv("POOL_TEST_SECRET", secret)
	assert pool.main(["redact", "--env-var", "POOL_TEST_SECRET", "--env-var", "POOL_TEST_UNSET", str(target), str(untouched), str(tmp_path / "missing")]) == 0
	assert secret not in target.read_text()
	assert "***" in target.read_text()
	assert secret not in capsys.readouterr().err


@pytest.mark.parametrize("prefix", ["", "x", "xy", "Authorization: Bearer "])
@pytest.mark.parametrize("encode", [base64.b64encode, base64.urlsafe_b64encode])
def test_redact_catches_base64_at_every_offset(prefix, encode):
	# A worker can print base64(secret), or base64 of a longer text holding it.
	token = "sk-ant-oat01-" + "Ab0_-" * 19
	encoded = encode(f"{prefix}{token} trailing".encode()).decode()
	redacted, count = pool.redact_text(f"output: {encoded}\n", [token])
	assert count >= 1
	assert "***" in redacted
	plain = encode(token.encode()).decode().rstrip("=")
	assert plain[:-1] not in redacted


@pytest.mark.parametrize("length", [24, 25, 26])
@pytest.mark.parametrize("encode", [base64.b64encode, base64.urlsafe_b64encode])
def test_redact_removes_every_character_of_a_standalone_base64_secret(length, encode):
	# Review round 2: for byte lengths not divisible by 3 the last character
	# of base64(secret) also carries secret bits and must not survive.
	token = ("sk-ant-oat01-" + "Ab0_-" * 10)[:length]
	encoded = encode(token.encode()).decode()
	for printed in (encoded, encoded.rstrip("=")):
		redacted, count = pool.redact_text(f"output: {printed} done\n", [token])
		assert count == 1
		assert redacted == "output: *** done\n"


def test_redact_base64_forms_leave_ordinary_text_alone():
	text = "The run finished; see the log for details. " + base64.b64encode(b"unrelated payload").decode()
	assert pool.redact_text(text, ["secret-value-1234567890"]) == (text, 0)


def test_redact_cli_fails_closed_on_a_file_it_cannot_read(tmp_path, monkeypatch, capsys):
	monkeypatch.setenv("POOL_TEST_SECRET", "secret-value-1234567890")
	folder = tmp_path / "not-a-file"
	folder.mkdir()
	assert pool.main(["redact", "--env-var", "POOL_TEST_SECRET", str(folder)]) == 1
	assert "redact_failed" in capsys.readouterr().err


# --- workflow wiring -------------------------------------------------------


def _workflow():
	return yaml.safe_load(WORKER_WF.read_text(encoding="utf-8"))


def test_worker_workflow_is_only_callable():
	data = _workflow()
	triggers = data.get(True) or data.get("on")
	assert set(triggers) == {"workflow_call"}
	inputs = triggers["workflow_call"]["inputs"]
	assert {"queue_issue", "item_type", "attempt", "exclude_accounts", "payload_b64"} <= set(inputs)
	assert data["permissions"] == {"contents": "read"}
	assert list(data["jobs"]) == ["select", "work", "report"]
	assert data["jobs"]["work"]["needs"] == "select"
	assert data["jobs"]["report"]["if"] == "always()"


def test_worker_workflow_calls_the_pool_script_for_every_decision():
	text = WORKER_WF.read_text(encoding="utf-8")
	for command in ("config", "accounts", "normalize", "probe-parse", "choose", "prompt", "classify", "redact"):
		assert f"claude_pool.py {command}" in text, command
	for flag in ("--permission-mode auto", "--settings", "--mcp-config", "--output-format stream-json", "--effort"):
		assert flag in text
	assert "hasTrustDialogAccepted" in text
	assert "claude-pool-transcript" in text and "claude-pool-result" in text
	assert "CLAUDE_POOL_WORKER: '1'" in text


def test_worker_workflow_never_echoes_a_secret():
	text = WORKER_WF.read_text(encoding="utf-8")
	# Secrets reach run bodies only through env:.
	for line in text.splitlines():
		stripped = line.strip()
		if "secrets" in stripped and "${{" in stripped:
			assert re.match(r"^[A-Z_]+: \$\{\{ (secrets\.|secrets\[|toJSON\(secrets\))", stripped) or stripped.startswith("token: ${{ secrets.GH_PAT }}"), stripped
	assert "toJSON(secrets)" in text
	assert text.count("toJSON(secrets)") == 1
	for name in ("CLAUDE_POOL_RAW_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ALL_SECRETS_JSON", "CLAUDE_POOL_GH_PAT", "GH_TOKEN"):
		assert not re.search(rf"echo [^|\n]*\${name}\b", text.replace("::add-mask::$token", "")), name
	assert text.count("::add-mask::$token") == 2


def test_probes_get_only_their_own_token_and_fit_the_select_timeout():
	data = _workflow()
	select = data["jobs"]["select"]
	probe = next(step for step in select["steps"] if step.get("name") == "Probe every pool account")
	assert "env -u ALL_SECRETS_JSON CLAUDE_CODE_OAUTH_TOKEN=" in probe["run"]
	assert 'timeout "$probe_timeout" claude' in probe["run"]
	timeout = int(re.search(r"probe_timeout=(\d+)", probe["run"]).group(1))
	budget = int(re.search(r"deadline=\$\(\(SECONDS \+ (\d+)\)\)", probe["run"]).group(1))
	# The last probe starts before the deadline and ends within its cap, with
	# room left for checkout, CLI install and choose.
	assert budget + timeout <= (select["timeout-minutes"] - 2) * 60
	assert "reason=select_deadline" in probe["run"]


def test_redaction_runs_from_a_fresh_checkout_and_gates_the_upload():
	data = _workflow()
	steps = data["jobs"]["work"]["steps"]
	names = [step.get("name") for step in steps]
	run_claude = names.index("Run Claude")
	fresh = names.index("Check out the redaction helper again")
	redact = names.index("Redact secrets from the transcript")
	upload = names.index("Upload the transcript")
	assert run_claude < fresh < redact < upload
	checkout = steps[fresh]
	assert checkout["if"] == "always()"
	assert checkout["with"]["ref"] == "${{ needs.select.outputs.pool_sha }}"
	assert checkout["with"]["path"] == "pool-redact"
	assert checkout["with"]["persist-credentials"] is False
	assert steps[redact]["id"] == "redact"
	assert "set -euo pipefail" in steps[redact]["run"]
	assert "python3 pool-redact/scripts/claude_pool.py redact" in steps[redact]["run"]
	assert steps[upload]["if"] == "always() && steps.redact.outcome == 'success'"
	assert data["jobs"]["select"]["outputs"]["pool_sha"] == "${{ steps.config.outputs.pool_sha }}"


def test_work_and_report_check_out_the_select_jobs_commit():
	# Review round 3: pool_ref is resolved once, by the select job; the later
	# jobs never re-resolve it, so one run never mixes two pool revisions.
	data = _workflow()
	refs = {}
	for job in ("select", "work", "report"):
		step = next(s for s in data["jobs"][job]["steps"] if s.get("name") == "Check out the pool scripts (coding-workflows)")
		refs[job] = step["with"]["ref"]
	assert refs["select"] == "${{ inputs.pool_ref }}"
	assert refs["work"] == "${{ needs.select.outputs.pool_sha }}"
	assert refs["report"] == "${{ needs.select.outputs.pool_sha || inputs.pool_ref }}"
	assert "git -C pool rev-parse HEAD" in next(s for s in data["jobs"]["select"]["steps"] if s.get("id") == "config")["run"]


def test_only_the_worker_workflow_names_pool_tokens():
	# G8: no other coding-workflows workflow reads a CLAUDE_POOL_TOKEN_* secret;
	# the worker workflow is workflow_call only, so it runs in the runner repo.
	offenders = [
		path.name
		for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
		if path.name != WORKER_WF.name and "CLAUDE_POOL_TOKEN" in path.read_text(encoding="utf-8")
	]
	assert offenders == []
	templates = ROOT / "workflow-templates"
	if templates.is_dir():
		assert [p for p in templates.rglob("*.y*ml") if "CLAUDE_POOL_TOKEN" in p.read_text(encoding="utf-8")] == []


def test_worker_test_suite_runs_in_ci():
	ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
	assert "tests/test_claude_pool.py" in ci
