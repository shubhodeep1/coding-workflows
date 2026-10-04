#!/usr/bin/env python3
"""scripts/claude_pool_token.sh and .github/actions/claude-pool-token (plan Phase 4).

A stub HTTP server plays the Actions OIDC endpoint and the claude-pool-broker
Worker on loopback; a fake `claude` answers the Haiku probe from a
per-token usage table. Checks the pool layout scripts/ai_engine.sh reads,
the least-used ordering, every available=false reason, masking, and the
post-step cleanup. Also asserts that no coding-workflows workflow or
template references the CLAUDE_POOL_TOKEN_* secrets, which live only in
shubhodeep1/claude-workers.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import stat
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "claude_pool_token.sh"
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "claude-pool-token"
REQUEST_TOKEN = "runner-request-token-123"
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJyZXBvc2l0b3J5IjoieCJ9.c2lnbmF0dXJlc2lnbmF0dXJl"

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in os.environ
assert "ACTIONS_ID_TOKEN_REQUEST_URL" not in os.environ
assert "GH_TOKEN" not in os.environ
assert "GITHUB_TOKEN" not in os.environ
table = json.loads(os.environ["FAKE_PROBE_TABLE"])
entry = table.get(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""), {"auth": True})
def emit(event):
	print(json.dumps(event))
if entry.get("auth"):
	emit({"type": "result", "subtype": "success", "is_error": True, "result": "Failed to authenticate. API Error: 401 OAuth access token is invalid."})
	sys.exit(1)
emit({"type": "rate_limit_event", "rate_limit_info": {"status": entry.get("status", "allowed"), "unifiedWindows": {
	"five_hour": {"utilization": entry["five"], "resetsAt": 1000},
	"seven_day": {"utilization": entry["seven"], "resetsAt": 2000}}}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": "OK"})
'''


class _Stub(http.server.BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.0"
	broker_status = 200
	broker_body: dict = {}
	seen: list[dict] = []

	def log_message(self, *_args):
		pass

	def _send(self, status: int, body: dict) -> None:
		payload = json.dumps(body).encode()
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(payload)))
		self.end_headers()
		self.wfile.write(payload)

	def do_GET(self):
		_Stub.seen.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
		if self.path.startswith("/oidc?") and self.headers.get("Authorization") == f"bearer {REQUEST_TOKEN}":
			self._send(200, {"value": JWT})
		else:
			self._send(401, {})

	def do_POST(self):
		_Stub.seen.append({"method": "POST", "path": self.path, "auth": self.headers.get("Authorization")})
		if self.path == "/v1/pool" and self.headers.get("Authorization") == f"Bearer {JWT}":
			self._send(_Stub.broker_status, _Stub.broker_body)
		else:
			self._send(403, {"error": "missing_token"})


@pytest.fixture()
def env(tmp_path: Path):
	server = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	port = server.server_address[1]
	_Stub.seen = []
	_Stub.broker_status = 200
	_Stub.broker_body = {
		"accounts": [{"name": "ALPHA", "token": "tok-alpha\n"}, {"name": "BETA", "token": " tok-beta "}],
		"probe_model": "claude-haiku-4-5-20251001",
		"gate": 0.9,
	}
	config = tmp_path / "claude_engine.json"
	config.write_text(json.dumps({"broker_url": f"http://127.0.0.1:{port}/v1/pool", "gate_utilization": 0.9}), encoding="utf-8")
	fake_bin = tmp_path / "bin"
	fake_bin.mkdir()
	fake = fake_bin / "claude"
	fake.write_text(FAKE_CLAUDE, encoding="utf-8")
	fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
	runner_temp = tmp_path / "rt"
	runner_temp.mkdir()
	values = {
		key: value
		for key, value in os.environ.items()
		if not key.startswith(("CLAUDE_", "ACTIONS_ID_TOKEN", "GITHUB_OUTPUT")) and key != "GITHUB_ACTIONS" and key.lower() not in ("http_proxy", "https_proxy", "all_proxy")
	}
	values.update(
		{
			"PATH": f"{fake_bin}:{os.environ['PATH']}",
			# Most cases exercise the loopback broker outside Actions; the Actions-only rejection test overrides this.
			"GITHUB_ACTIONS": "false",
			"RUNNER_TEMP": str(runner_temp),
			"GITHUB_OUTPUT": str(tmp_path / "output.txt"),
			"CLAUDE_ENGINE_CONFIG": str(config),
			"ACTIONS_ID_TOKEN_REQUEST_URL": f"http://127.0.0.1:{port}/oidc?api-version=2.0",
			"ACTIONS_ID_TOKEN_REQUEST_TOKEN": REQUEST_TOKEN,
			"FAKE_PROBE_TABLE": json.dumps({"tok-alpha": {"five": 0.5, "seven": 0.3}, "tok-beta": {"five": 0.2, "seven": 0.1}}),
			"NO_PROXY": "127.0.0.1,localhost",
			"no_proxy": "127.0.0.1,localhost",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	yield {"env": values, "tmp": tmp_path, "pool": runner_temp / "claude-pool", "config": config}
	server.shutdown()
	server.server_close()


def _run(env: dict, **extra: str) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
	values = dict(env["env"], **extra)
	for key in [k for k, v in values.items() if v is None]:
		values.pop(key)
	result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=values, timeout=120, check=False)
	outputs: dict[str, str] = {}
	output_path = Path(values["GITHUB_OUTPUT"])
	if output_path.exists():
		for line in output_path.read_text(encoding="utf-8").splitlines():
			key, _, value = line.partition("=")
			outputs[key] = value
	return result, outputs


def _without_masks(text: str) -> str:
	return "\n".join(line for line in text.splitlines() if not line.startswith("::add-mask::"))


def test_selected_pool_is_ordered_least_used_first(env: dict) -> None:
	result, outputs = _run(env, GH_TOKEN="probe-must-not-see", GITHUB_TOKEN="probe-must-not-see")
	assert result.returncode == 0, result.stderr
	assert outputs == {"available": "true", "reason": "selected", "accounts": "2", "pool_dir": str(env["pool"])}
	pool = env["pool"]
	assert (pool / "order").read_text(encoding="utf-8") == "BETA\nALPHA\n"
	assert (pool / "tokens" / "ALPHA").read_text(encoding="utf-8") == "tok-alpha"
	assert (pool / "tokens" / "BETA").read_text(encoding="utf-8") == "tok-beta"
	assert (pool / "token").read_text(encoding="utf-8") == "tok-beta"
	for path in (pool, pool / "tokens"):
		assert stat.S_IMODE(path.stat().st_mode) == 0o700
	for path in (pool / "tokens" / "ALPHA", pool / "tokens" / "BETA", pool / "token", pool / "order"):
		assert stat.S_IMODE(path.stat().st_mode) == 0o600
	# Every secret is masked, and nothing else prints it.
	assert f"::add-mask::{JWT}" in result.stdout
	assert "::add-mask::tok-alpha" in result.stdout and "::add-mask::tok-beta" in result.stdout
	rest = _without_masks(result.stdout + result.stderr)
	for secret in (JWT, "tok-alpha", "tok-beta", REQUEST_TOKEN):
		assert secret not in rest
	assert "CLAUDE_POOL probe account=BETA five_hour=0.2 seven_day=0.1 status=allowed error=None" in result.stdout
	assert "CLAUDE_POOL available=true reason=selected accounts=2" in result.stdout
	assert not list((pool / "work").glob("*.json"))
	assert [entry["method"] for entry in _Stub.seen] == ["GET", "POST"]
	assert "audience=coding-workflows-claude-pool" in _Stub.seen[0]["path"]


def test_the_pool_layout_is_what_ai_engine_reads(env: dict) -> None:
	_run(env)
	listed = subprocess.run(
		["bash", "-c", f"source {REPO_ROOT / 'scripts' / 'ai_engine.sh'}; ai_engine_accounts"],
		capture_output=True, text=True, env=dict(env["env"], CLAUDE_ENGINE_POOL_DIR=str(env["pool"])), check=False,
	)
	assert listed.stdout.split() == ["BETA", "ALPHA"]


def test_gated_and_rejected_accounts_are_left_out(env: dict) -> None:
	table = {"tok-alpha": {"five": 0.95, "seven": 0.1}, "tok-beta": {"auth": True}}
	_Stub.broker_body["accounts"].append({"name": "GAMMA", "token": "tok-gamma"})
	table["tok-gamma"] = {"five": 0.1, "seven": 0.2}
	result, outputs = _run(env, FAKE_PROBE_TABLE=json.dumps(table))
	assert outputs["available"] == "true" and outputs["accounts"] == "1"
	assert (env["pool"] / "order").read_text(encoding="utf-8") == "GAMMA\n"
	assert sorted(p.name for p in (env["pool"] / "tokens").iterdir()) == ["GAMMA"]


def test_all_gated_is_unavailable_and_removes_the_pool(env: dict) -> None:
	table = {"tok-alpha": {"five": 0.95, "seven": 0.1}, "tok-beta": {"five": 0.2, "seven": 0.99}}
	result, outputs = _run(env, FAKE_PROBE_TABLE=json.dumps(table))
	assert (outputs["available"], outputs["reason"]) == ("false", "all_gated")
	assert not env["pool"].exists()


def test_probe_false_keeps_the_broker_order(env: dict) -> None:
	result, outputs = _run(env, CLAUDE_POOL_PROBE="false")
	assert (outputs["available"], outputs["reason"]) == ("true", "probe_skipped")
	assert (env["pool"] / "order").read_text(encoding="utf-8") == "ALPHA\nBETA\n"


@pytest.mark.parametrize(
	"status, body, reason",
	[
		(403, {"error": "repo_not_allowed"}, "broker_refused_repo_not_allowed"),
		(403, {"error": "Weird Reason!"}, "broker_refused_unknown"),
		(503, {"error": "pool_empty"}, "broker_unavailable_503"),
		(200, {"accounts": "nope"}, "broker_response_invalid"),
		(200, {"accounts": [{"name": "bad name", "token": "x"}]}, "no_accounts"),
	],
)
def test_broker_failures(env: dict, status: int, body: dict, reason: str) -> None:
	_Stub.broker_status = status
	_Stub.broker_body = body
	result, outputs = _run(env)
	assert result.returncode == 0
	assert (outputs["available"], outputs["reason"]) == ("false", reason)
	assert not env["pool"].exists()


def test_unconfigured_broker(env: dict) -> None:
	env["config"].write_text(json.dumps({"broker_url": ""}), encoding="utf-8")
	_, outputs = _run(env)
	assert (outputs["available"], outputs["reason"]) == ("false", "broker_not_configured")
	assert _Stub.seen == []


def test_unconfigured_broker_in_actions(env: dict) -> None:
	env["config"].write_text(json.dumps({"broker_url": ""}), encoding="utf-8")
	_, outputs = _run(env, GITHUB_ACTIONS="true")
	assert (outputs["available"], outputs["reason"]) == ("false", "broker_not_configured")
	assert _Stub.seen == []


def test_no_oidc_permission(env: dict) -> None:
	_, outputs = _run(env, ACTIONS_ID_TOKEN_REQUEST_URL=None, ACTIONS_ID_TOKEN_REQUEST_TOKEN=None)
	assert (outputs["available"], outputs["reason"]) == ("false", "oidc_unavailable")


def test_untrusted_broker_url_is_refused_before_requesting_oidc(env: dict) -> None:
	env["config"].write_text(json.dumps({"broker_url": "https://attacker.example/v1/pool"}), encoding="utf-8")
	_, outputs = _run(env)
	assert (outputs["available"], outputs["reason"], outputs["accounts"]) == ("false", "broker_not_configured", "0")
	assert _Stub.seen == []


def test_actions_job_cannot_use_loopback_broker(env: dict) -> None:
	_, outputs = _run(env, GITHUB_ACTIONS="true")
	assert (outputs["available"], outputs["reason"]) == ("false", "broker_url_invalid")
	assert _Stub.seen == []


def test_oidc_request_url_without_query_string(env: dict) -> None:
	url = env["env"]["ACTIONS_ID_TOKEN_REQUEST_URL"].split("?", 1)[0]
	_, outputs = _run(env, ACTIONS_ID_TOKEN_REQUEST_URL=url)
	assert outputs["available"] == "true"
	assert "?audience=coding-workflows-claude-pool" in _Stub.seen[0]["path"]


def test_oidc_request_rejected(env: dict) -> None:
	_, outputs = _run(env, ACTIONS_ID_TOKEN_REQUEST_TOKEN="wrong")
	assert (outputs["available"], outputs["reason"]) == ("false", "oidc_request_failed_401")


def test_cli_missing(env: dict) -> None:
	path = ":".join(part for part in env["env"]["PATH"].split(":") if not (Path(part) / "claude").exists())
	_, outputs = _run(env, PATH=path)
	assert (outputs["available"], outputs["reason"]) == ("false", "cli_missing")
	assert not env["pool"].exists()


@pytest.mark.parametrize("bad", ["/", "/tmp", "relative/claude-pool", "/tmp/claude-pool/../x", "/home/user"])
def test_pool_dir_must_be_a_dedicated_directory(env: dict, bad: str) -> None:
	result, outputs = _run(env, CLAUDE_ENGINE_POOL_DIR=bad)
	assert (outputs["available"], outputs["reason"]) == ("false", "pool_dir_invalid")
	assert outputs["accounts"] == "0" and outputs["pool_dir"] == bad
	assert _Stub.seen == []


def test_pool_dir_cannot_escape_runner_temp_or_follow_symlink(env: dict) -> None:
	outside = env["tmp"] / "elsewhere" / "claude-pool"
	outside.mkdir(parents=True)
	(outside / "keep").write_text("kept", encoding="utf-8")
	_, outputs = _run(env, CLAUDE_ENGINE_POOL_DIR=str(outside))
	assert outputs["reason"] == "pool_dir_invalid"
	assert (outside / "keep").exists()
	env["pool"].symlink_to(outside, target_is_directory=True)
	_, outputs = _run(env)
	assert outputs["reason"] == "pool_dir_invalid"
	assert env["pool"].is_symlink() and (outside / "keep").exists()
	post = subprocess.run(["node", str(ACTION_DIR / "post.js")], capture_output=True, text=True, env=env["env"], check=False)
	assert "cleanup skipped reason=pool_dir_invalid" in post.stdout
	assert env["pool"].is_symlink() and (outside / "keep").exists()


def test_action_main_and_post(env: dict) -> None:
	values = dict(env["env"], INPUT_CONFIG_PATH=str(env["config"]), INPUT_PROBE="true")
	main = subprocess.run(["node", str(ACTION_DIR / "main.js")], capture_output=True, text=True, env=values, timeout=120, check=False)
	assert main.returncode == 0, main.stderr
	assert "CLAUDE_POOL available=true reason=selected accounts=2" in main.stdout
	assert (env["pool"] / "order").exists()
	post = subprocess.run(["node", str(ACTION_DIR / "post.js")], capture_output=True, text=True, env=values, timeout=60, check=False)
	assert post.returncode == 0
	assert "CLAUDE_POOL cleanup removed=true" in post.stdout
	assert not env["pool"].exists()


def test_action_post_cleans_pool_with_trailing_runner_temp_slash(env: dict) -> None:
	env["pool"].mkdir()
	(env["pool"] / "order").write_text("A\n", encoding="utf-8")
	post = subprocess.run(
		["node", str(ACTION_DIR / "post.js")],
		capture_output=True, text=True,
		env=dict(env["env"], RUNNER_TEMP=str(env["pool"].parent) + "/"), check=False,
	)
	assert post.returncode == 0
	assert "CLAUDE_POOL cleanup removed=true" in post.stdout
	assert not env["pool"].exists()


def test_action_post_refuses_a_dangerous_directory(env: dict, tmp_path: Path) -> None:
	keep = tmp_path / "keep"
	keep.mkdir()
	post = subprocess.run(
		["node", str(ACTION_DIR / "post.js")],
		capture_output=True, text=True, env=dict(env["env"], CLAUDE_ENGINE_POOL_DIR=str(keep)), timeout=60, check=False,
	)
	assert "cleanup skipped reason=pool_dir_invalid" in post.stdout
	assert keep.exists()


def test_action_metadata() -> None:
	text = (ACTION_DIR / "action.yml").read_text(encoding="utf-8")
	assert "using: node24" in text
	assert "post: post.js" in text and "post-if: always()" in text


def test_no_workflow_or_template_references_pool_secrets() -> None:
	pattern = re.compile(r"CLAUDE_POOL_TOKEN")
	offenders = []
	for base in (REPO_ROOT / ".github", REPO_ROOT / "workflow-templates"):
		for path in base.rglob("*"):
			if path.is_file() and path.suffix in (".yml", ".yaml", ".js", ".sh", ".py", ".json"):
				if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
					offenders.append(str(path.relative_to(REPO_ROOT)))
	assert offenders == []
