"""ChatGPT account pool health alert and daily usage section (trial plan Phase 4, Refs #8969).

Covers ``claude_engine.py`` ``pool-health`` / ``pool-usage`` with
``--chatgpt-status``, the ChatGPT part of ``scripts/claude_pool_health_alert.sh``
(run against a fake ``curl`` that records the Telegram request), and the
orchestrate_poll.yml wiring of the ``chatgpt-pool-token`` ``mode: status`` steps.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "claude_pool_health_alert.sh"
ENGINE = REPO_ROOT / "scripts" / "claude_engine.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import claude_engine  # noqa: E402

FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\0' "$@" > "${FAKE_CURL_LOG}"
printf '{"ok":true,"result":{"message_id":43}}'
"""
SECRET = "sentinel-refresh-token-value"
NOW = 1_800_000_000.0


def _iso(hours_ago: float, now: float = NOW) -> str:
	moment = datetime.datetime.fromtimestamp(now - hours_ago * 3600, datetime.timezone.utc)
	return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _account(
	name: str,
	*,
	five: float | None = 0.1,
	weekly: float | None = 0.2,
	hours_ago: float | None = 1.0,
	outcome: str | None = "ok",
	usable: bool = True,
	now: float = NOW,
	**extra: object,
) -> dict:
	entry = {
		"account": name,
		"generation": 3,
		"last_refresh": None if hours_ago is None else _iso(hours_ago, now),
		"last_outcome": outcome,
		"usage": {"five_hour": five, "weekly": weekly, "resets": {"five_hour": None, "weekly": None}},
		"usable": usable,
		"reason": None if usable else "gated",
	}
	entry.update(extra)
	return entry


def _status(*accounts: dict, gate: object = 0.9) -> str:
	return json.dumps({"accounts": list(accounts), "gate": gate})


def _claude_probe(account: str, five: float | None, seven: float | None, *, status: str = "allowed", error: str | None = None) -> dict:
	return {
		"account": account, "five_hour": five, "seven_day": seven, "five_hour_resets_at": 1000, "seven_day_resets_at": 2000,
		"resets_at": None, "status": status, "error": error,
	}


# --- engine: pool_health ------------------------------------------------------


def test_pool_health_without_chatgpt_status_is_unchanged() -> None:
	probes = [_claude_probe("ALPHA", 0.95, 0.1), _claude_probe("BETA", 0.1, 0.1)]
	before = claude_engine.pool_health(probes, 0.9, now=0)
	assert before["chatgpt_accounts"] == 0 and before["chatgpt_alerts"] == [] and before["chatgpt_warning"] is False
	assert before["claude_alert"] is True and before["alert"] is True
	empty = claude_engine.pool_health(probes, 0.9, now=0, chatgpt_status=_status())
	assert empty["text"] == before["text"]
	healthy = claude_engine.pool_health(probes, 0.9, now=NOW, chatgpt_status=_status(_account("ONE")))
	assert healthy["chatgpt_accounts"] == 1 and healthy["chatgpt_alerts"] == []
	assert "ChatGPT" not in healthy["text"]


def test_healthy_chatgpt_and_quiet_claude_do_not_alert() -> None:
	report = claude_engine.pool_health([_claude_probe("ALPHA", 0.1, 0.1)], 0.9, now=NOW, chatgpt_status=_status(_account("ONE")))
	assert report["alert"] is False and report["text"] == ""


@pytest.mark.parametrize(
	("entry", "cause", "warning"),
	[
		(_account("ONE", outcome="auth_failed"), "login rejected (auth_failed); save a new CHATGPT_POOL_AUTH_ONE", True),
		(_account("ONE", outcome="timeout"), "last refresh failed (timeout)", True),
		(_account("ONE", outcome="run_failed"), "last refresh failed (run_failed)", True),
		(_account("ONE", outcome="something_new"), "last refresh failed (something_new)", True),
		(_account("ONE", hours_ago=13), "stale (last refresh 13.0h ago)", True),
		(_account("ONE", hours_ago=None, outcome=None), "stale (never refreshed)", True),
		(_account("ONE", last_refresh="2026-01-01T00:00:00"), "stale (never refreshed)", True),
		(_account("ONE", weekly=0.95), "at or above the 90% usage gate", False),
		(_account("ONE", five=0.9), "at or above the 90% usage gate", False),
		(_account("ONE", outcome="quota_exhausted"), "quota exhausted", False),
	],
)
def test_each_alert_cause(entry: dict, cause: str, warning: bool) -> None:
	report = claude_engine.pool_health([], 0.9, now=NOW, chatgpt_status=_status(entry))
	assert report["alert"] is True and report["claude_alert"] is False
	assert report["chatgpt_alerts"] == ["ONE"]
	assert report["chatgpt_warning"] is warning
	lines = report["text"].splitlines()
	assert lines[0] == "ChatGPT pool: 1 of 1 account(s) need attention; 1 usable."
	assert lines[1].startswith("ONE: ") and f"; {cause}" in lines[1]


def test_fresh_refresh_is_not_stale_and_the_line_shows_every_field() -> None:
	report = claude_engine.pool_health([], 0.9, now=NOW, chatgpt_status=_status(_account("ONE", hours_ago=11, weekly=0.95)))
	assert report["text"].splitlines()[1] == (
		"ONE: 5h 10%, weekly 95%, refreshed 11.0h ago, last outcome ok; at or above the 90% usage gate"
	)
	assert report["chatgpt_warning"] is False


def test_stale_hours_override() -> None:
	status = _status(_account("ONE", hours_ago=3))
	assert claude_engine.pool_health([], 0.9, now=NOW, chatgpt_status=status)["chatgpt_alerts"] == []
	flagged = claude_engine.pool_health([], 0.9, now=NOW, chatgpt_status=status, chatgpt_stale_hours=2)
	assert flagged["chatgpt_alerts"] == ["ONE"] and "stale (last refresh 3.0h ago)" in flagged["text"]
	# A non-positive value falls back to 12 hours.
	assert claude_engine.pool_health([], 0.9, now=NOW, chatgpt_status=status, chatgpt_stale_hours=0)["chatgpt_alerts"] == []


@pytest.mark.parametrize(("gate", "flagged"), [(0.5, True), (0, False), ("high", False), (1.5, False)])
def test_status_gate_is_honoured_or_falls_back(gate: object, flagged: bool) -> None:
	report = claude_engine.pool_health([], 0.9, now=NOW, chatgpt_status=_status(_account("ONE", weekly=0.6), gate=gate))
	assert bool(report["chatgpt_alerts"]) is flagged


@pytest.mark.parametrize(
	"raw",
	["not json", "[]", json.dumps({"accounts": "x"}), json.dumps({"accounts": [1, None, "A"]}), "[" * 100000],
)
def test_malformed_status_never_raises(raw: str) -> None:
	report = claude_engine.pool_health([_claude_probe("ALPHA", 0.1, 0.1)], 0.9, now=NOW, chatgpt_status=raw)
	assert report["chatgpt_accounts"] == 0 and report["alert"] is False


def test_bad_entries_are_dropped_and_the_list_is_capped() -> None:
	entries = [_account("lower", outcome="auth_failed"), _account("BAD NAME", outcome="auth_failed"), _account("TRAIL\n")]
	entries += [_account(f"ACC_{index:03d}") for index in range(80)]
	accounts, gate, error = claude_engine._chatgpt_status_parse(_status(*entries))
	assert error is None and gate == 0.9
	assert len(accounts) == claude_engine.CHATGPT_STATUS_MAX_ACCOUNTS
	assert all(re.fullmatch(r"ACC_[0-9]{3}", entry["account"]) for entry in accounts)


def test_long_broker_valid_names_are_kept() -> None:
	"""The broker has no length cap on `[A-Z0-9_]+` names, so neither does the parser."""
	name = "X" * 65
	report = claude_engine.pool_health([], 0.9, now=NOW, chatgpt_status=_status(_account(name, outcome="auth_failed")))
	assert report["chatgpt_alerts"] == [name]


def test_unsafe_strings_are_never_rendered() -> None:
	entry = _account("ONE", outcome="bad\n::error::x", reason="x\ny", auth_json={"tokens": {"refresh_token": SECRET}}, refresh_token=SECRET)
	report = claude_engine.pool_health([], 0.9, now=NOW, chatgpt_status=_status(entry))
	assert "last refresh failed (unknown)" in report["text"]
	assert SECRET not in report["text"] and "::" not in report["text"] and "\ny" not in report["text"]


def test_claude_and_chatgpt_blocks_share_one_message() -> None:
	status = _status(_account("TWO", weekly=0.95, usable=False), _account("ONE"))
	report = claude_engine.pool_health([_claude_probe("ALPHA", 0.95, 0.1)], 0.9, now=NOW, chatgpt_status=status)
	lines = report["text"].splitlines()
	assert lines[0].startswith("Claude pool: 1 of 1 account(s)")
	index = lines.index("ChatGPT pool: 1 of 2 account(s) need attention; 1 usable.")
	assert lines[index + 1].startswith("ONE: ") and ";" not in lines[index + 1]
	assert lines[index + 2].startswith("TWO: ") and lines[index + 2].endswith("; at or above the 90% usage gate")


# --- engine: pool_usage_report -----------------------------------------------


def test_usage_report_appends_a_chatgpt_section() -> None:
	probes = [_claude_probe("ALPHA", 0.1, 0.2)]
	plain = claude_engine.pool_usage_report(probes, 0.9, now=0)
	report = claude_engine.pool_usage_report(
		probes, 0.9, now=0, chatgpt_status=_status(_account("ONE", now=0), _account("TWO", outcome="auth_failed", now=0))
	)
	assert report["text"].startswith(plain["text"] + "\n\nChatGPT accounts: 2 account(s), 2 usable, 1 needing attention.\n")
	assert report["chatgpt_accounts"] == 2 and report["chatgpt_alerts"] == 1
	assert "TWO: 5h 10%, weekly 20%, refreshed 1.0h ago, last outcome auth_failed; login rejected" in report["text"]


@pytest.mark.parametrize(
	("status", "reason", "section"),
	[
		(_status(), "", None),
		(_status(), "broker_refused_x", None),
		("", "", None),
		("", "broker_refused_not_found", "ChatGPT accounts: unavailable (broker_refused_not_found)."),
		("", "bad reason\n", "ChatGPT accounts: unavailable (unknown)."),
		("not json", "status", "ChatGPT accounts: unavailable (status_invalid)."),
	],
)
def test_usage_report_unavailable_and_empty(status: str, reason: str, section: str | None) -> None:
	probes = [_claude_probe("ALPHA", 0.1, 0.2)]
	plain = claude_engine.pool_usage_report(probes, 0.9, now=0)["text"]
	text = claude_engine.pool_usage_report(probes, 0.9, now=0, chatgpt_status=status, chatgpt_reason=reason)["text"]
	assert text == (plain if section is None else f"{plain}\n\n{section}")


def test_cli_flags_reach_the_renderers() -> None:
	status = _status(_account("ONE", outcome="auth_failed"))
	health = subprocess.run(
		[sys.executable, str(ENGINE), "pool-health", "--gate", "0.9", f"--chatgpt-status={status}", "--chatgpt-stale-hours", "-3"],
		input="[]", capture_output=True, text=True, timeout=60, check=False, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
	)
	assert health.returncode == 0, health.stderr
	parsed = json.loads(health.stdout)
	assert parsed["chatgpt_alerts"] == ["ONE"] and parsed["chatgpt_warning"] is True
	usage = subprocess.run(
		[sys.executable, str(ENGINE), "pool-usage", "--gate", "0.9", "--chatgpt-status=", "--chatgpt-reason=broker_unavailable_503"],
		input="[]", capture_output=True, text=True, timeout=60, check=False, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
	)
	assert usage.returncode == 0, usage.stderr
	assert json.loads(usage.stdout)["text"].endswith("\n\nChatGPT accounts: unavailable (broker_unavailable_503).")


# --- claude_pool_health_alert.sh ----------------------------------------------


@pytest.fixture()
def env(tmp_path: Path) -> dict:
	fake_bin = tmp_path / "bin"
	fake_bin.mkdir()
	curl = fake_bin / "curl"
	curl.write_text(FAKE_CURL, encoding="utf-8")
	curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
	log = tmp_path / "curl.log"
	values = {key: value for key, value in os.environ.items() if not key.startswith(("CLAUDE_", "CHATGPT_", "TG_", "GITHUB_", "ALERT_"))}
	values.update({
		"PATH": f"{fake_bin}:{os.environ['PATH']}",
		"FAKE_CURL_LOG": str(log),
		"TG_BOT_SECRET": "bot-secret-value",
		"TG_ADMIN_CHAT_ID": "4242",
		"CLAUDE_POOL_HEALTH_GATE": "0.9",
		"CLAUDE_POOL_HEALTH_WINDOW_MINUTES": "60",
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	return {"env": values, "log": log}


def _live_account(name: str, **kwargs: object) -> dict:
	"""An account relative to the real clock, since the script cannot be given ``now``."""
	return _account(name, now=datetime.datetime.now(datetime.timezone.utc).timestamp(), **kwargs)


def _run(env: dict, probes: list | None, status: str | None = None, **extra: str) -> subprocess.CompletedProcess:
	values = dict(env["env"], **extra)
	if probes is not None:
		values["CLAUDE_POOL_PROBES"] = json.dumps(probes)
	if status is not None:
		values["CHATGPT_POOL_STATUS"] = status
	return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=values, timeout=60, check=False)


def _sent_text(env: dict) -> str:
	args = env["log"].read_text(encoding="utf-8").split("\0")
	return next(arg for arg in args if arg.startswith("text=")).removeprefix("text=")


def test_shell_quiet_when_claude_below_gate_and_chatgpt_healthy(env: dict) -> None:
	result = _run(env, [_claude_probe("ALPHA", 0.1, 0.1)], _status(_live_account("ONE")))
	assert result.returncode == 0, result.stderr
	assert "alert=none waiting=0 chatgpt_accounts=1 chatgpt_alerts=0" in result.stdout
	assert not env["log"].exists()


def test_shell_chatgpt_only_warning_is_sent(env: dict) -> None:
	status = _status(_live_account("ONE", outcome="auth_failed", auth_json={"refresh_token": SECRET}))
	result = _run(env, [], status)
	assert result.returncode == 0, result.stderr
	assert "CLAUDE_POOL_HEALTH accounts=0 gated=0 auth_failed=0 probe_failed=0 alert=sent waiting=0 chatgpt_accounts=1 chatgpt_alerts=1" in result.stdout
	text = _sent_text(env)
	assert text.startswith("⚠️ WARNING: ChatGPT pool: 1 of 1 account(s) need attention; 1 usable.\nONE: ")
	assert "Claude pool" not in text
	assert "save a new CHATGPT_POOL_AUTH_ONE" in text
	assert SECRET not in result.stdout + result.stderr + text


def test_shell_chatgpt_only_outside_window(env: dict) -> None:
	result = _run(env, [], _status(_live_account("ONE", outcome="auth_failed")), CLAUDE_POOL_HEALTH_WINDOW_MINUTES="0")
	assert "alert=outside_window waiting=0 chatgpt_accounts=1 chatgpt_alerts=1" in result.stdout
	assert not env["log"].exists()


def test_shell_gated_claude_and_gated_chatgpt_share_one_debug_message(env: dict) -> None:
	status = _status(_live_account("ONE", weekly=0.95))
	result = _run(env, [_claude_probe("ALPHA", 0.95, 0.1)], status, ALERT_MSG_LEVEL="DEBUG")
	assert "alert=sent waiting=0 chatgpt_accounts=1 chatgpt_alerts=1" in result.stdout
	text = _sent_text(env)
	assert "DEBUG" in text and "Claude pool: 1 of 1" in text and "ChatGPT pool: 1 of 1" in text


def test_shell_gated_only_chatgpt_does_not_page_at_the_default_threshold(env: dict) -> None:
	result = _run(env, [], _status(_live_account("ONE", outcome="quota_exhausted")))
	assert "alert=not_delivered" in result.stdout
	assert not env["log"].exists()


def test_shell_claude_probe_failure_with_chatgpt_gate_only_is_debug(env: dict) -> None:
	status = _status(_live_account("ONE", weekly=0.95))
	probes = [_claude_probe("ALPHA", 0.1, 0.1), _claude_probe("BETA", None, None, status=None, error="probe_failed")]
	result = _run(env, probes, status)
	assert "probe_failed=1 alert=not_delivered" in result.stdout
	assert not env["log"].exists()


def test_shell_chatgpt_switch_off_is_claude_only(env: dict) -> None:
	status = _status(_live_account("ONE", outcome="auth_failed"))
	result = _run(env, [_claude_probe("ALPHA", 0.1, 0.1)], status, CHATGPT_POOL_HEALTH_ALERT_ENABLED="false")
	assert "alert=none waiting=0 chatgpt_accounts=0 chatgpt_alerts=0" in result.stdout
	off = _run(env, [], status, CHATGPT_POOL_HEALTH_ALERT_ENABLED="off")
	assert "alert=no_probes" in off.stdout
	assert not env["log"].exists()


@pytest.mark.parametrize(
	("probes", "status", "extra", "expected"),
	[
		([_claude_probe("ALPHA", 0.95, 0.1)], "{}", {"CLAUDE_POOL_HEALTH_ALERT_ENABLED": "false"}, "alert=disabled"),
		([], _status(), {}, "alert=no_probes"),
		([], "not json", {}, "alert=no_probes"),
		(None, None, {}, "alert=no_probes"),
	],
)
def test_shell_skips_never_fail(env: dict, probes: list | None, status: str | None, extra: dict, expected: str) -> None:
	result = _run(env, probes, status, **extra)
	assert result.returncode == 0 and expected in result.stdout
	assert not env["log"].exists()


def test_shell_long_alert_is_truncated_and_still_sent(env: dict) -> None:
	accounts = [_live_account(f"ACCOUNT_WITH_A_LONG_NAME_{index:03d}", outcome="auth_failed", hours_ago=30) for index in range(40)]
	result = _run(env, [_claude_probe("ALPHA", 0.95, 0.1)], _status(*accounts))
	assert result.returncode == 0, result.stderr
	assert "alert=sent" in result.stdout and "chatgpt_alerts=40" in result.stdout
	assert "::group::CLAUDE_POOL_HEALTH full alert" in result.stdout
	text = _sent_text(env)
	assert len(text) <= 4096
	assert "Claude pool: 1 of 1" in text and "(truncated: the alert is longer than 3700 characters" in text


def test_shell_empty_claude_probes_send_no_capacity_wait_notice(env: dict) -> None:
	items = json.dumps({"count": 2, "items": [{"kind": "issue", "item": "5"}], "opened_at": "2026-11-01T00:00:00Z"})
	result = _run(env, [], _status(_live_account("ONE")), CLAUDE_CAPACITY_WAIT_ITEMS=items)
	assert result.returncode == 0, result.stderr
	assert "alert=none waiting=0 chatgpt_accounts=1 chatgpt_alerts=0" in result.stdout
	assert not env["log"].exists()


def test_shell_invalid_stale_hours_falls_back(env: dict) -> None:
	status = _status(_live_account("ONE", hours_ago=13))
	result = _run(env, [], status, CHATGPT_POOL_ALERT_STALE_HOURS="nonsense; rm -rf /")
	assert "chatgpt_accounts=1 chatgpt_alerts=1" in result.stdout
	fresh = _run(env, [], status, CHATGPT_POOL_ALERT_STALE_HOURS="24")
	assert "alert=no_probes" not in fresh.stdout and "chatgpt_alerts=0" in fresh.stdout


# --- wiring -------------------------------------------------------------------


def _steps(job: dict) -> list[str]:
	return [step.get("id") or step.get("name") for step in job["steps"]]


def test_poll_job_reads_the_status_before_the_alert() -> None:
	yaml = pytest.importorskip("yaml")
	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	poll = workflow["jobs"]["poll"]
	order = _steps(poll)
	alert_name = "Alert on Claude pool accounts at the usage gate"
	assert order.index("claude_pool") < order.index("chatgpt_pool_status") < order.index(alert_name)
	step = next(step for step in poll["steps"] if step.get("id") == "chatgpt_pool_status")
	assert step["uses"] == "./.codex-workflow-src/.github/actions/chatgpt-pool-token"
	assert step["with"]["mode"] == "status"
	assert step["continue-on-error"] is True
	assert "vars.CHATGPT_POOL_HEALTH_ALERT_ENABLED != 'false'" in step["if"]
	alert = next(step for step in poll["steps"] if step.get("name") == alert_name)
	assert alert["env"]["CHATGPT_POOL_STATUS"] == "${{ steps.chatgpt_pool_status.outputs.status || '' }}"
	assert alert["env"]["CHATGPT_POOL_HEALTH_ALERT_ENABLED"] == "${{ vars.CHATGPT_POOL_HEALTH_ALERT_ENABLED || 'true' }}"
	assert alert["env"]["CHATGPT_POOL_ALERT_STALE_HOURS"] == "${{ vars.CHATGPT_POOL_ALERT_STALE_HOURS || '12' }}"


def test_daily_job_reads_the_status_before_sending() -> None:
	yaml = pytest.importorskip("yaml")
	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	job = workflow["jobs"]["claude_pool_daily_usage"]
	order = _steps(job)
	assert order.index("claude_pool") < order.index("chatgpt_pool_status") < order.index("pool_daily_send")
	steps = {step.get("id"): step for step in job["steps"] if step.get("id")}
	status, send, probe = steps["chatgpt_pool_status"], steps["pool_daily_send"], steps["claude_pool"]
	assert status["uses"] == "./.github/actions/chatgpt-pool-token"
	assert status["with"] == {"mode": "status", "config_path": ".github/ai/claude_engine.json"}
	assert status["continue-on-error"] is True and status["if"] == probe["if"]
	assert send["env"]["CHATGPT_POOL_STATUS"] == "${{ steps.chatgpt_pool_status.outputs.status || '' }}"
	assert send["env"]["CHATGPT_POOL_STATUS_REASON"] == "${{ steps.chatgpt_pool_status.outputs.reason || 'not_read' }}"
	assert send["env"]["CHATGPT_POOL_ALERT_STALE_HOURS"] == "${{ vars.CHATGPT_POOL_ALERT_STALE_HOURS || '12' }}"


def test_status_output_never_carries_a_credential_step_output() -> None:
	text = WORKFLOW.read_text(encoding="utf-8")
	assert "steps.chatgpt_pool_status.outputs.codex_home" not in text
	assert "steps.chatgpt_pool_status.outputs.account" not in text


def test_workflow_stays_under_the_size_guard() -> None:
	assert WORKFLOW.stat().st_size < 480_000


def test_orchestrate_poll_is_in_the_broker_allowlist() -> None:
	source = (REPO_ROOT / "tools" / "claude-pool-broker" / "src" / "index.ts").read_text(encoding="utf-8")
	match = re.search(r"ALLOWED_WORKFLOW_FILES[^=]*=\s*Object\.freeze\(\[(.*?)\]\)", source, re.DOTALL)
	assert match and '"orchestrate_poll.yml"' in match.group(1)


def test_ci_runs_this_file() -> None:
	ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
	assert "tests/test_chatgpt_pool_health_alert.py" in ci


def test_empty_outcome_is_stale_not_a_failed_refresh() -> None:
	report = claude_engine.pool_health([], 0.9, now=NOW, chatgpt_status=_status(_account("ONE", hours_ago=None, outcome="")))
	assert "stale (never refreshed)" in report["text"] and "last refresh failed" not in report["text"]
	assert report["chatgpt_warning"] is True


def test_shell_oversized_chatgpt_status_keeps_the_claude_alert(env: dict) -> None:
	status = json.dumps({"accounts": [], "gate": 0.9, "pad": "x" * 100_001})
	result = _run(env, [_claude_probe("ALPHA", 0.95, 0.1)], status, ALERT_MSG_LEVEL="DEBUG")
	assert result.returncode == 0, result.stderr
	assert "chatgpt_status=too_large" in result.stdout
	assert "alert=sent" in result.stdout and "Claude pool: 1 of 1" in _sent_text(env)
