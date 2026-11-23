"""Behaviour of the stage switch watch (issue #9994).

scripts/stage_switch_watch.py decides which registry switches are off and
which alert; scripts/stage_switch_watch.sh reads the variables listing and
sends the alerts. The wrapper runs from a temp copy of scripts/ with a stub
`gh` on PATH and a stub tg_helpers.sh that records each message.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PY = ROOT / "scripts" / "stage_switch_watch.py"
SCRIPT_SH = ROOT / "scripts" / "stage_switch_watch.sh"

sys.path.insert(0, str(ROOT / "scripts"))
import stage_switch_watch as ssw  # noqa: E402

NOW = "2026-11-23T06:00:00Z"


def _registry(tmp_path: Path, markers=None) -> Path:
	data = {
		"schema": "stage_switches.v1",
		"switches": [
			{"name": "FINAL_CI_FAILED_REDISPATCH_ENABLED", "default": "true", "stage": "Sends a failed final CI back to review."},
			{"name": "ALPHA_ENABLED", "default": "true", "stage": "Alpha stage."},
			{"name": "BETA_ENABLED", "default": "true", "stage": "Beta stage."},
			{"name": "DEPLOY_TOKEN_ENABLED", "default": "true", "stage": "Secret-like name."},
		],
		"off_on_purpose": markers or [],
	}
	path = tmp_path / "stage_switches.json"
	path.write_text(json.dumps(data), encoding="utf-8")
	return path


def _listing(*records) -> str:
	return "".join(json.dumps(record) + "\n" for record in records)


def _run_py(args, stdin=""):
	env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
	return subprocess.run(
		[sys.executable, str(SCRIPT_PY), *args],
		input=stdin, capture_output=True, text=True, env=env, check=False,
	)


def _snapshot(tmp_path: Path, registry: Path, *records) -> Path:
	result = _run_py(["snapshot", "--registry", str(registry)], _listing(*records))
	assert result.returncode == 0, result.stderr
	path = tmp_path / "snapshot.json"
	path.write_text(result.stdout, encoding="utf-8")
	return path


def _evaluate(registry: Path, snapshot: Path, now=NOW, wait="24"):
	result = _run_py(["evaluate", "--registry", str(registry), "--snapshot", str(snapshot), "--now", now, "--wait-hours", wait])
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout), result.stderr


def test_false_switch_with_true_default_is_reported_true_and_unset_are_not(tmp_path):
	registry = _registry(tmp_path)
	snapshot = _snapshot(
		tmp_path, registry,
		{"name": "FINAL_CI_FAILED_REDISPATCH_ENABLED", "value": "false", "updated_at": "2026-11-21T06:00:00Z"},
		{"name": "ALPHA_ENABLED", "value": "true", "updated_at": "2026-11-01T00:00:00Z"},
	)
	result, stderr = _evaluate(registry, snapshot)
	assert [entry["name"] for entry in result["off"]] == ["FINAL_CI_FAILED_REDISPATCH_ENABLED"]
	alert = result["alerts"][0]
	assert alert["display_value"] == "false"
	assert alert["default"] == "true"
	assert alert["stage"] == "Sends a failed final CI back to review."
	assert alert["off_since"] == "2026-11-21T06:00:00Z"
	assert alert["hours_off"] == 48.0
	assert "STAGE_SWITCH_OFF name=FINAL_CI_FAILED_REDISPATCH_ENABLED value=false default=true" in stderr
	assert "ALPHA_ENABLED" not in stderr and "BETA_ENABLED" not in stderr


def test_alert_waits_for_the_wait_time(tmp_path):
	registry = _registry(tmp_path)
	snapshot = _snapshot(tmp_path, registry, {"name": "ALPHA_ENABLED", "value": "false", "updated_at": "2026-11-23T00:00:00Z"})
	result, _ = _evaluate(registry, snapshot, wait="24")
	assert result["alerts"] == []
	assert result["off"][0]["status"] == "waiting"
	assert result["counts"] == {"off": 1, "waiting": 1, "on_purpose": 0, "alerts": 0}
	result, _ = _evaluate(registry, snapshot, now="2026-11-24T00:00:00Z", wait="24")
	assert [entry["name"] for entry in result["alerts"]] == ["ALPHA_ENABLED"]


def test_unknown_updated_at_counts_as_past_the_wait(tmp_path):
	registry = _registry(tmp_path)
	snapshot = _snapshot(tmp_path, registry, {"name": "ALPHA_ENABLED", "value": "false"})
	result, _ = _evaluate(registry, snapshot)
	assert result["alerts"][0]["off_since"] == "unknown"


def test_invalid_wait_hours_warns_and_uses_default(tmp_path):
	registry = _registry(tmp_path)
	snapshot = _snapshot(tmp_path, registry, {"name": "ALPHA_ENABLED", "value": "false", "updated_at": "2026-11-22T12:00:00Z"})
	result, stderr = _evaluate(registry, snapshot, wait="-3")
	assert "::warning::" in stderr
	assert result["alerts"] == []  # 18h < default 24h


def test_off_on_purpose_marker_holds_alert_until_expiry(tmp_path):
	registry = _registry(tmp_path, [{"name": "ALPHA_ENABLED", "until": "2026-11-30", "reason": "migration"}])
	snapshot = _snapshot(tmp_path, registry, {"name": "ALPHA_ENABLED", "value": "false", "updated_at": "2026-11-01T00:00:00Z"})
	result, _ = _evaluate(registry, snapshot)
	assert result["alerts"] == []
	assert result["off"][0]["status"] == "off_on_purpose"
	result, stderr = _evaluate(registry, snapshot, now="2026-11-30T00:00:00Z")
	assert result["alerts"][0]["marker"] == "expired"
	assert "marker=expired" in stderr


def test_invalid_marker_warns_and_switch_alerts(tmp_path):
	registry = _registry(tmp_path, [
		{"name": "ALPHA_ENABLED", "until": "someday", "reason": "x"},
		{"name": "NOT_LISTED_ENABLED", "until": "2099-01-01", "reason": "x"},
	])
	snapshot = _snapshot(tmp_path, registry, {"name": "ALPHA_ENABLED", "value": "false", "updated_at": "2026-11-01T00:00:00Z"})
	result, stderr = _evaluate(registry, snapshot)
	assert [entry["name"] for entry in result["alerts"]] == ["ALPHA_ENABLED"]
	assert stderr.count("::warning::") == 2


def test_values_are_sanitized_and_unlisted_variables_dropped(tmp_path):
	registry = _registry(tmp_path)
	result = _run_py(["snapshot", "--registry", str(registry)], _listing(
		{"name": "ALPHA_ENABLED", "value": "nope-s3cr3t", "updated_at": "2026-11-01T00:00:00Z"},
		{"name": "DEPLOY_TOKEN_ENABLED", "value": "false", "updated_at": "2026-11-01T00:00:00Z"},
		{"name": "OTHER_VARIABLE", "value": "very-private-value", "updated_at": "2026-11-01T00:00:00Z"},
	))
	assert result.returncode == 0, result.stderr
	assert "very-private-value" not in result.stdout and "OTHER_VARIABLE" not in result.stdout
	assert "nope-s3cr3t" not in result.stdout
	snapshot = json.loads(result.stdout)
	assert snapshot["ALPHA_ENABLED"]["display_value"] == "<non-boolean>"
	assert snapshot["DEPLOY_TOKEN_ENABLED"]["display_value"] == "<redacted>"
	assert ssw._display_value("FINAL_CI_FAILED_REDISPATCH_ENABLED", "FALSE") == "false"
	assert ssw._display_value("GH_PAT_ENABLED", "false") == "<redacted>"


def test_on_values_keep_a_consumer_on_value_from_alerting(tmp_path):
	registry = _registry(tmp_path)
	data = json.loads(registry.read_text(encoding="utf-8"))
	data["switches"][1]["on_values"] = ["1", "yes", "on"]  # ALPHA_ENABLED
	registry.write_text(json.dumps(data), encoding="utf-8")
	snapshot = _snapshot(
		tmp_path, registry,
		{"name": "ALPHA_ENABLED", "value": "YES", "updated_at": "2026-11-01T00:00:00Z"},
		{"name": "BETA_ENABLED", "value": "yes", "updated_at": "2026-11-01T00:00:00Z"},
	)
	result, _ = _evaluate(registry, snapshot)
	# ALPHA's consumers treat yes as on; BETA has no on_values, so yes is off.
	assert [entry["name"] for entry in result["off"]] == ["BETA_ENABLED"]


def test_invalid_on_values_exits_2(tmp_path):
	registry = _registry(tmp_path)
	data = json.loads(registry.read_text(encoding="utf-8"))
	data["switches"][1]["on_values"] = ["false"]
	registry.write_text(json.dumps(data), encoding="utf-8")
	result = _run_py(["snapshot", "--registry", str(registry)], "")
	assert result.returncode == 2


def test_invalid_registry_exits_2(tmp_path):
	bad = tmp_path / "bad.json"
	bad.write_text('{"schema": "other"}', encoding="utf-8")
	result = _run_py(["snapshot", "--registry", str(bad)], "")
	assert result.returncode == 2
	assert "::warning::" in result.stderr


# --- wrapper -----------------------------------------------------------------


def _wrapper_env(tmp_path: Path, gh_body: str, registry: Path | None = None, **extra) -> dict:
	scripts = tmp_path / "scripts"
	scripts.mkdir(exist_ok=True)
	shutil.copy(SCRIPT_SH, scripts / "stage_switch_watch.sh")
	shutil.copy(SCRIPT_PY, scripts / "stage_switch_watch.py")
	shutil.copy(registry or _registry(tmp_path), scripts / "stage_switches.json")
	sent = tmp_path / "sent.txt"
	(scripts / "tg_helpers.sh").write_text(
		"tg_alert_configured() { [ -n \"${TG_BOT_SECRET:-}\" ]; }\n"
		f"tg_send_msg() {{ printf '%s\\n---\\n' \"$1\" >> '{sent}'; printf '42'; }}\n",
		encoding="utf-8",
	)
	bindir = tmp_path / "bin"
	bindir.mkdir(exist_ok=True)
	gh = bindir / "gh"
	gh.write_text("#!/usr/bin/env bash\n" + gh_body + "\n", encoding="utf-8")
	gh.chmod(0o755)
	env = {
		"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
		"HOME": str(tmp_path),
		"RUNNER_TEMP": str(tmp_path / "runner"),
		"GITHUB_OUTPUT": str(tmp_path / "output.txt"),
		"GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
		"GITHUB_REPOSITORY": "owner/repo",
		"GITHUB_RUN_ID": "1",
		"GH_TOKEN": "x",
		"TG_BOT_SECRET": "bot",
		"STAGE_SWITCH_NOW": NOW,
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	(tmp_path / "runner").mkdir(exist_ok=True)
	env.update(extra)
	return env


def _wrapper(tmp_path: Path, command: str, env: dict):
	return subprocess.run(
		["bash", str(tmp_path / "scripts" / "stage_switch_watch.sh"), command],
		capture_output=True, text=True, env=env, check=False,
	)


GH_OFF = "printf '%s\\n' '{\"name\":\"ALPHA_ENABLED\",\"value\":\"false\",\"updated_at\":\"2026-11-20T00:00:00Z\"}' '{\"name\":\"OTHER\",\"value\":\"hidden-value\",\"updated_at\":\"2026-11-20T00:00:00Z\"}'"


def test_wrapper_alerts_once_and_not_again_same_day(tmp_path):
	env = _wrapper_env(tmp_path, GH_OFF)
	read = _wrapper(tmp_path, "read", env)
	assert read.returncode == 0, read.stderr
	assert "snapshot_ready=true" in Path(env["GITHUB_OUTPUT"]).read_text()
	assert "hidden-value" not in read.stdout + read.stderr
	assert not (tmp_path / "runner" / "stage-switch-watch" / "listing.jsonl").exists()

	report = _wrapper(tmp_path, "report", env)
	assert report.returncode == 0, report.stderr
	assert "STAGE_SWITCH_WATCH outcome=ok off=1 waiting=0 on_purpose=0 alerts=1 alert=sent" in report.stdout
	sent = (tmp_path / "sent.txt").read_text()
	assert "ALPHA_ENABLED" in sent and "Value: false (default true)" in sent and "Alpha stage." in sent
	assert "off_on_purpose" in sent
	assert "alerted=true" in Path(env["GITHUB_OUTPUT"]).read_text()
	assert (tmp_path / "runner" / "stage-switch-alert-day" / "day.txt").is_file()
	assert "ALPHA_ENABLED" in Path(env["GITHUB_STEP_SUMMARY"]).read_text()

	(tmp_path / "sent.txt").unlink()
	again = _wrapper(tmp_path, "report", dict(env, STAGE_SWITCH_ALERTED_TODAY="true"))
	assert again.returncode == 0
	assert "alert=already_sent_today" in again.stdout
	assert not (tmp_path / "sent.txt").exists()


def test_wrapper_failed_send_writes_no_day_marker(tmp_path):
	env = _wrapper_env(tmp_path, GH_OFF)
	# A tg_send_msg that returns no message id is a failed send.
	(tmp_path / "scripts" / "tg_helpers.sh").write_text(
		"tg_alert_configured() { return 0; }\ntg_send_msg() { return 1; }\n", encoding="utf-8",
	)
	assert _wrapper(tmp_path, "read", env).returncode == 0
	report = _wrapper(tmp_path, "report", env)
	assert report.returncode == 0
	assert "alerts=1 alert=not_delivered" in report.stdout
	assert "alerted=true" not in Path(env["GITHUB_OUTPUT"]).read_text()
	assert not (tmp_path / "runner" / "stage-switch-alert-day" / "day.txt").exists()


def test_wrapper_partial_delivery_warns_and_keeps_day_marker(tmp_path):
	gh_two_off = (
		"printf '%s\\n' '{\"name\":\"ALPHA_ENABLED\",\"value\":\"false\",\"updated_at\":\"2026-11-20T00:00:00Z\"}'"
		" '{\"name\":\"BETA_ENABLED\",\"value\":\"false\",\"updated_at\":\"2026-11-20T00:00:00Z\"}'"
	)
	env = _wrapper_env(tmp_path, gh_two_off)
	count = tmp_path / "count.txt"
	# The first send is delivered, the second fails.
	(tmp_path / "scripts" / "tg_helpers.sh").write_text(
		"tg_alert_configured() { return 0; }\n"
		f"tg_send_msg() {{ echo x >> '{count}'; [ \"$(wc -l < '{count}')\" -eq 1 ] && printf '42'; }}\n",
		encoding="utf-8",
	)
	assert _wrapper(tmp_path, "read", env).returncode == 0
	report = _wrapper(tmp_path, "report", env)
	assert report.returncode == 0
	assert "alerts=2 alert=not_delivered" in report.stdout
	assert "::warning::STAGE_SWITCH_WATCH delivered 1 of 2 alerts" in report.stdout
	assert "alerted=true" in Path(env["GITHUB_OUTPUT"]).read_text()


def test_wrapper_does_not_alert_under_wait_time(tmp_path):
	env = _wrapper_env(tmp_path, GH_OFF, STAGE_SWITCH_OFF_ALERT_HOURS="200")
	assert _wrapper(tmp_path, "read", env).returncode == 0
	report = _wrapper(tmp_path, "report", env)
	assert "waiting=1" in report.stdout and "alert=none" in report.stdout
	assert not (tmp_path / "sent.txt").exists()


def test_wrapper_telegram_unconfigured(tmp_path):
	env = _wrapper_env(tmp_path, GH_OFF, TG_BOT_SECRET="")
	_wrapper(tmp_path, "read", env)
	report = _wrapper(tmp_path, "report", env)
	assert report.returncode == 0
	assert "alert=telegram_unconfigured" in report.stdout


def test_wrapper_gh_failure_warns_and_exits_zero(tmp_path):
	env = _wrapper_env(tmp_path, "echo 'HTTP 403' >&2; exit 1")
	read = _wrapper(tmp_path, "read", env)
	assert read.returncode == 0
	assert "::warning::" in read.stdout
	assert "STAGE_SWITCH_WATCH outcome=read_failed" in read.stdout
	assert "HTTP 403" in read.stdout  # gh's error line is shown
	assert "snapshot_ready=false" in Path(env["GITHUB_OUTPUT"]).read_text()


def test_wrapper_missing_token_warns_and_exits_zero(tmp_path):
	env = _wrapper_env(tmp_path, "echo should-not-run; exit 9", GH_TOKEN="")
	read = _wrapper(tmp_path, "read", env)
	assert read.returncode == 0
	assert "outcome=read_failed reason=no_token" in read.stdout


def test_wrapper_invalid_registry_warns_and_exits_zero(tmp_path):
	bad = tmp_path / "bad.json"
	bad.write_text("{not json", encoding="utf-8")
	env = _wrapper_env(tmp_path, GH_OFF, registry=bad)
	read = _wrapper(tmp_path, "read", env)
	assert read.returncode == 0
	assert "STAGE_SWITCH_WATCH outcome=registry_invalid" in read.stdout


def test_wrapper_disabled(tmp_path):
	env = _wrapper_env(tmp_path, "exit 9", STAGE_SWITCH_WATCH_ENABLED="false")
	read = _wrapper(tmp_path, "read", env)
	assert read.returncode == 0
	assert "STAGE_SWITCH_WATCH outcome=disabled" in read.stdout
