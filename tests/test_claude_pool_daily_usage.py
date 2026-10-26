"""scripts/claude_pool_daily_usage.sh — the once-a-day Claude pool usage report.

Runs the shipped script against a fake ``curl`` that records the Telegram
request, checks ``claude_engine.py pool-usage``, and pins the
orchestrate_poll.yml wiring: the slot check and marker lookup in the poll job,
the separate report job that probes, sends and saves the day's marker, and the
ci.yml step that runs this file.
"""

from __future__ import annotations

import calendar
import time
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "claude_pool_daily_usage.sh"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import claude_engine  # noqa: E402

FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\0' "$@" > "${FAKE_CURL_LOG}"
printf '{"ok":true,"result":{"message_id":77}}'
"""


def _probe(account: str, five: float | None, seven: float | None, *, status: str = "allowed", error: str | None = None) -> dict:
	return {
		"account": account, "five_hour": five, "seven_day": seven, "five_hour_resets_at": 1000, "seven_day_resets_at": 2000,
		"resets_at": None, "status": status, "error": error,
	}


def _epoch(text: str) -> str:
	return str(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M")))


@pytest.fixture()
def env(tmp_path: Path) -> dict:
	fake_bin = tmp_path / "bin"
	fake_bin.mkdir()
	curl = fake_bin / "curl"
	curl.write_text(FAKE_CURL, encoding="utf-8")
	curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
	values = {key: value for key, value in os.environ.items() if not key.startswith(("CLAUDE_", "TG_", "GITHUB_", "ALERT_"))}
	values.update({
		"PATH": f"{fake_bin}:{os.environ['PATH']}",
		"FAKE_CURL_LOG": str(tmp_path / "curl.log"),
		"GITHUB_OUTPUT": str(tmp_path / "output"),
		"TG_BOT_SECRET": "bot-secret-value",
		"TG_ADMIN_CHAT_ID": "4242",
		"CLAUDE_POOL_DAILY_USAGE_GATE": "0.9",
		"GITHUB_SERVER_URL": "https://github.com",
		"GITHUB_REPOSITORY": "shubhodeep1/coding-workflows",
		"GITHUB_RUN_ID": "123456",
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	return {"env": values, "log": tmp_path / "curl.log", "output": tmp_path / "output"}


def _run(env: dict, mode: str, probes: list | None = None, **extra: str) -> subprocess.CompletedProcess:
	values = dict(env["env"], **extra)
	if probes is not None:
		values["CLAUDE_POOL_PROBES"] = json.dumps(probes)
	return subprocess.run(["bash", str(SCRIPT), mode], capture_output=True, text=True, env=values, timeout=60, check=False)


def _outputs(env: dict) -> dict:
	if not env["output"].exists():
		return {}
	return dict(line.split("=", 1) for line in env["output"].read_text(encoding="utf-8").splitlines() if "=" in line)


def _sent_text(env: dict) -> str:
	args = env["log"].read_text(encoding="utf-8").split("\0")
	assert any(arg.startswith("https://api.telegram.org/bot") and arg.endswith("/sendMessage") for arg in args)
	return next(arg for arg in args if arg.startswith("text=")).removeprefix("text=")


# --- claude_engine.py pool-usage ------------------------------------------------


def test_report_lists_every_account_in_one_message() -> None:
	probes = [
		_probe("GAMMA", None, None, status="rejected"),
		_probe("ALPHA", 0.12, 0.48),
		_probe("BETA", 0.97, 0.6),
		_probe("DELTA", None, None, status=None, error="auth_failed"),
		_probe("EPSILON", None, None, status=None, error="probe_failed"),
		_probe("ZETA", None, 0.3),
	]
	report = claude_engine.pool_usage_report(probes, 0.95)
	assert report == {
		"accounts": 6, "usable": 2, "gated": 2, "auth_failed": 1, "probe_failed": 1,
		"text": "\n".join([
			"Claude pool daily usage: 6 account(s), 2 usable, 2 at or above the 95% gate, 1 with a rejected token, 1 not probed.",
			"ALPHA: 5h 12%, 7d 48%",
			"BETA: 5h 97% (resets 1970-01-01 00:16 UTC), 7d 60%",
			"DELTA: token rejected (auth_failed); rotate CLAUDE_POOL_TOKEN_DELTA",
			"EPSILON: probe failed (probe_failed)",
			"GAMMA: 5h ?, 7d ?, usage limit reached (resets unknown)",
			"ZETA: 5h ?, 7d 30%",
		]),
	}


def test_report_with_no_probes_still_has_text() -> None:
	report = claude_engine.pool_usage_report([], 0.95)
	assert report["accounts"] == 0
	assert report["text"] == "Claude pool daily usage: no probe results."


def test_pool_usage_cli_reads_the_gate_from_the_config(tmp_path: Path) -> None:
	config = tmp_path / "claude_engine.json"
	config.write_text(json.dumps({"gate_utilization": 0.5}), encoding="utf-8")
	result = subprocess.run(
		[sys.executable, str(REPO_ROOT / "scripts" / "claude_engine.py"), "pool-usage", "--config", str(config)],
		input=json.dumps([_probe("ALPHA", 0.6, 0.1)]), capture_output=True, text=True, timeout=60, check=False,
		env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
	)
	assert result.returncode == 0, result.stderr
	assert "at or above the 50% gate" in json.loads(result.stdout)["text"]


# --- slot ---------------------------------------------------------------------


@pytest.mark.parametrize(
	("now", "extra", "in_slot", "reason"),
	[
		("2026-10-17T00:00", {}, "true", "due"),
		("2026-10-17T23:59", {}, "true", "due"),
		("2026-10-17T03:59", {"CLAUDE_POOL_DAILY_USAGE_HOUR_UTC": "4"}, "false", "before_hour"),
		("2026-10-17T04:00", {"CLAUDE_POOL_DAILY_USAGE_HOUR_UTC": "4"}, "true", "due"),
		("2026-10-17T04:30", {"CLAUDE_POOL_DAILY_USAGE_ENABLED": "false"}, "false", "disabled"),
		("2026-10-17T04:30", {"ALERT_MSG_LEVEL": "silent"}, "false", "silent"),
		("2026-10-17T04:30", {"TG_BOT_SECRET": ""}, "false", "telegram_not_configured"),
		("2026-10-17T09:30", {"CLAUDE_POOL_DAILY_USAGE_HOUR_UTC": "10"}, "false", "before_hour"),
		("2026-10-17T00:30", {"CLAUDE_POOL_DAILY_USAGE_HOUR_UTC": "nonsense"}, "true", "due"),
		("2026-10-17T00:30", {"CLAUDE_POOL_DAILY_USAGE_HOUR_UTC": "24"}, "true", "due"),
	],
)
def test_slot(env: dict, now: str, extra: dict, in_slot: str, reason: str) -> None:
	result = _run(env, "slot", CLAUDE_POOL_DAILY_USAGE_NOW=_epoch(now), **extra)
	assert result.returncode == 0, result.stderr
	assert _outputs(env) == {"in_slot": in_slot, "date": "2026-10-17"}
	assert f"CLAUDE_POOL_DAILY_USAGE op=slot in_slot={in_slot} reason={reason} date=2026-10-17" in result.stdout
	assert not env["log"].exists()


# --- send ---------------------------------------------------------------------


def test_send_reaches_telegram_at_the_default_warning_threshold(env: dict) -> None:
	probes = [_probe("BETA", 0.2, 0.1), _probe("ALPHA", 0.95, 0.4)]
	result = _run(env, "send", probes)
	assert result.returncode == 0, result.stderr
	assert "CLAUDE_POOL_DAILY_USAGE op=send accounts=2 usable=1 gated=1 auth_failed=0 probe_failed=0 outcome=sent" in result.stdout
	assert _outputs(env) == {"sent": "true"}
	text = _sent_text(env)
	assert text.startswith("📊 Claude pool daily usage: 2 account(s), 1 usable, 1 at or above the 90% gate.\n")
	assert "ALPHA: 5h 95% (resets 1970-01-01 00:16 UTC), 7d 40%\nBETA: 5h 20%, 7d 10%\n" in text
	assert "DEBUG" not in text and "WARNING" not in text
	assert text.endswith("Probed by https://github.com/shubhodeep1/coding-workflows/actions/runs/123456")
	assert "bot-secret-value" not in result.stdout + result.stderr


def test_send_honours_silent(env: dict) -> None:
	result = _run(env, "send", [_probe("ALPHA", 0.1, 0.1)], ALERT_MSG_LEVEL="SILENT")
	assert "outcome=not_delivered" in result.stdout
	assert _outputs(env) == {"sent": "false"}
	assert not env["log"].exists()


def test_send_without_probes_names_the_pool_reason(env: dict) -> None:
	result = _run(env, "send", None, CLAUDE_POOL_REASON="broker_refused_workflow_not_allowed")
	assert "accounts=0" in result.stdout and "outcome=sent" in result.stdout
	assert _sent_text(env).startswith(
		"📊 Claude pool daily usage: no probe results (pool: broker_refused_workflow_not_allowed).\n"
	)


def test_send_with_unreadable_probes_still_reports(env: dict) -> None:
	result = _run(env, "send", None, CLAUDE_POOL_PROBES="not json")
	assert result.returncode == 0 and "outcome=sent" in result.stdout
	assert _sent_text(env).startswith("📊 Claude pool daily usage: probe results unreadable.")


def test_unknown_mode_never_fails(env: dict) -> None:
	result = _run(env, "bogus")
	assert result.returncode == 0
	assert not env["log"].exists()


# --- wiring -------------------------------------------------------------------


def test_orchestrate_poll_wiring() -> None:
	workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml").read_text(encoding="utf-8"))
	poll = workflow["jobs"]["poll"]
	steps = {step.get("id"): step for step in poll["steps"] if step.get("id")}
	slot, seen = steps["pool_daily_slot"], steps["pool_daily_seen"]
	assert slot["if"] == "github.repository == 'shubhodeep1/coding-workflows'"
	assert slot["run"] == "bash .codex-workflow-src/scripts/claude_pool_daily_usage.sh slot"
	assert slot["env"]["CLAUDE_POOL_DAILY_USAGE_HOUR_UTC"] == "${{ vars.CLAUDE_POOL_DAILY_USAGE_HOUR_UTC || '0' }}"
	assert slot["env"]["CLAUDE_POOL_DAILY_USAGE_ENABLED"] == "${{ vars.CLAUDE_POOL_DAILY_USAGE_ENABLED || 'true' }}"
	assert seen["uses"].startswith("actions/cache/restore@") and seen["with"]["lookup-only"] is True
	order = [step.get("id") for step in poll["steps"]]
	assert order.index("claude_pool") < order.index("pool_daily_slot") < order.index("pool_daily_seen")
	assert "steps.pool_daily_seen.outputs.cache-hit != 'true'" in poll["outputs"]["claude_pool_daily_due"]
	assert "steps.pool_daily_seen.outcome == 'success'" in poll["outputs"]["claude_pool_daily_due"]

	job = workflow["jobs"]["claude_pool_daily_usage"]
	assert job["needs"] == "poll"
	assert "needs.poll.outputs.claude_pool_daily_due == 'true'" in job["if"]
	job_steps = {step.get("id"): step for step in job["steps"] if step.get("id")}
	recheck, send = job_steps["pool_daily_recheck"], job_steps["pool_daily_send"]
	save = next(step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/cache/save@"))
	key = "claude-pool-daily-usage-v1-${{ needs.poll.outputs.claude_pool_daily_date }}"
	assert recheck["with"]["key"] == save["with"]["key"] == key
	assert seen["with"]["key"] == "claude-pool-daily-usage-v1-${{ steps.pool_daily_slot.outputs.date }}"
	assert seen["with"]["path"] == recheck["with"]["path"] == save["with"]["path"]
	assert job_steps["claude_pool"]["uses"] == "./.github/actions/claude-pool-token"
	assert send["run"] == "bash scripts/claude_pool_daily_usage.sh send"
	assert send["env"]["CLAUDE_POOL_PROBES"] == "${{ steps.claude_pool.outputs.probes || '[]' }}"
	assert save["if"] == "steps.pool_daily_send.outputs.sent == 'true'"


def test_ci_runs_this_file() -> None:
	ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
	assert "tests/test_claude_pool_daily_usage.py" in ci
