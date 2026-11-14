"""Contract tests for alert inbox capture and routing in scripts/tg_helpers.sh.

Alert triage plan, Phase 1 (docs/plans/alert-triage-auto-heal-plan.md):
`tg_send_msg` captures alerts through scripts/alert_inbox_capture.sh when the
switches in the trusted `.github/ai/alert_inbox.json` say so. The shipped
switches are off, and with them off `tg_send_msg` must behave exactly as
before (one Telegram call with the same argv, the same stdout and stderr).

The harness puts a fake `curl` on PATH that logs each call (argv, the
--data-binary body file, whether the -H @file held the bearer token) and
answers Telegram, the dispatch endpoint and the Contents API from a script.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BOT_TOKEN = "bot-sentinel-token-a1b2c3"
GH_TOKEN_VALUE = "gh-sentinel-token-d4e5f6"
SELF_REPO = "shubhodeep1/coding-workflows"
RECORD_KEYS = sorted([
	"id", "captured_at", "level", "source_repo", "workflow", "run_id", "run_url",
	"job", "message", "release_sha", "sent_to_telegram",
])

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq binary not available")

FAKE_CURL = r'''#!__PYTHON__
import json
import os
import sys

state = os.environ["FAKE_CURL_STATE"]
args = sys.argv[1:]
method = None
out_file = None
write_out = None
headers = []
data = []
body_ref = None
url = ""
i = 0
while i < len(args):
	arg = args[i]
	if arg in ("-X", "--request"):
		method = args[i + 1]; i += 2
	elif arg == "-o":
		out_file = args[i + 1]; i += 2
	elif arg == "-w":
		write_out = args[i + 1]; i += 2
	elif arg == "-H":
		headers.append(args[i + 1]); i += 2
	elif arg in ("-d", "--data", "--data-urlencode"):
		data.append(args[i + 1]); i += 2
	elif arg == "--data-binary":
		body_ref = args[i + 1]; i += 2
	elif arg == "--max-time":
		i += 2
	elif arg.startswith("-"):
		i += 1
	else:
		url = arg; i += 1
if method is None:
	method = "POST" if (data or body_ref) else "GET"
body = None
if body_ref and body_ref.startswith("@"):
	with open(body_ref[1:], encoding="utf-8") as fh:
		body = fh.read()
header_text = ""
for item in headers:
	if item.startswith("@"):
		with open(item[1:], encoding="utf-8") as fh:
			header_text += fh.read()
	else:
		header_text += item + "\n"

if "api.telegram.org" in url:
	kind = "telegram"
elif url.endswith("/dispatches"):
	kind = "dispatch"
elif "/contents/" in url and "?ref=" in url:
	kind = "contents_get"
elif "/contents/" in url:
	kind = "contents"
elif "/git/ref/heads/" in url:
	kind = "ref_get"
elif url.endswith("/git/commits"):
	kind = "commits"
elif url.endswith("/git/trees"):
	kind = "trees"
elif url.endswith("/git/refs"):
	kind = "refs"
else:
	kind = "other"

with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as fh:
	fh.write(json.dumps({
		"method": method, "kind": kind, "url": url, "argv": args, "body": body,
		"auth_ok": ("Authorization: Bearer " + os.environ.get("EXPECT_TOKEN", "?")) in header_text,
	}) + "\n")

defaults = {
	"telegram": [200, '{"ok":true,"result":{"message_id":77}}'],
	"dispatch": [204, ""],
	"contents": [201, '{"content":{}}'],
	"contents_get": [404, '{"message":"Not Found"}'],
	"ref_get": [200, '{"ref":"refs/heads/ai/alert-inbox"}'],
	"commits": [201, '{"sha":"' + "c" * 40 + '"}'],
	"trees": [201, '{"sha":"' + "d" * 40 + '"}'],
	"refs": [201, '{"ref":"refs/heads/ai/alert-inbox"}'],
	"other": [599, ""],
}
queues_path = os.path.join(state, "queues.json")
with open(queues_path, encoding="utf-8") as fh:
	queues = json.load(fh)
queue = queues.get(kind, [])
entry = queue.pop(0) if queue else defaults[kind]
with open(queues_path, "w", encoding="utf-8") as fh:
	json.dump(queues, fh)
if isinstance(entry, str) and entry.startswith("exit:"):
	sys.exit(int(entry.split(":", 1)[1]))
status, resp = entry
if out_file:
	with open(out_file, "w", encoding="utf-8") as fh:
		fh.write(resp)
else:
	sys.stdout.write(resp)
if write_out is not None:
	sys.stdout.write(write_out.replace("%{http_code}", str(status)))
sys.exit(0)
'''


def _write_config(root: Path, capture, route, raw: str | None = None) -> Path:
	cfg = root / ".github" / "ai" / "alert_inbox.json"
	cfg.parent.mkdir(parents=True, exist_ok=True)
	if raw is None:
		raw = json.dumps({"capture_enabled": capture, "route_warning_error_to_inbox": route})
	cfg.write_text(raw, encoding="utf-8")
	return cfg


def _env(tmp_path: Path, mode: str | None, **overrides: str) -> tuple[dict, Path]:
	state = tmp_path / "state"
	bindir = tmp_path / "bin"
	state.mkdir(exist_ok=True)
	bindir.mkdir(exist_ok=True)
	fake = bindir / "curl"
	fake.write_text(FAKE_CURL.replace("__PYTHON__", sys.executable), encoding="utf-8")
	fake.chmod(0o755)
	sleep = bindir / "sleep"
	sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
	sleep.chmod(0o755)
	if not (state / "queues.json").exists():
		(state / "queues.json").write_text("{}", encoding="utf-8")
	support = tmp_path / "support"
	support.mkdir(exist_ok=True)
	if mode is not None:
		_write_config(support, mode in ("capture", "route"), mode == "route")
	workspace = tmp_path / "ws"
	workspace.mkdir(exist_ok=True)
	env = {k: v for k, v in os.environ.items() if not k.startswith(
		("TG_", "ALERT_MSG_LEVEL", "_TG_", "_ALERT_", "GH_", "GITHUB_", "SUPPORT_ROOT_DIR", "RUNNER_"))}
	env.update({
		"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
		"FAKE_CURL_STATE": str(state),
		"EXPECT_TOKEN": GH_TOKEN_VALUE,
		"GH_TOKEN": GH_TOKEN_VALUE,
		"GITHUB_REPOSITORY": SELF_REPO,
		"GITHUB_RUN_ID": "12345",
		"GITHUB_WORKFLOW": "CI",
		"GITHUB_JOB": "build",
		"GITHUB_SERVER_URL": "https://github.com",
		"GITHUB_API_URL": "https://api.github.com",
		"GITHUB_WORKSPACE": str(workspace),
		"SUPPORT_ROOT_DIR": str(support),
		"TG_BOT_SECRET": BOT_TOKEN,
		"TG_ADMIN_CHAT_ID": "-1001",
		"ALERT_MSG_LEVEL": "DEBUG",
		"GH_RETRY_MAX_ATTEMPTS": "1",
		"TMPDIR": str(tmp_path),
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	for key, value in overrides.items():
		if value is None:
			env.pop(key, None)
		else:
			env[key] = value
	return env, state


def _run(env: dict, script: str, helpers: str = "scripts/tg_helpers.sh") -> subprocess.CompletedProcess:
	harness = f'set -euo pipefail\nsource "{helpers}"\n{script}\necho HARNESS_OK\n'
	result = subprocess.run(["bash", "-c", harness], cwd=REPO_ROOT, env=env, capture_output=True, text=True,
		check=False, timeout=120)
	assert result.returncode == 0, result.stderr
	assert "HARNESS_OK" in result.stdout, result.stderr
	for secret in (BOT_TOKEN, GH_TOKEN_VALUE):
		assert secret not in result.stdout
		assert secret not in result.stderr
	return result


def _send(env: dict, level: str | None, msg: str = "deploy broke", fn: str = "tg_send_msg",
		helpers: str = "scripts/tg_helpers.sh") -> subprocess.CompletedProcess:
	env = dict(env)
	env["T_MSG"] = msg
	if level is None:
		call = f'out="$({fn} "$T_MSG")"'
	else:
		env["T_LEVEL"] = level
		call = f'out="$({fn} "$T_MSG" "$T_LEVEL")"'
	return _run(env, f"{call}\nprintf 'OUT=[%s]\\n' \"$out\"", helpers)


def _calls(state: Path) -> list[dict]:
	log = state / "calls.jsonl"
	if not log.exists():
		return []
	return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _kinds(calls: list[dict], kind: str) -> list[dict]:
	return [c for c in calls if c["kind"] == kind]


def _out(result: subprocess.CompletedProcess) -> str:
	match = re.search(r"^OUT=\[(.*)\]$", result.stdout, re.M)
	assert match, result.stdout
	return match.group(1)


def _record(call: dict) -> dict:
	payload = json.loads(call["body"])
	assert payload["event_type"] == "alert-inbox"
	assert payload["client_payload"]["schema"] == "alert_inbox.v1"
	assert sorted(payload["client_payload"]) == ["record", "schema"]
	return payload["client_payload"]["record"]


# ---------------------------------------------------------------------------
# Routing table
# ---------------------------------------------------------------------------

# (mode, level) -> (telegram calls, dispatch calls, stdout id, sent_to_telegram)
ROUTING = {
	("off", "WARNING"): (1, 0, "77", None),
	("off", "ERROR"): (1, 0, "77", None),
	("off", "CRITICAL"): (1, 0, "77", None),
	("off", "DEBUG"): (1, 0, "77", None),
	("capture", "WARNING"): (1, 1, "77", True),
	("capture", "ERROR"): (1, 1, "77", True),
	("capture", "CRITICAL"): (1, 1, "77", True),
	("capture", "DEBUG"): (1, 0, "77", None),
	("route", "WARNING"): (0, 1, "", False),
	("route", "ERROR"): (0, 1, "", False),
	("route", "CRITICAL"): (1, 1, "77", True),
	("route", "DEBUG"): (1, 0, "77", None),
}


@pytest.mark.parametrize("mode,level", sorted(ROUTING))
def test_routing_table(tmp_path: Path, mode: str, level: str) -> None:
	env, state = _env(tmp_path, mode)
	result = _send(env, level)
	tg_calls, dispatch_calls, out, sent = ROUTING[(mode, level)]
	calls = _calls(state)
	assert len(_kinds(calls, "telegram")) == tg_calls
	assert len(_kinds(calls, "dispatch")) == dispatch_calls
	assert _out(result) == out
	if dispatch_calls:
		dispatch = _kinds(calls, "dispatch")[0]
		assert dispatch["url"] == f"https://api.github.com/repos/{SELF_REPO}/dispatches"
		assert dispatch["method"] == "POST"
		assert dispatch["auth_ok"] is True
		record = _record(dispatch)
		assert sorted(record) == RECORD_KEYS
		assert record["sent_to_telegram"] is sent
		assert record["level"] == level
		assert record["message"] == "deploy broke"
		assert "ALERT_INBOX capture outcome=sent reason=ok" in result.stderr
	else:
		assert "ALERT_INBOX" not in result.stderr


def test_lowercase_level_is_captured_upper_cased(tmp_path: Path) -> None:
	env, state = _env(tmp_path, "capture")
	_send(env, "error")
	record = _record(_kinds(_calls(state), "dispatch")[0])
	assert record["level"] == "ERROR"


def test_default_level_is_critical_and_captured(tmp_path: Path) -> None:
	env, state = _env(tmp_path, "route")
	result = _send(env, None)
	calls = _calls(state)
	assert len(_kinds(calls, "telegram")) == 1
	assert _record(_kinds(calls, "dispatch")[0])["level"] == "CRITICAL"
	assert _out(result) == "77"


def test_filtered_level_is_neither_sent_nor_captured(tmp_path: Path) -> None:
	env, state = _env(tmp_path, "route", ALERT_MSG_LEVEL="ERROR")
	result = _send(env, "WARNING")
	assert _calls(state) == []
	assert _out(result) == ""


def test_capture_mode_without_telegram_credentials_records_not_sent(tmp_path: Path) -> None:
	env, state = _env(tmp_path, "capture", TG_BOT_SECRET=None)
	result = _send(env, "ERROR")
	calls = _calls(state)
	assert _kinds(calls, "telegram") == []
	assert _record(_kinds(calls, "dispatch")[0])["sent_to_telegram"] is False
	assert _out(result) == ""


# ---------------------------------------------------------------------------
# Fail open
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dispatch_entry,reason", [
	([500, '{"message":"boom"}'], "http_500"),
	([403, '{"message":"forbidden"}'], "http_403"),
	("exit:28", "curl_error"),
])
def test_route_mode_falls_back_to_telegram_when_capture_fails(tmp_path: Path, dispatch_entry, reason: str) -> None:
	env, state = _env(tmp_path, "route")
	(state / "queues.json").write_text(json.dumps({"dispatch": [dispatch_entry]}), encoding="utf-8")
	result = _send(env, "WARNING")
	calls = _calls(state)
	assert len(_kinds(calls, "dispatch")) == 1
	assert len(_kinds(calls, "telegram")) == 1
	assert _out(result) == "77"
	assert f"ALERT_INBOX capture outcome=failed reason={reason}" in result.stderr


@pytest.mark.parametrize("override,reason", [
	({"GH_TOKEN": None}, "no_token"),
	({"GITHUB_RUN_ID": None}, "no_run_context"),
	({"GITHUB_REPOSITORY": "bad repo"}, "no_run_context"),
])
def test_route_mode_without_context_sends_to_telegram(tmp_path: Path, override: dict, reason: str) -> None:
	env, state = _env(tmp_path, "route", **override)
	result = _send(env, "ERROR")
	calls = _calls(state)
	assert _kinds(calls, "dispatch") == []
	assert len(_kinds(calls, "telegram")) == 1
	assert _out(result) == "77"
	assert f"ALERT_INBOX capture outcome=failed reason={reason}" in result.stderr


def test_capture_failure_never_fails_a_set_e_caller(tmp_path: Path) -> None:
	env, state = _env(tmp_path, "capture")
	(state / "queues.json").write_text(json.dumps({"dispatch": ["exit:7"]}), encoding="utf-8")
	result = _send(env, "CRITICAL")
	assert _out(result) == "77"
	assert "reason=curl_error" in result.stderr


def test_missing_capture_script_keeps_alerts_on_telegram(tmp_path: Path) -> None:
	staged = tmp_path / "staged" / "scripts"
	staged.mkdir(parents=True)
	shutil.copy2(REPO_ROOT / "scripts" / "tg_helpers.sh", staged / "tg_helpers.sh")
	env, state = _env(tmp_path, "route")
	result = _send(env, "WARNING", helpers=str(staged / "tg_helpers.sh"))
	calls = _calls(state)
	assert len(_kinds(calls, "telegram")) == 1
	assert _kinds(calls, "dispatch") == []
	assert _out(result) == "77"
	assert result.stderr == ""


def test_missing_truncate_script_fails_capture_and_sends(tmp_path: Path) -> None:
	staged = tmp_path / "staged" / "scripts"
	staged.mkdir(parents=True)
	for name in ("tg_helpers.sh", "alert_inbox_capture.sh"):
		shutil.copy2(REPO_ROOT / "scripts" / name, staged / name)
	env, state = _env(tmp_path, "route")
	result = _send(env, "WARNING", helpers=str(staged / "tg_helpers.sh"))
	assert _kinds(_calls(state), "dispatch") == []
	assert _out(result) == "77"
	assert "ALERT_INBOX capture outcome=failed reason=truncate_unavailable" in result.stderr


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def _mode(env: dict, helpers: str = "scripts/tg_helpers.sh") -> str:
	result = _run(env, 'printf "MODE=[%s]\\n" "$(alert_inbox_mode)"', helpers)
	match = re.search(r"MODE=\[(.*)\]", result.stdout)
	assert match
	assert result.stderr == ""
	return match.group(1)


def test_shipped_config_is_off() -> None:
	cfg = json.loads((REPO_ROOT / ".github" / "ai" / "alert_inbox.json").read_text(encoding="utf-8"))
	assert cfg["capture_enabled"] is False
	assert cfg["route_warning_error_to_inbox"] is False
	for key in ("pipeline_self_files", "break_glass_allowed_paths", "break_glass_denied_paths"):
		assert isinstance(cfg[key], list) and cfg[key]
		assert all(isinstance(item, str) and item for item in cfg[key])


def test_shipped_config_resolves_off(tmp_path: Path) -> None:
	env, _ = _env(tmp_path, None, SUPPORT_ROOT_DIR=None)
	assert _mode(env) == "off"


@pytest.mark.parametrize("raw", [
	"not json",
	"[]",
	'{"capture_enabled": "true", "route_warning_error_to_inbox": false}',
	'{"capture_enabled": 1, "route_warning_error_to_inbox": true}',
	'{"capture_enabled": true}',
	'{"capture_enabled": true, "route_warning_error_to_inbox": "yes"}',
	'{"capture_enabled": false, "route_warning_error_to_inbox": true}',
])
def test_invalid_or_off_config_is_off(tmp_path: Path, raw: str) -> None:
	env, state = _env(tmp_path, None)
	_write_config(tmp_path / "support", None, None, raw=raw)
	assert _mode(env) == "off"
	_send(env, "WARNING")
	calls = _calls(state)
	assert _kinds(calls, "dispatch") == []
	assert len(_kinds(calls, "telegram")) == 1


def test_missing_config_everywhere_is_off(tmp_path: Path) -> None:
	staged = tmp_path / "staged" / "scripts"
	staged.mkdir(parents=True)
	for name in ("tg_helpers.sh", "alert_inbox_capture.sh", "truncate_to_utf8_byte_cap.py"):
		shutil.copy2(REPO_ROOT / "scripts" / name, staged / name)
	env, _ = _env(tmp_path, None, SUPPORT_ROOT_DIR=None)
	assert _mode(env, helpers=str(staged / "tg_helpers.sh")) == "off"


def test_symlinked_config_is_ignored(tmp_path: Path) -> None:
	env, _ = _env(tmp_path, None)
	real = _write_config(tmp_path / "elsewhere", True, True)
	link = tmp_path / "support" / ".github" / "ai" / "alert_inbox.json"
	link.parent.mkdir(parents=True, exist_ok=True)
	link.symlink_to(real)
	# Falls through to the repository's shipped (off) config.
	assert _mode(env) == "off"


def test_support_root_order(tmp_path: Path) -> None:
	env, _ = _env(tmp_path, "capture")
	_write_config(tmp_path / "ws" / ".codex-workflow-src", True, True)
	assert _mode(env) == "capture"
	env.pop("SUPPORT_ROOT_DIR")
	assert _mode(env) == "route"


def test_callers_checkout_config_is_never_read(tmp_path: Path) -> None:
	env, _ = _env(tmp_path, None, SUPPORT_ROOT_DIR=None)
	caller = tmp_path / "caller"
	_write_config(caller, True, True)
	result = subprocess.run(
		["bash", "-c", f'set -euo pipefail\nsource "{REPO_ROOT}/scripts/tg_helpers.sh"\nprintf "MODE=[%s]\\n" "$(alert_inbox_mode)"\n'],
		cwd=caller, env=env, capture_output=True, text=True, check=False, timeout=60)
	assert result.returncode == 0, result.stderr
	assert "MODE=[off]" in result.stdout


# ---------------------------------------------------------------------------
# Off-switch byte identity and the uncaptured senders
# ---------------------------------------------------------------------------

def _expected_telegram_argv(text: str) -> list[str]:
	return [
		"-s", "-X", "POST", f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
		"-d", "chat_id=-1001",
		"-d", "disable_web_page_preview=true",
		"--data-urlencode", f"text={text}",
	]


@pytest.mark.parametrize("level,prefix", [("WARNING", "⚠️ WARNING: "), ("ERROR", "❌ ERROR: "), ("CRITICAL", "🚨 CRITICAL: ")])
def test_off_switch_is_byte_identical(tmp_path: Path, level: str, prefix: str) -> None:
	env, state = _env(tmp_path, None, SUPPORT_ROOT_DIR=None, ALERT_MSG_LEVEL=None)
	result = _send(env, level, msg="line one\nline two")
	calls = _calls(state)
	assert len(calls) == 1
	assert calls[0]["argv"] == _expected_telegram_argv(prefix + "line one\nline two")
	assert result.stdout == "OUT=[77]\nHARNESS_OK\n"
	assert result.stderr == ""


def test_off_switch_curl_failure_warning_unchanged(tmp_path: Path) -> None:
	env, state = _env(tmp_path, None, SUPPORT_ROOT_DIR=None)
	(state / "queues.json").write_text(json.dumps({"telegram": ["exit:6"]}), encoding="utf-8")
	result = _send(env, "ERROR")
	assert _out(result) == ""
	assert result.stderr == "::warning::Telegram send failed\n"


def test_off_switch_without_credentials_is_silent(tmp_path: Path) -> None:
	env, state = _env(tmp_path, None, SUPPORT_ROOT_DIR=None, TG_BOT_SECRET=None)
	result = _send(env, "ERROR")
	assert _calls(state) == []
	assert result.stdout == "OUT=[]\nHARNESS_OK\n"
	assert result.stderr == ""


@pytest.mark.parametrize("fn,level", [("tg_send_msg_direct", "WARNING"), ("tg_send_msg_direct", None), ("tg_send_report_msg", None)])
def test_uncaptured_senders_never_dispatch(tmp_path: Path, fn: str, level: str | None) -> None:
	env, state = _env(tmp_path, "route")
	result = _send(env, level, fn=fn)
	calls = _calls(state)
	assert _kinds(calls, "dispatch") == []
	assert len(_kinds(calls, "telegram")) == 1
	assert _out(result) == "77"
	assert "ALERT_INBOX" not in result.stderr


def test_direct_sender_matches_send_argv_and_filter(tmp_path: Path) -> None:
	env, state = _env(tmp_path, "route")
	_send(env, "ERROR", fn="tg_send_msg_direct")
	assert _calls(state)[0]["argv"] == _expected_telegram_argv("❌ ERROR: deploy broke")
	(tmp_path / "second").mkdir(exist_ok=True)
	env2, state2 = _env(tmp_path / "second", "route", ALERT_MSG_LEVEL="CRITICAL")
	result = _send(env2, "ERROR", fn="tg_send_msg_direct")
	assert _calls(state2) == []
	assert _out(result) == ""


# ---------------------------------------------------------------------------
# Payload safety
# ---------------------------------------------------------------------------

def test_hostile_message_is_literal_capped_and_token_free(tmp_path: Path) -> None:
	env, state = _env(tmp_path, "capture")
	marker = tmp_path / "pwned"
	hostile = f'$(touch {marker}) `touch {marker}` "dq" \'sq\' \\ ;|&' + "é" * 3000 + "tail"
	_send(env, "ERROR", msg=hostile)
	assert not marker.exists()
	calls = _calls(state)
	record = _record(_kinds(calls, "dispatch")[0])
	encoded = record["message"].encode("utf-8")
	assert len(encoded) <= 4000
	assert len(encoded) >= 3998
	assert hostile.startswith(record["message"])
	for call in calls:
		assert all(GH_TOKEN_VALUE not in arg for arg in call["argv"])
		assert all(hostile[:40] not in arg for arg in call["argv"] if call["kind"] == "dispatch")


def test_record_fields(tmp_path: Path) -> None:
	env, state = _env(tmp_path, "capture", GITHUB_WORKFLOW="W" * 300, GITHUB_JOB="j" * 150)
	_send(env, "WARNING")
	record = _record(_kinds(_calls(state), "dispatch")[0])
	assert re.fullmatch(r"[0-9a-f]{32}", record["id"])
	assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", record["captured_at"])
	assert record["source_repo"] == SELF_REPO
	assert record["run_id"] == "12345"
	assert record["run_url"] == f"https://github.com/{SELF_REPO}/actions/runs/12345"
	assert record["workflow"] == "W" * 200
	assert record["job"] == "j" * 100
	# The support root in this test is not a git checkout.
	assert record["release_sha"] == ""


# ---------------------------------------------------------------------------
# End to end: one captured alert lands as one record on the inbox branch
# ---------------------------------------------------------------------------

def test_captured_alert_is_stored_as_one_record(tmp_path: Path) -> None:
	env, state = _env(tmp_path, "capture")
	_send(env, "ERROR", msg="build exploded")
	record_call = _kinds(_calls(state), "dispatch")[0]
	payload = json.loads(record_call["body"])
	event = tmp_path / "event.json"
	event.write_text(json.dumps({"action": "alert-inbox", "client_payload": payload["client_payload"]}), encoding="utf-8")
	(state / "calls.jsonl").unlink()
	store_env = dict(env)
	store_env.pop("GH_TOKEN")
	store_env.update({"GITHUB_TOKEN": GH_TOKEN_VALUE, "GITHUB_EVENT_PATH": str(event)})
	result = subprocess.run(["bash", "scripts/alert_inbox_store.sh"], cwd=REPO_ROOT, env=store_env,
		capture_output=True, text=True, check=False, timeout=120)
	assert result.returncode == 0, result.stderr
	assert "ALERT_INBOX store outcome=stored reason=ok" in result.stderr
	calls = _calls(state)
	puts = [c for c in calls if c["method"] == "PUT"]
	assert len(puts) == 1
	assert len(calls) == 1
	record = payload["client_payload"]["record"]
	ts = record["captured_at"]
	expected_prefix = f"https://api.github.com/repos/{SELF_REPO}/contents/inbox/{ts[0:4]}/{ts[5:7]}/{ts[8:10]}/{ts[11:13]}/"
	assert puts[0]["url"].startswith(expected_prefix)
	assert puts[0]["url"].endswith(f"-shubhodeep1-coding-workflows-12345-{record['id'][:8]}.json")
	body = json.loads(puts[0]["body"])
	assert body["branch"] == "ai/alert-inbox"
	assert json.loads(base64.b64decode(body["content"])) == record
