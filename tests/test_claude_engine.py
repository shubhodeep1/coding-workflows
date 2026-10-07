#!/usr/bin/env python3
"""Unit tests for scripts/claude_engine.py (plan Phase 3, D1–D3).

Covers engine resolution (labels, per-role and global variables, defaults,
the D2 precedence), the D3 model/effort mapping, config normalisation,
transcript extraction and classification (spike evidence S7, S15),
probe parsing and the least-used account order, and the smoke-run inspector.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "claude_engine.py"
CONFIG = REPO_ROOT / ".github" / "ai" / "claude_engine.json"


def _load():
	spec = importlib.util.spec_from_file_location("claude_engine", SCRIPT)
	module = importlib.util.module_from_spec(spec)
	sys.modules["claude_engine"] = module
	spec.loader.exec_module(module)
	return module


ce = _load()


def _run(*args: str, env: dict[str, str] | None = None, stdin: str = "") -> subprocess.CompletedProcess:
	# CI jobs carry a real GITHUB_EVENT_PATH whose labels would leak in.
	full_env = {key: value for key, value in os.environ.items() if not key.startswith(("AI_ENGINE", "GITHUB_EVENT_PATH"))}
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	full_env.update(env or {})
	return subprocess.run(
		[sys.executable, str(SCRIPT), *args],
		input=stdin,
		capture_output=True,
		text=True,
		env=full_env,
		check=False,
	)


def _jsonl(*events: dict) -> str:
	return "".join(json.dumps(event) + "\n" for event in events)


# --- config ------------------------------------------------------------------

# Roles whose checked-in default is Claude, by cutover phase.
CUTOVER_ROLES = {"CLARIFY", "CLARIFY_RESPOND", "PLAN"}  # Phase 5a
CUTOVER_ROLES |= {"IMPLEMENT", "IMPLEMENT_REPAIR", "IMPLEMENT_DIAGNOSE"}  # Phase 5b


def test_checked_in_config_is_valid_and_inert() -> None:
	raw = json.loads(CONFIG.read_text(encoding="utf-8"))
	config, invalid = ce.normalize_config(raw)
	assert invalid == []
	assert set(config["role_defaults"]) == set(ce.ROLES)
	# Roles move to Claude only in their own cutover phase (plan Phase 5a-5d).
	on_claude = {role for role, fields in config["role_defaults"].items() if fields["engine"] == "claude"}
	assert on_claude == CUTOVER_ROLES
	assert config["broker_url"] == "https://claude-pool-broker.shubhodeep.workers.dev/v1/pool"
	assert config["gate_utilization"] == 0.9
	assert config["probe_model"] == "claude-haiku-4-5-20251001"
	assert config["oidc_audience"] == "coding-workflows-claude-pool"


def test_checked_in_config_matches_code_defaults() -> None:
	# Only the broker URL and the cut-over roles' engine differ: a missing
	# config file means no broker and every role on codex.
	raw = json.loads(CONFIG.read_text(encoding="utf-8"))
	config, _ = ce.normalize_config(raw)
	expected = json.loads(json.dumps(ce.DEFAULT_CONFIG))
	for role in CUTOVER_ROLES:
		expected["role_defaults"][role]["engine"] = "claude"
	assert {**config, "broker_url": ""} == expected


@pytest.mark.parametrize(
	"url, ok",
	[
		("https://claude-pool-broker.shubhodeep.workers.dev/v1/pool", True),
		("http://127.0.0.1:8080/v1/pool", True),
		("http://localhost/v1/pool", True),
		("https://attacker.example/v1/pool", False),
		("https://claude-pool-broker.shubhodeep.workers.dev/other", False),
		("http://claude-pool-broker.example/v1/pool", False),
		("https://evil.example/v1/pool?x=1", False),
		("ftp://x/y", False),
	],
)
def test_broker_url_must_be_https_or_loopback(url: str, ok: bool) -> None:
	_, invalid = ce.normalize_config({"broker_url": url})
	assert ("broker_url" not in invalid) is ok


def test_utility_roles_use_sonnet_and_the_rest_opus() -> None:
	config, _ = ce.normalize_config(None)
	for role in ce.ROLES:
		expected = "claude-sonnet-5-5" if role in ce.UTILITY_ROLES else "claude-opus-5-5"
		assert config["role_defaults"][role]["claude_model"] == expected, role
	assert set(config["utility_roles"]) == {"LOG_SUMMARY", "RETRO", "MATERIALITY", "SUMMARISER", "BEHAVIOURAL_SMOKE"}


def test_missing_config_is_every_role_on_codex(tmp_path: Path) -> None:
	config, invalid = ce.load_config(tmp_path / "absent.json")
	assert invalid == []
	assert all(fields["engine"] == "codex" for fields in config["role_defaults"].values())


def test_unreadable_config_falls_back_to_defaults(tmp_path: Path) -> None:
	path = tmp_path / "bad.json"
	path.write_text("{not json", encoding="utf-8")
	config, invalid = ce.load_config(path)
	assert invalid == ["<unreadable>"]
	assert config == ce.DEFAULT_CONFIG


@pytest.mark.parametrize(
	"raw, key",
	[
		({"cli_version": "latest"}, "cli_version"),
		({"broker_url": "http://insecure.example"}, "broker_url"),
		({"gate_utilization": 1.5}, "gate_utilization"),
		({"gate_utilization": True}, "gate_utilization"),
		({"probe_model": "Haiku!"}, "probe_model"),
		({"default_effort": "extreme"}, "default_effort"),
		({"hide_claude_md": "yes"}, "hide_claude_md"),
		({"utility_roles": ["NOT_A_ROLE"]}, "utility_roles"),
		({"role_defaults": {"PLAN": {"engine": "gpt"}}}, "role_defaults.PLAN.engine"),
		({"role_defaults": {"PLAN": {"claude_model": "gpt-5"}}}, "role_defaults.PLAN.claude_model"),
		({"role_defaults": {"PLAN": {"profile": "admin"}}}, "role_defaults.PLAN.profile"),
		({"role_defaults": {"NOPE": {"engine": "claude"}}}, "role_defaults.NOPE"),
	],
)
def test_invalid_config_values_keep_their_default(raw: dict, key: str) -> None:
	config, invalid = ce.normalize_config(raw)
	assert key in invalid
	assert config["cli_version"] == ce.DEFAULT_CONFIG["cli_version"]
	assert config["role_defaults"]["PLAN"] == ce.DEFAULT_CONFIG["role_defaults"]["PLAN"]


def test_role_defaults_merge_per_field() -> None:
	config, invalid = ce.normalize_config({"role_defaults": {"PLAN": {"engine": "claude"}}})
	assert invalid == []
	assert config["role_defaults"]["PLAN"] == {"engine": "claude", "claude_model": "claude-opus-5-5", "profile": "write"}
	assert config["role_defaults"]["IMPLEMENT"]["engine"] == "codex"


# --- resolution (D1–D3) ------------------------------------------------------


def _resolve(role: str, env: dict[str, str], config: dict | None = None, **hints: str) -> dict:
	return ce.resolve_role(role, config or ce.normalize_config(None)[0], env, **hints)


def test_code_default_is_codex() -> None:
	resolved = _resolve("IMPLEMENT", {})
	assert (resolved["engine"], resolved["source"]) == ("codex", "default")


def test_config_default_can_select_claude() -> None:
	config, _ = ce.normalize_config({"role_defaults": {"PLAN": {"engine": "claude"}}})
	assert _resolve("PLAN", {}, config)["engine"] == "claude"


def test_global_variable_overrides_default() -> None:
	resolved = _resolve("PLAN", {"AI_ENGINE": "Claude"})
	assert (resolved["engine"], resolved["source"]) == ("claude", "var:AI_ENGINE")


def test_role_variable_beats_global_variable() -> None:
	resolved = _resolve("PLAN", {"AI_ENGINE": "claude", "AI_ENGINE_PLAN": "codex"})
	assert (resolved["engine"], resolved["source"]) == ("codex", "var:AI_ENGINE_PLAN")


def test_invalid_role_variable_is_skipped_with_a_warning() -> None:
	resolved = _resolve("PLAN", {"AI_ENGINE_PLAN": "gpt", "AI_ENGINE": "claude"})
	assert (resolved["engine"], resolved["source"]) == ("claude", "var:AI_ENGINE")
	assert resolved["warnings"] and "AI_ENGINE_PLAN" in resolved["warnings"][0]


def test_engine_claude_label_beats_variables_and_forces_opus_high() -> None:
	resolved = _resolve(
		"RETRO",
		{"AI_ENGINE_LABELS": "bug, ai:engine-claude", "AI_ENGINE_RETRO": "codex"},
		model_hint="claude-sonnet-5-5",
		effort_hint="low",
	)
	assert resolved["engine"] == "claude"
	assert resolved["source"] == "label:ai:engine-claude"
	assert (resolved["model"], resolved["effort"]) == ("claude-opus-5-5", "high")


def test_codex_label_wins_over_engine_claude_label() -> None:
	resolved = _resolve("PLAN", {"AI_ENGINE_LABELS": '[{"name": "ai:engine-claude"}, {"name": "ai:codex"}]', "AI_ENGINE": "claude"})
	assert (resolved["engine"], resolved["source"]) == ("codex", "label:ai:codex")


def test_labels_are_case_insensitive_and_accept_newlines() -> None:
	resolved = _resolve("PLAN", {"AI_ENGINE_LABELS": "AI:Engine-Claude\nother"})
	assert resolved["engine"] == "claude"


@pytest.mark.parametrize(
	"hint, expected",
	[
		("claude-sonnet-5-5", "claude-sonnet-5-5"),
		("openai/gpt-6-sol", "claude-opus-5-5"),
		("gpt-5.5", "claude-opus-5-5"),
		("", "claude-opus-5-5"),
		("claude-Bad Model", "claude-opus-5-5"),
	],
)
def test_model_hint_is_used_only_when_it_is_a_claude_model(hint: str, expected: str) -> None:
	assert _resolve("PLAN", {}, model_hint=hint)["model"] == expected


def test_utility_role_defaults_to_sonnet() -> None:
	assert _resolve("SUMMARISER", {})["model"] == "claude-sonnet-5-5"


@pytest.mark.parametrize(
	"hint, expected",
	[
		("none", "low"),
		("minimal", "low"),
		("low", "low"),
		("medium", "medium"),
		("high", "high"),
		("xhigh", "xhigh"),
		("max", "max"),
		("MEDIUM", "medium"),
		("", "high"),
		("bogus", "high"),
	],
)
def test_effort_mapping(hint: str, expected: str) -> None:
	assert _resolve("PLAN", {}, effort_hint=hint)["effort"] == expected


def test_read_roles_get_the_read_profile() -> None:
	for role in ce.ROLES:
		expected = "read" if role in ce.READ_ROLES else "write"
		assert _resolve(role, {})["profile"] == expected, role


def test_unknown_role_is_rejected() -> None:
	with pytest.raises(ce.EngineError):
		_resolve("NOT_A_ROLE", {})


def test_resolve_cli_prints_one_field_and_warns() -> None:
	result = _run("resolve", "--role", "PLAN", "--field", "engine", env={"AI_ENGINE": "claude", "AI_ENGINE_PLAN": "nope"})
	assert result.returncode == 0
	assert result.stdout.strip() == "claude"
	assert "::warning::AI engine: invalid AI_ENGINE_PLAN" in result.stderr


def test_resolve_cli_rejects_unknown_role() -> None:
	result = _run("resolve", "--role", "NOPE")
	assert result.returncode == 2


# --- transcripts ---------------------------------------------------------------

SUCCESS = _jsonl(
	{"type": "system", "subtype": "init"},
	{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 10}}},
	{
		"type": "result",
		"subtype": "success",
		"is_error": False,
		"result": "final answer\n",
		"total_cost_usd": 0.12,
		"duration_ms": 900,
		"num_turns": 1,
		"usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50},
		"modelUsage": {"claude-opus-5-5": {"inputTokens": 10, "outputTokens": 5, "costUSD": 0.12}},
		"session_id": "x",
	},
)
# Spike S15: a rejected OAuth token.
AUTH_FAILED = _jsonl(
	{"type": "result", "subtype": "success", "is_error": True, "result": "Failed to authenticate. API Error: 401 OAuth access token is invalid."},
)
# Spike S7 shape, with the status a rejected request carries.
RATE_REJECTED = _jsonl(
	{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": 1760000000}}}},
	{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "API Error"},
)
WINDOW_FULL = _jsonl(
	{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {"seven_day": {"utilization": 1.0}}}},
	{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "stopped"},
)
LIMIT_TEXT = _jsonl({"type": "result", "subtype": "success", "is_error": True, "result": "Claude AI usage limit reached|1760000000"})
MAX_TURNS = _jsonl({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""})


def test_extract_writes_result_text_and_returns_usage(tmp_path: Path) -> None:
	transcript = tmp_path / "t.jsonl"
	transcript.write_text("not json\n" + SUCCESS, encoding="utf-8")
	out = tmp_path / "out.txt"
	result = _run("extract", "--transcript", str(transcript), "--out", str(out))
	assert result.returncode == 0
	assert out.read_text(encoding="utf-8") == "final answer\n"
	usage = json.loads(result.stdout)
	assert usage["type"] == "result"
	assert usage["total_cost_usd"] == 0.12
	assert usage["usage"]["output_tokens"] == 5
	assert "result" not in usage
	assert "session_id" not in usage


def test_extract_without_result_fails_and_writes_nothing(tmp_path: Path) -> None:
	transcript = tmp_path / "t.jsonl"
	transcript.write_text(_jsonl({"type": "system"}), encoding="utf-8")
	out = tmp_path / "out.txt"
	result = _run("extract", "--transcript", str(transcript), "--out", str(out))
	assert result.returncode == 1
	assert not out.exists()


@pytest.mark.parametrize(
	"text, exit_code, outcome, reason",
	[
		(SUCCESS, 0, "success", "result_success"),
		(SUCCESS, None, "success", "result_success"),
		(SUCCESS, 1, "crashed", "result_success"),
		(AUTH_FAILED, 1, "auth_failed", "result_auth_error"),
		(RATE_REJECTED, 1, "usage_limit", "rate_limit_rejected"),
		(WINDOW_FULL, 1, "usage_limit", "usage_limit"),
		(LIMIT_TEXT, 1, "usage_limit", "usage_limit"),
		(MAX_TURNS, 1, "crashed", "result_error_max_turns"),
		("", 1, "crashed", "no_transcript"),
		(_jsonl({"type": "system"}), 1, "crashed", "no_result"),
		(SUCCESS, 124, "timeout", "exit_124"),
		("", 137, "timeout", "exit_137"),
	],
)
def test_classify(text: str, exit_code: int | None, outcome: str, reason: str) -> None:
	verdict = ce.classify(text, exit_code)
	assert (verdict["outcome"], verdict["reason"]) == (outcome, reason)
	assert verdict["outcome"] in ce.OUTCOMES


def test_classify_cli_missing_transcript_is_crashed(tmp_path: Path) -> None:
	result = _run("classify", "--transcript", str(tmp_path / "absent.jsonl"), "--exit-code", "1")
	assert result.returncode == 0
	assert json.loads(result.stdout)["outcome"] == "crashed"


def test_inspect_reports_startup_tokens_and_denials() -> None:
	text = _jsonl(
		{
			"type": "assistant",
			"message": {
				"usage": {"input_tokens": 3, "cache_creation_input_tokens": 12000, "cache_read_input_tokens": 500},
				"content": [
					{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "gh pr merge 1 --squash"}},
					{"type": "tool_use", "id": "b", "name": "Write", "input": {"file_path": "/w/.github/workflows/x.yml"}},
					{"type": "tool_use", "id": "c", "name": "Read", "input": {"file_path": "/w/README.md"}},
				],
			},
		},
		{"type": "assistant", "message": {"usage": {"input_tokens": 99999}, "content": []}},
		{
			"type": "user",
			"message": {
				"content": [
					{"type": "tool_result", "tool_use_id": "a", "is_error": True, "content": "Permission to use Bash has been denied."},
					{"type": "tool_result", "tool_use_id": "b", "is_error": True, "content": [{"type": "text", "text": "denied by rule"}]},
					{"type": "tool_result", "tool_use_id": "c", "content": "ok"},
				]
			},
		},
	)
	facts = ce.inspect_transcript(text)
	assert facts["startup_input_tokens"] == 12503
	assert [(c["tool"], c["is_error"]) for c in facts["tool_calls"]] == [("Bash", True), ("Write", True), ("Read", False)]
	assert facts["tool_calls"][0]["target"] == "gh pr merge 1 --squash"
	assert facts["tool_calls"][1]["result"] == "denied by rule"


# --- probes and account choice -----------------------------------------------


def _probe_text(five: float | None, seven: float | None, *, status: str = "allowed", ok: bool = True) -> str:
	windows = {}
	if five is not None:
		windows["five_hour"] = {"utilization": five, "resetsAt": 1000}
	if seven is not None:
		windows["seven_day"] = {"utilization": seven, "resetsAt": 2000}
	return _jsonl(
		{"type": "rate_limit_event", "rate_limit_info": {"status": status, "unifiedWindows": windows}},
		{"type": "result", "subtype": "success", "is_error": not ok, "result": "OK"},
	)


def test_probe_parse_reads_both_windows() -> None:
	record = ce.parse_probe(_probe_text(0.2, 0.5), "A")
	assert (record["five_hour"], record["seven_day"], record["error"]) == (0.2, 0.5, None)


def test_probe_parse_auth_failure() -> None:
	assert ce.parse_probe(AUTH_FAILED, "A", 1)["error"] == "auth_failed"


def test_probe_parse_limited_account_is_rejected_not_failed() -> None:
	record = ce.parse_probe(RATE_REJECTED, "A", 1)
	assert (record["error"], record["status"]) == (None, "rejected")


def test_probe_parse_cli() -> None:
	result = _run("probe-parse", "--account", "B", stdin=_probe_text(0.1, 0.3))
	assert result.returncode == 0
	assert json.loads(result.stdout)["account"] == "B"


def test_choose_orders_by_least_used_with_alphabetical_ties() -> None:
	probes = [
		ce.parse_probe(_probe_text(0.5, 0.5), "C"),
		ce.parse_probe(_probe_text(0.2, 0.4), "B"),
		ce.parse_probe(_probe_text(0.4, 0.1), "A"),
		ce.parse_probe(_probe_text(None, None), "D"),
	]
	verdict = ce.choose_account(probes, 0.9)
	assert verdict["outcome"] == "selected"
	assert verdict["order"] == ["A", "B", "C", "D"]
	assert verdict["account"] == "A"
	assert verdict["utilization"] == 0.4


def test_choose_gates_accounts_at_or_over_the_gate() -> None:
	probes = [
		ce.parse_probe(_probe_text(0.95, 0.1), "A"),
		ce.parse_probe(_probe_text(0.1, None), "B"),
		ce.parse_probe(_probe_text(0.9, None), "C"),
	]
	verdict = ce.choose_account(probes, 0.9)
	assert verdict["order"] == ["B"]
	assert verdict["gated"] == ["A", "C"]


def test_choose_all_gated_reports_the_earliest_reset() -> None:
	probes = [ce.parse_probe(_probe_text(0.95, 0.1), "A"), ce.parse_probe(_probe_text(0.2, 0.99), "B")]
	verdict = ce.choose_account(probes, 0.9)
	assert verdict["outcome"] == "all_gated"
	assert verdict["resets_at"] == 1000
	assert verdict["order"] == []


@pytest.mark.parametrize(
	"probes, outcome",
	[
		([], "no_accounts"),
		([{"account": "A", "error": "auth_failed"}], "auth_failed"),
		([{"account": "A", "error": "probe_failed"}], "crashed"),
	],
)
def test_choose_without_a_usable_account(probes: list, outcome: str) -> None:
	assert ce.choose_account(probes, 0.9)["outcome"] == outcome


def test_choose_cli_uses_config_gate_and_rejects_bad_input() -> None:
	probes = [ce.parse_probe(_probe_text(0.85, 0.1), "A")]
	result = _run("choose", stdin=json.dumps(probes))
	assert json.loads(result.stdout)["account"] == "A"
	result = _run("choose", "--gate", "0.8", stdin=json.dumps(probes))
	assert json.loads(result.stdout)["outcome"] == "all_gated"
	assert _run("choose", stdin="{}").returncode == 2
	assert _run("choose", "--gate", "2", stdin="[]").returncode == 2


# --- trust and support files ---------------------------------------------------


def test_trust_marks_the_workdir_and_keeps_other_keys(tmp_path: Path) -> None:
	home = tmp_path / "home"
	home.mkdir()
	(home / ".claude.json").write_text(json.dumps({"keep": 1, "projects": {"/other": {"x": True}}}), encoding="utf-8")
	work = tmp_path / "work"
	work.mkdir()
	result = _run("trust", "--workdir", str(work), "--home", str(home))
	assert result.returncode == 0
	data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
	assert data["keep"] == 1
	assert data["projects"]["/other"] == {"x": True}
	assert data["projects"][str(work.resolve())]["hasTrustDialogAccepted"] is True
	assert oct((home / ".claude.json").stat().st_mode & 0o777) == "0o600"


def test_trust_replaces_an_unreadable_file(tmp_path: Path) -> None:
	(tmp_path / ".claude.json").write_text("[1, 2", encoding="utf-8")
	ce.trust_workdir(tmp_path, "/w")
	assert json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8")) == {"projects": {"/w": {"hasTrustDialogAccepted": True}}}


def test_support_files_come_from_the_trusted_roots_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	# A caller checkout's own copy (cwd) is never searched.
	(tmp_path / ".github" / "ai").mkdir(parents=True)
	(tmp_path / ".github" / "ai" / "claude_engine.json").write_text('{"role_defaults": {"PLAN": {"engine": "claude"}}}', encoding="utf-8")
	monkeypatch.chdir(tmp_path)
	monkeypatch.delenv("SUPPORT_ROOT_DIR", raising=False)
	monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
	assert ce.find_support_file(ce.CONFIG_RELATIVE) == CONFIG
	result = _run("support-file", "--name", "guard-hook", env={"SUPPORT_ROOT_DIR": "", "GITHUB_WORKSPACE": ""})
	assert result.returncode == 0
	assert result.stdout.strip().endswith(".claude/hooks/gh_api_write_guard.py")


def test_support_root_takes_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	(tmp_path / ".github" / "ai").mkdir(parents=True)
	staged = tmp_path / ".github" / "ai" / "claude_engine.json"
	staged.write_text('{"role_defaults": {"PLAN": {"engine": "claude"}}}', encoding="utf-8")
	monkeypatch.setenv("SUPPORT_ROOT_DIR", str(tmp_path))
	config, _ = ce.load_config()
	assert config["role_defaults"]["PLAN"]["engine"] == "claude"


# --- cost_audit.py reads the usage line ------------------------------------------


def test_cost_audit_parses_the_usage_line(tmp_path: Path) -> None:
	spec = importlib.util.spec_from_file_location("cost_audit_for_engine", REPO_ROOT / "scripts" / "cost_audit.py")
	cost_audit = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(cost_audit)
	transcript = tmp_path / "t.jsonl"
	transcript.write_text(SUCCESS, encoding="utf-8")
	usage_line = _run("extract", "--transcript", str(transcript), "--out", str(tmp_path / "o")).stdout.strip()
	log = "\n".join(
		[
			f"2026-10-04T01:00:01.0000000Z {usage_line}",
			f"2026-10-04T01:00:02.0000000Z {usage_line}",
			'2026-10-04T01:00:03.0000000Z {"type": "result", "note": "no usage object"}',
			"2026-10-04T01:00:04.0000000Z not json {\"type\": \"result\"",
		]
	)
	parsed = cost_audit.parse_log(log)
	assert parsed["codex_tokens_used"] == 0
	assert parsed["claude_calls"] == 2
	assert parsed["claude_input_tokens"] == 20
	assert parsed["claude_output_tokens"] == 10
	assert parsed["claude_cache_read_tokens"] == 200
	assert parsed["claude_cache_write_tokens"] == 100
	assert parsed["claude_cost_usd"] == 0.24
	assert parsed["claude_models"]["claude-opus-5-5"]["calls"] == 2
	assert parsed["claude_models"]["claude-opus-5-5"]["cost_usd"] == 0.24


def test_cost_audit_ignores_non_finite_costs() -> None:
	spec = importlib.util.spec_from_file_location("cost_audit_costs", REPO_ROOT / "scripts" / "cost_audit.py")
	cost_audit = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(cost_audit)
	line = json.dumps({"type": "result", "usage": {}, "total_cost_usd": "Infinity"})
	assert cost_audit.parse_log(line)["claude_cost_usd"] == 0.0
	line = json.dumps({"type": "result", "usage": {}, "total_cost_usd": -3})
	assert cost_audit.parse_log(line)["claude_cost_usd"] == 0.0


# --- Phase 6: labels from the event payload --------------------------------------


def test_labels_come_from_the_event_payload_when_unset(tmp_path: Path) -> None:
	event = tmp_path / "event.json"
	event.write_text(json.dumps({"issue": {"labels": [{"name": "bug"}, {"name": "ai:engine-claude"}]}}), encoding="utf-8")
	resolved = _resolve("PLAN", {"GITHUB_EVENT_PATH": str(event)})
	assert (resolved["engine"], resolved["source"], resolved["model"], resolved["effort"]) == ("claude", "label:ai:engine-claude", "claude-opus-5-5", "high")
	event.write_text(json.dumps({"pull_request": {"labels": [{"name": "ai:engine-claude"}, {"name": "ai:codex"}]}}), encoding="utf-8")
	assert _resolve("REVIEW_EDITOR", {"GITHUB_EVENT_PATH": str(event)})["source"] == "label:ai:codex"


def test_an_explicit_empty_label_list_overrides_the_payload(tmp_path: Path) -> None:
	event = tmp_path / "event.json"
	event.write_text(json.dumps({"issue": {"labels": [{"name": "ai:engine-claude"}]}}), encoding="utf-8")
	assert _resolve("PLAN", {"GITHUB_EVENT_PATH": str(event), "AI_ENGINE_LABELS": ""})["engine"] == "codex"


@pytest.mark.parametrize("content", ["not json", "[]", '{"issue": {"labels": "x"}}', '{"issue": {"labels": [1, {"name": 2}]}}'])
def test_an_unusable_payload_means_no_labels(tmp_path: Path, content: str) -> None:
	event = tmp_path / "event.json"
	event.write_text(content, encoding="utf-8")
	assert ce.work_item_labels({"GITHUB_EVENT_PATH": str(event)}) == []
	assert ce.work_item_labels({"GITHUB_EVENT_PATH": str(tmp_path / "absent.json")}) == []


# --- fallback classification (engine-fallback-report) ---------------------------


def test_collect_fallbacks_classifies_capacity_and_defects() -> None:
	record = "\n".join([
		"role=PLAN reason=no_credential class=",
		"role=PLAN reason=no_credential class=",  # duplicate
		"role=IMPLEMENT reason=all_accounts_failed class=capacity",
		"role=IMPLEMENT_DIAGNOSE reason=all_accounts_failed class=",
		"role=CLARIFY reason=image_build_failed class=",
		"role=NOT_A_ROLE reason=cli_missing class=",  # unknown role
		"role=PLAN reason=Bad-Reason class=",  # malformed
		"garbage",
	])
	assert ce.collect_fallbacks(record, "all_gated") == [
		{"role": "PLAN", "reason": "no_credential", "detail": "all_gated", "class": "capacity"},
		{"role": "IMPLEMENT", "reason": "all_accounts_failed", "detail": "", "class": "capacity"},
		{"role": "IMPLEMENT_DIAGNOSE", "reason": "all_accounts_failed", "detail": "", "class": "defect"},
		{"role": "CLARIFY", "reason": "image_build_failed", "detail": "", "class": "defect"},
	]


@pytest.mark.parametrize("pool_reason,detail", [
	("cli_missing", "cli_missing"),
	("broker_refused_unregistered_repo", "broker_refused_unregistered_repo"),
	("oidc_request_failed_503", "oidc_request_failed_503"),
	("auth_failed", "auth_failed"),
	("", "pool_unresolved"),
	("Bad Reason!", "pool_unresolved"),
])
def test_no_credential_is_a_defect_unless_every_account_is_gated(pool_reason: str, detail: str) -> None:
	assert ce.collect_fallbacks("role=IMPLEMENT reason=no_credential class=", pool_reason) == [
		{"role": "IMPLEMENT", "reason": "no_credential", "detail": detail, "class": "defect"},
	]


def test_collect_fallbacks_is_bounded() -> None:
	record = "\n".join(f"role=PLAN reason=reason_{index} class=" for index in range(25))
	assert len(ce.collect_fallbacks(record)) == ce.FALLBACK_RECORD_LIMIT


def test_fallbacks_cli_prints_one_json_line(tmp_path: Path) -> None:
	record = tmp_path / "record.txt"
	record.write_text("role=PLAN reason=no_credential class=\n", encoding="utf-8")
	result = _run("fallbacks", "--record-file", str(record), "--pool-reason", "cli_missing")
	assert result.returncode == 0, result.stderr
	assert result.stdout.count("\n") == 1
	assert json.loads(result.stdout) == [{"class": "defect", "detail": "cli_missing", "reason": "no_credential", "role": "PLAN"}]
	missing = _run("fallbacks", "--record-file", str(tmp_path / "absent.txt"))
	assert missing.returncode == 0 and json.loads(missing.stdout) == []
