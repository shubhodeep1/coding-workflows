#!/usr/bin/env python3
"""scripts/chatgpt_pool_token.sh, .github/actions/chatgpt-pool-token and .github/ai/chatgpt_pool.json.

ChatGPT account reviewer trial, Phase 1. A stub HTTP server plays the Actions
OIDC endpoint and the claude-pool-broker /v1/chatgpt endpoint on loopback.
Checks the 0600 auth.json layout, masking, that a refresh token never reaches
disk, every available=false reason, the post-step cleanup, the config file
(reviewer slot off), and that the broker's CHATGPT_ALLOWED_WORKFLOW_FILES
equals the workflows that use the action (none yet).
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "chatgpt_pool_token.sh"
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "chatgpt-pool-token"
CONFIG = REPO_ROOT / ".github" / "ai" / "chatgpt_pool.json"
BROKER_SOURCE = REPO_ROOT / "tools" / "claude-pool-broker" / "src" / "index.ts"
REQUEST_TOKEN = "runner-request-token-123"
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJyZXBvc2l0b3J5IjoieCJ9.c2lnbmF0dXJlc2lnbmF0dXJl"
ACCESS = "eyJhbGciOiJSUzI1NiJ9.eyJhY2Nlc3MiOnRydWV9.YWNjZXNzc2lnbmF0dXJl"
ID_TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJpZCI6dHJ1ZX0.aWRzaWduYXR1cmVzaWc"
REFRESH = "rt_never_on_disk_refresh_token"
EXPIRES = 1893456000


class _Stub(http.server.BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.0"
	broker_status = 200
	broker_body: object = {}
	seen: list[dict] = []
	oidc_statuses: list[int] = []

	def log_message(self, *_args):
		pass

	def _send(self, status: int, body: object) -> None:
		payload = json.dumps(body).encode()
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(payload)))
		self.end_headers()
		self.wfile.write(payload)

	def do_GET(self):
		_Stub.seen.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
		if self.path.startswith("/oidc?") and self.headers.get("Authorization") == f"bearer {REQUEST_TOKEN}":
			if _Stub.oidc_statuses:
				status = _Stub.oidc_statuses.pop(0)
				self._send(status, {"value": JWT} if status == 200 else {})
			else:
				self._send(200, {"value": JWT})
		else:
			self._send(401, {})

	def do_POST(self):
		_Stub.seen.append({"method": "POST", "path": self.path, "auth": self.headers.get("Authorization")})
		if self.path == "/v1/chatgpt" and self.headers.get("Authorization") == f"Bearer {JWT}":
			self._send(_Stub.broker_status, _Stub.broker_body)
		else:
			self._send(403, {"error": "missing_token"})


# A loopback-only stand-in for the curl subset scripts/chatgpt_pool_token.sh
# uses (-sS, -o FILE, -w '%{http_code}', --max-time N, -H VALUE / -H @FILE,
# -X METHOD, URL). It is put on PATH only when the host has no curl (the
# credential-free pre-push container), so CI still runs the script against
# the real curl. Like curl, it prints 000 and exits non-zero on no response.
FAKE_CURL = r'''
import sys, urllib.error, urllib.request
args = sys.argv[1:]
out_path = write_out = method = url = None
timeout = 30.0
headers = {}
i = 0
while i < len(args):
	arg = args[i]
	if arg in ("-o", "-w", "--max-time", "-H", "-X"):
		value = args[i + 1]
		i += 2
		if arg == "-o":
			out_path = value
		elif arg == "-w":
			write_out = value
		elif arg == "--max-time":
			timeout = float(value)
		elif arg == "-X":
			method = value
		else:
			lines = open(value[1:], encoding="utf-8").read().splitlines() if value.startswith("@") else [value]
			for line in lines:
				name, _, content = line.partition(":")
				if name.strip():
					headers[name.strip()] = content.strip()
		continue
	if not arg.startswith("-"):
		url = arg
	i += 1
data = b"" if method == "POST" else None
headers.pop("Content-Length", None)
request = urllib.request.Request(url, data=data, headers=headers, method=method or "GET")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
	with opener.open(request, timeout=timeout) as response:
		status, body = response.status, response.read()
except urllib.error.HTTPError as error:
	status, body = error.code, error.read()
except Exception:
	if write_out:
		sys.stdout.write("000")
	sys.exit(7)
if out_path:
	with open(out_path, "wb") as handle:
		handle.write(body)
if write_out:
	sys.stdout.write(write_out.replace("%{http_code}", "%03d" % status))
'''


def _curl_path_prefix(tmp_path: Path) -> str | None:
	"""A PATH entry holding the curl stand-in, or None when the host has curl."""
	if shutil.which("curl"):
		return None
	fake_bin = tmp_path / "fake-curl-bin"
	fake_bin.mkdir()
	fake = fake_bin / "curl"
	fake.write_text(f"#!{sys.executable}\n{FAKE_CURL}", encoding="utf-8")
	fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
	return str(fake_bin)


@pytest.fixture()
def env(tmp_path: Path):
	server = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	port = server.server_address[1]
	_Stub.seen = []
	_Stub.oidc_statuses = []
	_Stub.broker_status = 200
	_Stub.broker_body = {"access_token": ACCESS, "id_token": ID_TOKEN, "account_id": "acct-1234", "expires_at": EXPIRES, "refresh_token": REFRESH}
	config = tmp_path / "chatgpt_pool.json"
	config.write_text(json.dumps({
		"schema_version": 1,
		"broker_url": f"http://127.0.0.1:{port}/v1/chatgpt",
		"audience": "coding-workflows-chatgpt-pool",
		"reviewer_slot_enabled": False,
	}), encoding="utf-8")
	runner_temp = tmp_path / "rt"
	runner_temp.mkdir()
	values = {
		key: value
		for key, value in os.environ.items()
		if not key.startswith(("CHATGPT_", "ACTIONS_ID_TOKEN", "GITHUB_OUTPUT")) and key != "GITHUB_ACTIONS" and key.lower() not in ("http_proxy", "https_proxy", "all_proxy")
	}
	values.update(
		{
			# Most cases exercise the loopback broker outside Actions; the Actions-only cases override this.
			"GITHUB_ACTIONS": "false",
			"RUNNER_TEMP": str(runner_temp),
			"GITHUB_OUTPUT": str(tmp_path / "output.txt"),
			"CHATGPT_POOL_CONFIG": str(config),
			"ACTIONS_ID_TOKEN_REQUEST_URL": f"http://127.0.0.1:{port}/oidc?api-version=2.0",
			"ACTIONS_ID_TOKEN_REQUEST_TOKEN": REQUEST_TOKEN,
			"NO_PROXY": "127.0.0.1,localhost",
			"no_proxy": "127.0.0.1,localhost",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	curl_prefix = _curl_path_prefix(tmp_path)
	if curl_prefix:
		values["PATH"] = f"{curl_prefix}:{values.get('PATH', os.defpath)}"
	yield {"env": values, "tmp": tmp_path, "pool": runner_temp / "chatgpt-pool", "config": config, "port": port}
	server.shutdown()
	server.server_close()


def _run(env: dict, **extra: str | None) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
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


def _gets() -> list[dict]:
	return [entry for entry in _Stub.seen if entry["method"] == "GET"]


def test_granted_writes_a_0600_auth_json(env: dict) -> None:
	result, outputs = _run(env)
	assert result.returncode == 0, result.stderr
	pool = env["pool"]
	assert outputs == {
		"available": "true",
		"reason": "granted",
		"auth_dir": str(pool),
		"auth_file": str(pool / "auth.json"),
		"expires_at": str(EXPIRES),
	}
	auth = json.loads((pool / "auth.json").read_text(encoding="utf-8"))
	assert set(auth) == {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}
	assert auth["auth_mode"] == "chatgpt" and auth["OPENAI_API_KEY"] is None
	assert auth["tokens"] == {"id_token": ID_TOKEN, "access_token": ACCESS, "account_id": "acct-1234"}
	assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", auth["last_refresh"])
	assert (pool / "expires_at").read_text(encoding="utf-8") == f"{EXPIRES}\n"
	assert stat.S_IMODE(pool.stat().st_mode) == 0o700
	for path in (pool / "auth.json", pool / "expires_at"):
		assert stat.S_IMODE(path.stat().st_mode) == 0o600
	assert sorted(p.name for p in pool.iterdir()) == ["auth.json", "expires_at"]
	# Masks come first, before anything else is printed about the credential.
	lines = result.stdout.splitlines()
	assert lines[:3] == [f"::add-mask::{JWT}", f"::add-mask::{ACCESS}", f"::add-mask::{ID_TOKEN}"]
	rest = _without_masks(result.stdout + result.stderr)
	for secret in (JWT, ACCESS, ID_TOKEN, REFRESH, REQUEST_TOKEN):
		assert secret not in rest
	output_text = Path(env["env"]["GITHUB_OUTPUT"]).read_text(encoding="utf-8")
	for secret in (JWT, ACCESS, ID_TOKEN, REFRESH):
		assert secret not in output_text
	assert "CHATGPT_POOL available=true reason=granted" in result.stdout
	assert [entry["method"] for entry in _Stub.seen] == ["GET", "POST"]
	assert "audience=coding-workflows-chatgpt-pool" in _Stub.seen[0]["path"]


def test_refresh_token_never_reaches_disk(env: dict) -> None:
	_run(env)
	for path in env["pool"].rglob("*"):
		if path.is_file():
			assert REFRESH not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
	"status, body, reason",
	[
		(403, {"error": "workflow_not_allowed"}, "broker_refused_workflow_not_allowed"),
		(403, {"error": "bad_audience"}, "broker_refused_bad_audience"),
		(403, {"error": "Weird Reason!"}, "broker_refused_unknown"),
		(503, {"error": "credential_not_seeded"}, "broker_unavailable_503"),
		(500, {"error": "internal"}, "broker_unavailable_500"),
		(200, {"access_token": "short", "id_token": ID_TOKEN, "account_id": "a", "expires_at": EXPIRES}, "broker_response_invalid"),
		(200, {"access_token": ACCESS, "id_token": ID_TOKEN, "account_id": "bad id", "expires_at": EXPIRES}, "broker_response_invalid"),
		(200, {"access_token": ACCESS, "id_token": ID_TOKEN, "account_id": "a", "expires_at": "soon"}, "broker_response_invalid"),
		(200, {"access_token": ACCESS, "id_token": ID_TOKEN, "account_id": "a", "expires_at": True}, "broker_response_invalid"),
		(200, ["not", "an", "object"], "broker_response_invalid"),
	],
)
def test_broker_failures(env: dict, status: int, body: object, reason: str) -> None:
	_Stub.broker_status = status
	_Stub.broker_body = body
	result, outputs = _run(env)
	assert result.returncode == 0
	assert outputs == {"available": "false", "reason": reason, "auth_dir": "", "auth_file": "", "expires_at": ""}
	assert not env["pool"].exists()


def test_config_missing(env: dict) -> None:
	_, outputs = _run(env, CHATGPT_POOL_CONFIG=str(env["tmp"] / "absent.json"))
	assert (outputs["available"], outputs["reason"]) == ("false", "config_missing")
	assert _Stub.seen == []


@pytest.mark.parametrize("text", ["{", "[]", json.dumps({"broker_url": 5}), json.dumps({"broker_url": "x\ny"})])
def test_config_invalid(env: dict, text: str) -> None:
	env["config"].write_text(text, encoding="utf-8")
	_, outputs = _run(env)
	assert (outputs["available"], outputs["reason"]) == ("false", "config_invalid")
	assert _Stub.seen == []


@pytest.mark.parametrize("actions", ["false", "true"])
def test_unconfigured_broker(env: dict, actions: str) -> None:
	env["config"].write_text(json.dumps({"broker_url": "", "audience": "coding-workflows-chatgpt-pool"}), encoding="utf-8")
	_, outputs = _run(env, GITHUB_ACTIONS=actions)
	assert (outputs["available"], outputs["reason"]) == ("false", "broker_not_configured")
	assert _Stub.seen == []


@pytest.mark.parametrize("url", [
	"https://attacker.example/v1/chatgpt",
	"https://claude-pool-broker.shubhodeep.workers.dev/v1/pool",
	"https://claude-pool-broker.shubhodeep.workers.dev/v1/chatgpt?x=1",
	"http://claude-pool-broker.shubhodeep.workers.dev/v1/chatgpt",
])
def test_untrusted_broker_url_is_refused_before_requesting_oidc(env: dict, url: str) -> None:
	env["config"].write_text(json.dumps({"broker_url": url, "audience": "coding-workflows-chatgpt-pool"}), encoding="utf-8")
	_, outputs = _run(env)
	assert (outputs["available"], outputs["reason"]) == ("false", "broker_url_invalid")
	assert _Stub.seen == []


def test_actions_job_cannot_use_loopback_broker(env: dict) -> None:
	_, outputs = _run(env, GITHUB_ACTIONS="true")
	assert (outputs["available"], outputs["reason"]) == ("false", "broker_url_invalid")
	assert _Stub.seen == []


@pytest.mark.parametrize("audience, actions", [("Bad Audience", "false"), ("coding-workflows-claude-pool", "true")])
def test_audience_is_checked(env: dict, audience: str, actions: str) -> None:
	data = json.loads(env["config"].read_text(encoding="utf-8"))
	data["audience"] = audience
	if actions == "true":
		data["broker_url"] = "https://claude-pool-broker.shubhodeep.workers.dev/v1/chatgpt"
	env["config"].write_text(json.dumps(data), encoding="utf-8")
	_, outputs = _run(env, GITHUB_ACTIONS=actions)
	assert (outputs["available"], outputs["reason"]) == ("false", "config_invalid")
	assert _Stub.seen == []


def test_no_oidc_permission(env: dict) -> None:
	_, outputs = _run(env, ACTIONS_ID_TOKEN_REQUEST_URL=None, ACTIONS_ID_TOKEN_REQUEST_TOKEN=None)
	assert (outputs["available"], outputs["reason"]) == ("false", "oidc_unavailable")


def test_oidc_request_rejected_is_not_retried(env: dict) -> None:
	result, outputs = _run(env, ACTIONS_ID_TOKEN_REQUEST_TOKEN="wrong", CHATGPT_POOL_OIDC_RETRY_SLEEPS="0 0")
	assert (outputs["available"], outputs["reason"]) == ("false", "oidc_request_failed_401")
	assert len(_gets()) == 1
	assert "oidc_retry" not in result.stdout


def test_oidc_retry_recovers_after_503(env: dict) -> None:
	_Stub.oidc_statuses = [503, 200]
	result, outputs = _run(env, CHATGPT_POOL_OIDC_RETRY_SLEEPS="0 0")
	assert (outputs["available"], outputs["reason"]) == ("true", "granted")
	assert len(_gets()) == 2
	assert "CHATGPT_POOL oidc_retry attempt=2 code=503" in result.stdout
	rest = _without_masks(result.stdout + result.stderr)
	for secret in (REQUEST_TOKEN, JWT, "audience="):
		assert secret not in rest


def test_oidc_retry_gives_up_after_three_503(env: dict) -> None:
	_Stub.oidc_statuses = [503, 503, 503, 200]
	result, outputs = _run(env, CHATGPT_POOL_OIDC_RETRY_SLEEPS="0 0")
	assert (outputs["available"], outputs["reason"]) == ("false", "oidc_request_failed_503")
	assert len(_gets()) == 3
	assert not [entry for entry in _Stub.seen if entry["method"] == "POST"]
	assert "attempt=4" not in result.stdout
	assert not env["pool"].exists()


def test_oidc_retry_flag_invalid_is_off_with_warning(env: dict) -> None:
	_Stub.oidc_statuses = [503, 200]
	result, outputs = _run(env, CHATGPT_POOL_OIDC_RETRY_ENABLED="maybe", CHATGPT_POOL_OIDC_RETRY_SLEEPS="0 0")
	assert (outputs["available"], outputs["reason"]) == ("false", "oidc_request_failed_503")
	assert len(_gets()) == 1
	assert "::warning::" in result.stderr and "CHATGPT_POOL_OIDC_RETRY_ENABLED" in result.stderr
	assert "maybe" not in result.stdout + result.stderr


def test_oidc_retry_flag_false_makes_one_attempt(env: dict) -> None:
	_Stub.oidc_statuses = [503, 200]
	result, outputs = _run(env, CHATGPT_POOL_OIDC_RETRY_ENABLED="FALSE", CHATGPT_POOL_OIDC_RETRY_SLEEPS="0 0")
	assert (outputs["available"], outputs["reason"]) == ("false", "oidc_request_failed_503")
	assert len(_gets()) == 1
	assert "::warning::" not in result.stderr


@pytest.mark.parametrize("sleeps", ["0", "0 0 0", "x y", "61 0", "-1 0"])
def test_oidc_retry_invalid_sleeps_fall_back(env: dict, sleeps: str) -> None:
	result, outputs = _run(env, CHATGPT_POOL_OIDC_RETRY_SLEEPS=sleeps)
	assert outputs["available"] == "true"
	assert [line for line in result.stdout.splitlines() if "oidc_retry_sleeps_invalid" in line] == ["CHATGPT_POOL oidc_retry_sleeps_invalid fallback=2,5"]


@pytest.mark.parametrize("bad", ["/", "/tmp", "relative/chatgpt-pool", "/tmp/chatgpt-pool/../x", "/home/user"])
def test_pool_dir_must_be_a_dedicated_directory(env: dict, bad: str) -> None:
	_, outputs = _run(env, CHATGPT_POOL_DIR=bad)
	assert outputs == {"available": "false", "reason": "pool_dir_invalid", "auth_dir": "", "auth_file": "", "expires_at": ""}
	assert _Stub.seen == []


def test_pool_dir_cannot_escape_runner_temp_or_follow_symlink(env: dict) -> None:
	outside = env["tmp"] / "elsewhere" / "chatgpt-pool"
	outside.mkdir(parents=True)
	(outside / "keep").write_text("kept", encoding="utf-8")
	_, outputs = _run(env, CHATGPT_POOL_DIR=str(outside))
	assert outputs["reason"] == "pool_dir_invalid"
	assert (outside / "keep").exists()
	env["pool"].symlink_to(outside, target_is_directory=True)
	_, outputs = _run(env)
	assert outputs["reason"] == "pool_dir_invalid"
	assert env["pool"].is_symlink() and (outside / "keep").exists()
	post = subprocess.run(["node", str(ACTION_DIR / "post.js")], capture_output=True, text=True, env=env["env"], check=False)
	assert "CHATGPT_POOL cleanup skipped reason=pool_dir_invalid" in post.stdout
	assert env["pool"].is_symlink() and (outside / "keep").exists()


def test_action_main_and_post(env: dict) -> None:
	values = dict(env["env"], INPUT_CONFIG_PATH=str(env["config"]))
	values.pop("CHATGPT_POOL_CONFIG")
	main = subprocess.run(["node", str(ACTION_DIR / "main.js")], capture_output=True, text=True, env=values, timeout=120, check=False)
	assert main.returncode == 0, main.stderr
	assert "CHATGPT_POOL available=true reason=granted" in main.stdout
	assert (env["pool"] / "auth.json").exists()
	post = subprocess.run(["node", str(ACTION_DIR / "post.js")], capture_output=True, text=True, env=values, timeout=60, check=False)
	assert post.returncode == 0
	assert "CHATGPT_POOL cleanup removed=true" in post.stdout
	assert not env["pool"].exists()


def test_action_post_refuses_a_dangerous_directory(env: dict, tmp_path: Path) -> None:
	keep = tmp_path / "keep"
	keep.mkdir()
	post = subprocess.run(
		["node", str(ACTION_DIR / "post.js")],
		capture_output=True, text=True, env=dict(env["env"], CHATGPT_POOL_DIR=str(keep)), timeout=60, check=False,
	)
	assert "CHATGPT_POOL cleanup skipped reason=pool_dir_invalid" in post.stdout
	assert keep.exists()


def test_action_runs_as_commonjs_under_a_module_type_consumer(tmp_path: Path) -> None:
	"""A consumer root package.json with "type": "module" must not turn main.js / post.js into ES modules."""
	consumer = tmp_path / "consumer"
	action = consumer / ".codex-workflow-src" / ".github" / "actions" / "chatgpt-pool-token"
	action.mkdir(parents=True)
	(consumer / "package.json").write_text(json.dumps({"type": "module"}), encoding="utf-8")
	for name in ("main.js", "post.js", "package.json"):
		shutil.copy2(ACTION_DIR / name, action / name)
	runner_temp = tmp_path / "runner-temp"
	runner_temp.mkdir()
	out = tmp_path / "out"
	values = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "RUNNER_TEMP": str(runner_temp), "GITHUB_OUTPUT": str(out)}
	# scripts/chatgpt_pool_token.sh is absent here, so main.js's own fallback must run.
	main = subprocess.run(["node", str(action / "main.js")], capture_output=True, text=True, env=values, timeout=60, check=False)
	assert main.returncode == 0, main.stderr
	assert "require is not defined" not in main.stderr
	assert "CHATGPT_POOL available=false reason=script_failed" in main.stdout
	assert out.read_text(encoding="utf-8") == "available=false\nreason=script_failed\nauth_dir=\nauth_file=\nexpires_at=\n"
	pool = runner_temp / "chatgpt-pool"
	pool.mkdir()
	(pool / "auth.json").write_text("{}", encoding="utf-8")
	post = subprocess.run(["node", str(action / "post.js")], capture_output=True, text=True, env=values, timeout=60, check=False)
	assert post.returncode == 0, post.stderr
	assert "CHATGPT_POOL cleanup removed=true" in post.stdout
	assert not pool.exists()


def test_action_metadata() -> None:
	text = (ACTION_DIR / "action.yml").read_text(encoding="utf-8")
	assert "using: node24" in text
	assert "main: main.js" in text
	assert "post: post.js" in text and "post-if: always()" in text
	for output in ("available:", "reason:", "auth_dir:", "auth_file:", "expires_at:"):
		assert f"\n  {output}" in text
	assert "id-token: write" in text
	assert json.loads((ACTION_DIR / "package.json").read_text(encoding="utf-8")) == {"type": "commonjs"}


def test_script_defaults_every_input() -> None:
	text = SCRIPT.read_text(encoding="utf-8")
	for name in ("CHATGPT_POOL_CONFIG", "CHATGPT_POOL_DIR", "CHATGPT_POOL_OIDC_RETRY_ENABLED", "CHATGPT_POOL_OIDC_RETRY_SLEEPS"):
		assert re.search(r"\$\{" + name + r":-", text), name


def test_config_contract() -> None:
	data = json.loads(CONFIG.read_text(encoding="utf-8"))
	assert set(data) == {"schema_version", "broker_url", "audience", "reviewer_slot_enabled"}
	assert data["schema_version"] == 1
	# Go-live is a separate human pull request.
	assert data["reviewer_slot_enabled"] is False
	assert data["broker_url"] == "https://claude-pool-broker.shubhodeep.workers.dev/v1/chatgpt"
	match = re.search(r'\bCHATGPT_AUDIENCE\s*=\s*"([^"]+)"', BROKER_SOURCE.read_text(encoding="utf-8"))
	assert match, "CHATGPT_AUDIENCE not found in the broker source"
	assert data["audience"] == match.group(1)
	claude = json.loads((REPO_ROOT / ".github" / "ai" / "claude_engine.json").read_text(encoding="utf-8"))
	assert data["audience"] != claude.get("oidc_audience")


def test_broker_chatgpt_allowlist_matches_action_users() -> None:
	"""The broker's /v1/chatgpt workflow allowlist equals the workflows that use the action."""
	source = BROKER_SOURCE.read_text(encoding="utf-8")
	match = re.search(r"\bCHATGPT_ALLOWED_WORKFLOW_FILES\b[^=]*=\s*Object\.freeze\(\[(.*?)\]\)", source, re.S)
	assert match, "CHATGPT_ALLOWED_WORKFLOW_FILES not found in the broker source"
	allowed = set(re.findall(r'"([^"]+)"', match.group(1)))
	users = {
		path.name
		for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml")
		if ".github/actions/chatgpt-pool-token" in path.read_text(encoding="utf-8", errors="replace")
	}
	assert allowed == users
