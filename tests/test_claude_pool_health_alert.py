"""scripts/claude_pool_health_alert.sh — the hourly Claude pool near-cap alert.

Runs the shipped script against a fake ``curl`` that records the Telegram
request, and pins the orchestrate_poll.yml wiring: the step runs right after
the pool step, reads its ``probes`` output, and the ci.yml step runs this file.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "claude_pool_health_alert.sh"
FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\0' "$@" > "${FAKE_CURL_LOG}"
printf '{"ok":true,"result":{"message_id":41}}'
"""


def _probe(account: str, five: float | None, seven: float | None, *, status: str = "allowed", error: str | None = None) -> dict:
	return {
		"account": account, "five_hour": five, "seven_day": seven, "five_hour_resets_at": 1000, "seven_day_resets_at": 2000,
		"resets_at": None, "status": status, "error": error,
	}


@pytest.fixture()
def env(tmp_path: Path) -> dict:
	fake_bin = tmp_path / "bin"
	fake_bin.mkdir()
	curl = fake_bin / "curl"
	curl.write_text(FAKE_CURL, encoding="utf-8")
	curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
	log = tmp_path / "curl.log"
	values = {key: value for key, value in os.environ.items() if not key.startswith(("CLAUDE_", "TG_", "GITHUB_", "ALERT_"))}
	values.update({
		"PATH": f"{fake_bin}:{os.environ['PATH']}",
		"FAKE_CURL_LOG": str(log),
		"TG_BOT_SECRET": "bot-secret-value",
		"TG_ADMIN_CHAT_ID": "4242",
		"CLAUDE_POOL_HEALTH_GATE": "0.9",
		"CLAUDE_POOL_HEALTH_WINDOW_MINUTES": "60",
		"GITHUB_SERVER_URL": "https://github.com",
		"GITHUB_REPOSITORY": "shubhodeep1/coding-workflows",
		"GITHUB_RUN_ID": "123456",
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	return {"env": values, "log": log}


def _run(env: dict, probes: list | None, **extra: str) -> subprocess.CompletedProcess:
	values = dict(env["env"], **extra)
	if probes is not None:
		values["CLAUDE_POOL_PROBES"] = json.dumps(probes)
	return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=values, timeout=60, check=False)


def _sent_text(env: dict) -> str:
	args = env["log"].read_text(encoding="utf-8").split("\0")
	assert any(arg.startswith("https://api.telegram.org/bot") and arg.endswith("/sendMessage") for arg in args)
	return next(arg for arg in args if arg.startswith("text=")).removeprefix("text=")


def test_sends_one_warning_naming_every_account_at_the_gate(env: dict) -> None:
	probes = [_probe("BETA", 0.2, 0.1), _probe("ALPHA", 0.95, 0.1), _probe("GAMMA", None, None, status="rejected")]
	result = _run(env, probes)
	assert result.returncode == 0, result.stderr
	assert "CLAUDE_POOL_HEALTH accounts=3 gated=2 auth_failed=0 probe_failed=0 alert=sent" in result.stdout
	text = _sent_text(env)
	assert text.startswith("⚠️ WARNING: Claude pool: 2 of 3 account(s) at or above the 90% usage gate; 1 usable.\n")
	assert "ALPHA: 5h 95% (resets 1970-01-01 00:16 UTC), 7d 10%" in text
	assert "GAMMA: usage limit reached (resets unknown)" in text
	assert "BETA:" not in text
	assert text.endswith("Probed by https://github.com/shubhodeep1/coding-workflows/actions/runs/123456")
	assert "bot-secret-value" not in result.stdout + result.stderr


def test_quiet_while_every_account_is_below_the_gate(env: dict) -> None:
	result = _run(env, [_probe("ALPHA", 0.85, 0.3), _probe("BETA", 0.2, 0.1)])
	assert "CLAUDE_POOL_HEALTH accounts=2 gated=0 auth_failed=0 probe_failed=0 alert=none" in result.stdout
	assert not env["log"].exists()


def test_a_rejected_token_is_alerted_too(env: dict) -> None:
	result = _run(env, [_probe("ALPHA", 0.1, 0.1), _probe("BETA", None, None, status=None, error="auth_failed")])
	assert "alert=sent" in result.stdout and "auth_failed=1" in result.stdout
	assert "BETA: token rejected (auth_failed); rotate CLAUDE_POOL_TOKEN_BETA" in _sent_text(env)


def test_outside_the_hourly_window_nothing_is_sent(env: dict) -> None:
	result = _run(env, [_probe("ALPHA", 0.95, 0.1)], CLAUDE_POOL_HEALTH_WINDOW_MINUTES="0")
	assert "CLAUDE_POOL_HEALTH accounts=1 gated=1 auth_failed=0 probe_failed=0 alert=outside_window" in result.stdout
	assert not env["log"].exists()


@pytest.mark.parametrize(
	("probes", "extra", "expected"),
	[
		(None, {}, "accounts=0 gated=0 auth_failed=0 probe_failed=0 alert=no_probes"),
		([], {}, "alert=no_probes"),
		([_probe("ALPHA", 0.95, 0.1)], {"CLAUDE_POOL_HEALTH_ALERT_ENABLED": "false"}, "alert=disabled"),
		(None, {"CLAUDE_POOL_PROBES": "not json"}, "alert=invalid_probes"),
	],
)
def test_skips_never_fail_the_job(env: dict, probes: list | None, extra: dict, expected: str) -> None:
	result = _run(env, probes, **extra)
	assert result.returncode == 0 and expected in result.stdout
	assert not env["log"].exists()


def test_the_gate_comes_from_the_engine_config_when_not_overridden(env: dict, tmp_path: Path) -> None:
	config = tmp_path / "claude_engine.json"
	config.write_text(json.dumps({"gate_utilization": 0.5}), encoding="utf-8")
	values = {key: value for key, value in env["env"].items() if key != "CLAUDE_POOL_HEALTH_GATE"}
	result = subprocess.run(
		["bash", str(SCRIPT)], capture_output=True, text=True, timeout=60, check=False,
		env=dict(values, CLAUDE_ENGINE_CONFIG=str(config), CLAUDE_POOL_PROBES=json.dumps([_probe("ALPHA", 0.6, 0.1)])),
	)
	assert "gated=1" in result.stdout and "alert=sent" in result.stdout
	assert "at or above the 50% usage gate" in _sent_text(env)


def test_orchestrate_poll_runs_the_alert_right_after_the_pool_step() -> None:
	text = (REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml").read_text(encoding="utf-8")
	pool = text.index("        id: claude_pool\n")
	alert = text.index("      - name: Alert on Claude pool accounts at the usage gate\n")
	process = text.index("      - name: Process each tracking issue\n")
	assert pool < alert < process
	step = text[alert:process]
	assert "if: steps.find_tracking.outputs.has_work == 'true' && steps.ai_engine.outputs.any_claude == 'true'" in step
	assert "continue-on-error: true" in step
	assert "CLAUDE_POOL_PROBES: ${{ steps.claude_pool.outputs.probes || '[]' }}" in step
	assert "CLAUDE_POOL_HEALTH_ALERT_ENABLED: ${{ vars.CLAUDE_POOL_HEALTH_ALERT_ENABLED || 'true' }}" in step
	assert "CLAUDE_ENGINE_CONFIG: .codex-workflow-src/.github/ai/claude_engine.json" in step
	assert "TG_BOT_SECRET: ${{ secrets.TG_BOT_SECRET }}" in step
	assert "run: bash .codex-workflow-src/scripts/claude_pool_health_alert.sh" in step
	action = (REPO_ROOT / ".github" / "actions" / "claude-pool-token" / "action.yml").read_text(encoding="utf-8")
	assert re.search(r"^  probes:\n", action, re.M)


def test_ci_runs_this_file() -> None:
	ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
	assert "tests/test_claude_pool_health_alert.py" in ci
