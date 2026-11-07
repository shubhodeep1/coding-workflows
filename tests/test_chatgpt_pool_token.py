#!/usr/bin/env python3
"""scripts/chatgpt_pool_token.sh and .github/actions/chatgpt-pool-token (ChatGPT trial plan, Phase 1).

A stub HTTP server plays the Actions OIDC endpoint and the claude-pool-broker
Worker's /v1/chatgpt and /v1/chatgpt/status routes on loopback. Checks the
token-mode auth.json layout (0600 file, 0700 directories, refresh token
dropped), the status-mode whitelist, every available=false reason, masking,
the post-step cleanup, and that every workflow using the action is in the
broker's ALLOWED_WORKFLOW_FILES.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "chatgpt_pool_token.sh"
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "chatgpt-pool-token"
REQUEST_TOKEN = "runner-request-token-456"
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJyZXBvc2l0b3J5IjoieSJ9.c2lnbmF0dXJlc2lnbmF0dXJl"
SECRETS = ("id-secret-value", "at-secret-value", "rt-secret-value", "sk-openai-secret")


def _auth_json(with_refresh: bool = True) -> dict:
	tokens = {"id_token": "id-secret-value", "access_token": "at-secret-value", "account_id": "acct-1"}
	if with_refresh:
		tokens["refresh_token"] = "rt-secret-value"
	return {"OPENAI_API_KEY": "sk-openai-secret", "tokens": tokens, "last_refresh": "2026-11-07T00:00:00Z"}


STATUS_BODY = {
	"accounts": [
		{
			"account": "ALPHA",
			"generation": 4,
			"last_refresh": "2026-11-07T10:00:00.000Z",
			"last_outcome": "ok",
			"usage": {"five_hour": 0.1, "weekly": 0.25, "resets": {"five_hour": 900, "weekly": "2026-11-10T00:00:00Z"}},
			"usable": True,
			"reason": None,
			"auth_json": _auth_json(),
			"seed_sha256": "a" * 64,
		},
		{
			"account": "BETA",
			"generation": 2,
			"last_refresh": "2026-11-07T10:00:00Z\n::set-output name=x::y",
			"last_outcome": "auth_failed",
			"usage": {"five_hour": "lots", "weekly": None, "resets": {}},
			"usable": "yes",
			"reason": "auth_rejected",
		},
		{"account": "bad name", "generation": 1},
	],
	"gate": 0.9,
}


# A stand-in for curl when the host has none (credential-free test containers).
# It covers only the flags scripts/chatgpt_pool_token.sh passes (-sS, -o, -w
# '%{http_code}', --max-time, -X, -H / -H @file) and talks to loopback only; a
# host with curl runs the real binary.
_CURL_SHIM = r'''#!/usr/bin/env python3
import sys, urllib.error, urllib.request
args = sys.argv[1:]
out = None
method = None
timeout = 30.0
headers = {}
url = None
i = 0
while i < len(args):
	arg = args[i]
	if arg in ("-o", "-w", "--max-time", "-X", "-H"):
		value = args[i + 1]
		i += 2
		if arg == "-o":
			out = value
		elif arg == "--max-time":
			timeout = float(value)
		elif arg == "-X":
			method = value
		elif arg == "-H":
			lines = open(value[1:], encoding="utf-8").read().splitlines() if value.startswith("@") else [value]
			for line in lines:
				name, _, val = line.partition(":")
				if name.strip():
					headers[name.strip()] = val.strip()
		continue
	if not arg.startswith("-"):
		url = arg
	i += 1
if not url or not url.startswith(("http://127.0.0.1", "http://localhost")):
	sys.stdout.write("000")
	sys.exit(7)
data = b"" if method == "POST" else None
request = urllib.request.Request(url, data=data, headers=headers, method=method or "GET")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
	response = opener.open(request, timeout=timeout)
	code, body = response.status, response.read()
except urllib.error.HTTPError as error:
	code, body = error.code, error.read()
except Exception:
	sys.stdout.write("000")
	sys.exit(7)
if out:
	with open(out, "wb") as handle:
		handle.write(body)
sys.stdout.write(str(code))
'''


def _path_with_curl(tmp_path: Path, path: str) -> str:
	if shutil.which("curl", path=path):
		return path
	shim_dir = tmp_path / "curl-shim"
	shim_dir.mkdir(exist_ok=True)
	shim = shim_dir / "curl"
	shim.write_text(_CURL_SHIM, encoding="utf-8")
	shim.chmod(0o755)
	return f"{shim_dir}{os.pathsep}{path}"


class _Stub(http.server.BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.0"
	broker_status = 200
	broker_body: object = {}
	oidc_body: object = {"value": JWT}
	seen: list[dict] = []

	def log_message(self, *_args):
		pass

	def _send(self, status: int, body: object) -> None:
		payload = body.encode() if isinstance(body, str) else json.dumps(body).encode()
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(payload)))
		self.end_headers()
		self.wfile.write(payload)

	def _broker(self, expected_path: str) -> None:
		if self.path == expected_path and self.headers.get("Authorization") == f"Bearer {JWT}":
			self._send(_Stub.broker_status, _Stub.broker_body)
		else:
			self._send(404, {"error": "not_found"})

	def do_GET(self):
		_Stub.seen.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
		if self.path.startswith("/oidc?"):
			if self.headers.get("Authorization") == f"bearer {REQUEST_TOKEN}":
				self._send(200, _Stub.oidc_body)
			else:
				self._send(401, {})
			return
		self._broker("/v1/chatgpt/status")

	def do_POST(self):
		_Stub.seen.append({"method": "POST", "path": self.path, "auth": self.headers.get("Authorization")})
		self._broker("/v1/chatgpt")


@pytest.fixture()
def env(tmp_path: Path):
	server = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	port = server.server_address[1]
	_Stub.seen = []
	_Stub.broker_status = 200
	_Stub.broker_body = {"account": "ALPHA", "auth_json": _auth_json()}
	_Stub.oidc_body = {"value": JWT}
	config = tmp_path / "claude_engine.json"
	config.write_text(json.dumps({"broker_url": f"http://127.0.0.1:{port}/v1/pool", "oidc_audience": "coding-workflows-claude-pool"}), encoding="utf-8")
	runner_temp = tmp_path / "rt"
	runner_temp.mkdir()
	values = {
		key: value
		for key, value in os.environ.items()
		if not key.startswith(("CHATGPT_", "CLAUDE_", "ACTIONS_ID_TOKEN", "GITHUB_OUTPUT", "INPUT_")) and key != "GITHUB_ACTIONS" and key.lower() not in ("http_proxy", "https_proxy", "all_proxy")
	}
	values.update(
		{
			"GITHUB_ACTIONS": "false",
			"RUNNER_TEMP": str(runner_temp),
			"GITHUB_OUTPUT": str(tmp_path / "output.txt"),
			"CHATGPT_POOL_CONFIG": str(config),
			"ACTIONS_ID_TOKEN_REQUEST_URL": f"http://127.0.0.1:{port}/oidc?api-version=2.0",
			"ACTIONS_ID_TOKEN_REQUEST_TOKEN": REQUEST_TOKEN,
			"NO_PROXY": "127.0.0.1,localhost",
			"no_proxy": "127.0.0.1,localhost",
			"PYTHONDONTWRITEBYTECODE": "1",
			"PATH": _path_with_curl(tmp_path, os.environ.get("PATH", os.defpath)),
		}
	)
	yield {"env": values, "tmp": tmp_path, "rt": runner_temp, "pool": runner_temp / "chatgpt-pool", "config": config}
	server.shutdown()
	server.server_close()


def _read_outputs(path: Path) -> dict[str, str]:
	outputs: dict[str, str] = {}
	if path.exists():
		for line in path.read_text(encoding="utf-8").splitlines():
			key, _, value = line.partition("=")
			outputs[key] = value
	return outputs


def _run(env: dict, **extra: str | None) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
	values = dict(env["env"], **extra)
	for key in [k for k, v in values.items() if v is None]:
		values.pop(key)
	result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=values, timeout=120, check=False)
	return result, _read_outputs(Path(values["GITHUB_OUTPUT"]))


def _without_masks(text: str) -> str:
	return "\n".join(line for line in text.splitlines() if not line.startswith("::add-mask::"))


def _assert_no_secret_leak(result: subprocess.CompletedProcess, env: dict) -> None:
	rest = _without_masks(result.stdout + result.stderr)
	for secret in (JWT, REQUEST_TOKEN, *SECRETS):
		assert secret not in rest
	outputs = Path(env["env"]["GITHUB_OUTPUT"]).read_text(encoding="utf-8") if Path(env["env"]["GITHUB_OUTPUT"]).exists() else ""
	for secret in (JWT, REQUEST_TOKEN, *SECRETS):
		assert secret not in outputs


def _no_work_dirs(env: dict) -> None:
	assert not list(env["rt"].glob("chatgpt-pool-work.*"))


def test_token_mode_writes_auth_json(env: dict) -> None:
	result, outputs = _run(env)
	assert result.returncode == 0, result.stderr
	home = env["pool"] / "ALPHA"
	assert outputs == {"available": "true", "reason": "selected", "account": "ALPHA", "codex_home": str(home), "status": ""}
	auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
	expected = _auth_json(with_refresh=False)
	assert auth == expected
	assert "refresh_token" not in auth["tokens"]
	assert stat.S_IMODE((home / "auth.json").stat().st_mode) == 0o600
	for path in (env["pool"], home):
		assert stat.S_IMODE(path.stat().st_mode) == 0o700
	assert sorted(p.name for p in home.iterdir()) == ["auth.json"]
	# Every secret is masked before anything else prints it, and nothing else prints it.
	for secret in (JWT, *SECRETS):
		assert f"::add-mask::{secret}" in result.stdout
	_assert_no_secret_leak(result, env)
	assert "CHATGPT_POOL refresh_token_dropped=true" in result.stdout
	assert "CHATGPT_POOL available=true reason=selected mode=token account=ALPHA" in result.stdout
	assert [(e["method"], e["path"].split("?")[0]) for e in _Stub.seen] == [("GET", "/oidc"), ("POST", "/v1/chatgpt")]
	assert "audience=coding-workflows-claude-pool" in _Stub.seen[0]["path"]
	_no_work_dirs(env)


def test_token_mode_without_refresh_token_logs_no_drop(env: dict) -> None:
	_Stub.broker_body = {"account": "BETA", "auth_json": _auth_json(with_refresh=False)}
	result, outputs = _run(env)
	assert (outputs["available"], outputs["account"]) == ("true", "BETA")
	assert "refresh_token_dropped" not in result.stdout


def test_status_mode_outputs_whitelisted_metadata(env: dict) -> None:
	_Stub.broker_body = STATUS_BODY
	result, outputs = _run(env, CHATGPT_POOL_MODE="status")
	assert result.returncode == 0, result.stderr
	assert (outputs["available"], outputs["reason"], outputs["account"], outputs["codex_home"]) == ("true", "status", "", "")
	raw = outputs["status"]
	assert "\n" not in raw
	for forbidden in ("auth_json", "seed_sha256", "refresh_token", "::", *SECRETS):
		assert forbidden not in raw
	assert json.loads(raw) == {
		"accounts": [
			{"account": "ALPHA", "generation": 4, "last_refresh": "2026-11-07T10:00:00.000Z", "last_outcome": "ok",
			 "usage": {"five_hour": 0.1, "weekly": 0.25, "resets": {"five_hour": 900, "weekly": "2026-11-10T00:00:00Z"}},
			 "usable": True, "reason": None},
			{"account": "BETA", "generation": 2, "last_refresh": None, "last_outcome": "auth_failed",
			 "usage": {"five_hour": None, "weekly": None, "resets": {"five_hour": None, "weekly": None}},
			 "usable": False, "reason": "auth_rejected"},
		],
		"gate": 0.9,
	}
	assert not env["pool"].exists()
	assert sorted(p.name for p in env["rt"].iterdir()) == []
	assert [(e["method"], e["path"].split("?")[0]) for e in _Stub.seen] == [("GET", "/oidc"), ("GET", "/v1/chatgpt/status")]
	_assert_no_secret_leak(result, env)


def test_status_mode_keeps_an_existing_account_directory(env: dict) -> None:
	(env["pool"] / "ALPHA").mkdir(parents=True)
	_Stub.broker_status = 503
	_Stub.broker_body = {"error": "chatgpt_store_unavailable"}
	_, outputs = _run(env, CHATGPT_POOL_MODE="status")
	assert outputs["reason"] == "broker_unavailable_503"
	assert (env["pool"] / "ALPHA").exists()


@pytest.mark.parametrize(
	"mode, status, body, reason",
	[
		("token", 503, {"error": "chatgpt_pool_empty"}, "broker_refused_chatgpt_pool_empty"),
		("status", 503, {"error": "chatgpt_pool_empty"}, "broker_refused_chatgpt_pool_empty"),
		("token", 503, {"error": "chatgpt_store_unavailable"}, "broker_unavailable_503"),
		("token", 503, "not json", "broker_unavailable_503"),
		("token", 500, {"error": "internal"}, "broker_unavailable_500"),
		("token", 403, {"error": "workflow_not_allowed"}, "broker_refused_workflow_not_allowed"),
		("token", 403, {"error": "bad_audience"}, "broker_refused_bad_audience"),
		("token", 403, {"error": "Weird Reason!"}, "broker_refused_unknown"),
		("token", 200, {"account": "bad name", "auth_json": {"tokens": {}}}, "broker_response_invalid"),
		("token", 200, {"account": "ALPHA", "auth_json": {"tokens": "nope"}}, "broker_response_invalid"),
		("token", 200, "not json", "broker_response_invalid"),
		("status", 200, {"accounts": "nope"}, "broker_response_invalid"),
		("status", 200, ["x"], "broker_response_invalid"),
	],
)
def test_broker_failures(env: dict, mode: str, status: int, body: object, reason: str) -> None:
	_Stub.broker_status = status
	_Stub.broker_body = body
	result, outputs = _run(env, CHATGPT_POOL_MODE=mode)
	assert result.returncode == 0
	assert outputs == {"available": "false", "reason": reason, "account": "", "codex_home": "", "status": ""}
	assert not env["pool"].exists()
	_no_work_dirs(env)
	assert f"CHATGPT_POOL available=false reason={reason} mode={mode} account=none" in result.stdout


def test_bad_account_name_is_still_masked(env: dict) -> None:
	_Stub.broker_body = {"account": "bad name", "auth_json": _auth_json()}
	result, outputs = _run(env)
	assert outputs["reason"] == "broker_response_invalid"
	assert "::add-mask::at-secret-value" in result.stdout
	_assert_no_secret_leak(result, env)


def test_mode_invalid(env: dict) -> None:
	result, outputs = _run(env, CHATGPT_POOL_MODE="::set-output name=x::y")
	assert (outputs["available"], outputs["reason"]) == ("false", "mode_invalid")
	assert "set-output" not in result.stdout + result.stderr
	assert _Stub.seen == []


def test_config_missing(env: dict) -> None:
	_, outputs = _run(env, CHATGPT_POOL_CONFIG=str(env["tmp"] / "nope.json"))
	assert (outputs["available"], outputs["reason"]) == ("false", "config_missing")


def test_config_unreadable_json_is_the_unconfigured_default(env: dict) -> None:
	# claude_engine.py reads malformed JSON as the defaults (broker_url "").
	env["config"].write_text("{not json", encoding="utf-8")
	_, outputs = _run(env)
	assert (outputs["available"], outputs["reason"]) == ("false", "broker_not_configured")
	assert _Stub.seen == []


def test_config_invalid_when_the_config_helper_fails(env: dict, tmp_path: Path) -> None:
	# A copy of the script without scripts/claude_engine.py next to it.
	lonely = tmp_path / "lonely" / "scripts"
	lonely.mkdir(parents=True)
	shutil.copy2(SCRIPT, lonely / SCRIPT.name)
	values = dict(env["env"])
	result = subprocess.run(["bash", str(lonely / SCRIPT.name)], capture_output=True, text=True, env=values, timeout=60, check=False)
	assert result.returncode == 0
	outputs = _read_outputs(Path(values["GITHUB_OUTPUT"]))
	assert (outputs["available"], outputs["reason"]) == ("false", "config_invalid")
	assert _Stub.seen == []


def test_unconfigured_broker(env: dict) -> None:
	env["config"].write_text(json.dumps({"broker_url": ""}), encoding="utf-8")
	_, outputs = _run(env)
	assert (outputs["available"], outputs["reason"]) == ("false", "broker_not_configured")
	assert _Stub.seen == []


def test_untrusted_broker_url_is_never_contacted(env: dict) -> None:
	# claude_engine.py drops an untrusted URL to the empty default.
	env["config"].write_text(json.dumps({"broker_url": "https://attacker.example/v1/pool"}), encoding="utf-8")
	_, outputs = _run(env)
	assert (outputs["available"], outputs["reason"]) == ("false", "broker_not_configured")
	assert _Stub.seen == []


def test_actions_job_cannot_use_loopback_broker(env: dict) -> None:
	_, outputs = _run(env, GITHUB_ACTIONS="true")
	assert (outputs["available"], outputs["reason"]) == ("false", "broker_url_invalid")
	assert _Stub.seen == []


def test_no_oidc_permission(env: dict) -> None:
	_, outputs = _run(env, ACTIONS_ID_TOKEN_REQUEST_URL=None, ACTIONS_ID_TOKEN_REQUEST_TOKEN=None)
	assert (outputs["available"], outputs["reason"]) == ("false", "oidc_unavailable")
	assert _Stub.seen == []


def test_oidc_request_rejected(env: dict) -> None:
	result, outputs = _run(env, ACTIONS_ID_TOKEN_REQUEST_TOKEN="wrong")
	assert (outputs["available"], outputs["reason"]) == ("false", "oidc_request_failed_401")
	assert len(_Stub.seen) == 1
	assert not env["pool"].exists()
	_no_work_dirs(env)


@pytest.mark.parametrize("body", [{"value": "short"}, {"value": "has space in it 12345678901234"}, {}, "not json"])
def test_oidc_invalid(env: dict, body: object) -> None:
	_Stub.oidc_body = body
	_, outputs = _run(env)
	assert (outputs["available"], outputs["reason"]) == ("false", "oidc_invalid")
	assert [e["method"] for e in _Stub.seen] == ["GET"]
	_no_work_dirs(env)


def test_oidc_request_url_without_query_string(env: dict) -> None:
	url = env["env"]["ACTIONS_ID_TOKEN_REQUEST_URL"].split("?", 1)[0]
	_, outputs = _run(env, ACTIONS_ID_TOKEN_REQUEST_URL=url)
	assert outputs["available"] == "true"
	assert "?audience=coding-workflows-claude-pool" in _Stub.seen[0]["path"]


@pytest.mark.parametrize("bad", ["/", "/tmp", "relative/chatgpt-pool", "/tmp/chatgpt-pool/../x", "/home/user"])
def test_pool_dir_must_be_a_dedicated_directory(env: dict, bad: str) -> None:
	_, outputs = _run(env, CHATGPT_POOL_DIR=bad)
	assert (outputs["available"], outputs["reason"]) == ("false", "pool_dir_invalid")
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


def test_a_failed_token_run_removes_a_previous_directory(env: dict) -> None:
	_run(env)
	assert (env["pool"] / "ALPHA" / "auth.json").exists()
	_Stub.broker_status = 503
	_Stub.broker_body = {"error": "chatgpt_pool_empty"}
	_, outputs = _run(env)
	assert outputs["reason"] == "broker_refused_chatgpt_pool_empty"
	assert not env["pool"].exists()


def test_tokens_never_reach_argv() -> None:
	text = SCRIPT.read_text(encoding="utf-8")
	assert text.count('-H @"${work}/oidc-request-headers"') == 1
	assert text.count('-H @"${work}/broker-request-headers"') == 2
	assert not re.search(r"-H\s+[\"']Authorization", text)
	for literal in ("CLAUDE_POOL_TOKEN", "claude -p", "codex exec", ".github/ai/chatgpt_pool.json"):
		for path in (SCRIPT, *ACTION_DIR.iterdir()):
			assert literal not in path.read_text(encoding="utf-8"), (literal, path)


def test_action_main_and_post(env: dict) -> None:
	values = dict(env["env"], INPUT_CONFIG_PATH=str(env["config"]), INPUT_MODE="token")
	values.pop("CHATGPT_POOL_CONFIG")
	main = subprocess.run(["node", str(ACTION_DIR / "main.js")], capture_output=True, text=True, env=values, timeout=120, check=False)
	assert main.returncode == 0, main.stderr
	assert "CHATGPT_POOL available=true reason=selected mode=token account=ALPHA" in main.stdout
	assert (env["pool"] / "ALPHA" / "auth.json").exists()
	post = subprocess.run(["node", str(ACTION_DIR / "post.js")], capture_output=True, text=True, env=values, timeout=60, check=False)
	assert post.returncode == 0
	assert "CHATGPT_POOL cleanup removed=true" in post.stdout
	assert not env["pool"].exists()


def test_action_status_mode(env: dict) -> None:
	_Stub.broker_body = STATUS_BODY
	values = dict(env["env"], INPUT_MODE="status")
	main = subprocess.run(["node", str(ACTION_DIR / "main.js")], capture_output=True, text=True, env=values, timeout=120, check=False)
	assert main.returncode == 0, main.stderr
	outputs = _read_outputs(Path(values["GITHUB_OUTPUT"]))
	assert (outputs["available"], outputs["reason"]) == ("true", "status")
	assert json.loads(outputs["status"])["gate"] == 0.9
	assert not env["pool"].exists()


def test_action_post_cleans_with_trailing_runner_temp_slash(env: dict) -> None:
	(env["pool"] / "ALPHA").mkdir(parents=True)
	post = subprocess.run(
		["node", str(ACTION_DIR / "post.js")],
		capture_output=True, text=True, env=dict(env["env"], RUNNER_TEMP=str(env["rt"]) + "/"), check=False,
	)
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


def test_action_package_json_pins_commonjs() -> None:
	assert json.loads((ACTION_DIR / "package.json").read_text(encoding="utf-8")) == {"type": "commonjs"}


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
	values = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "RUNNER_TEMP": str(runner_temp), "GITHUB_OUTPUT": str(tmp_path / "out")}
	# scripts/chatgpt_pool_token.sh is absent here, so main.js's own fallback must run.
	main = subprocess.run(["node", str(action / "main.js")], capture_output=True, text=True, env=values, timeout=60, check=False)
	assert main.returncode == 0, main.stderr
	assert "require is not defined" not in main.stderr
	assert "CHATGPT_POOL available=false reason=script_failed" in main.stdout
	assert _read_outputs(tmp_path / "out") == {"available": "false", "reason": "script_failed", "account": "", "codex_home": "", "status": ""}
	pool = runner_temp / "chatgpt-pool"
	(pool / "ALPHA").mkdir(parents=True)
	post = subprocess.run(["node", str(action / "post.js")], capture_output=True, text=True, env=values, timeout=60, check=False)
	assert post.returncode == 0, post.stderr
	assert "CHATGPT_POOL cleanup removed=true" in post.stdout
	assert not pool.exists()
	(action / "package.json").unlink()
	broken = subprocess.run(["node", str(action / "main.js")], capture_output=True, text=True, env=values, timeout=60, check=False)
	assert broken.returncode != 0
	assert "require is not defined" in broken.stderr


def test_action_metadata() -> None:
	text = (ACTION_DIR / "action.yml").read_text(encoding="utf-8")
	assert "using: node24" in text
	assert "main: main.js" in text and "post: post.js" in text and "post-if: always()" in text
	outputs = text.split("\noutputs:\n", 1)[1].split("\nruns:\n", 1)[0]
	assert re.findall(r"^  ([a-z_]+):$", outputs, re.M) == ["available", "reason", "account", "codex_home", "status"]
	inputs = text.split("\ninputs:\n", 1)[1].split("\noutputs:\n", 1)[0]
	assert re.findall(r"^  ([a-z_]+):$", inputs, re.M) == ["mode", "config_path"]
	assert "id-token: write" in text


def test_workflows_using_the_action_are_in_the_broker_allowlist() -> None:
	source = (REPO_ROOT / "tools" / "claude-pool-broker" / "src" / "index.ts").read_text(encoding="utf-8")
	match = re.search(r"ALLOWED_WORKFLOW_FILES[^=]*=\s*Object\.freeze\(\[(.*?)\]\)", source, re.S)
	assert match, "ALLOWED_WORKFLOW_FILES not found in the broker source"
	allowed = set(re.findall(r'"([^"]+)"', match.group(1)))
	assert allowed, "ALLOWED_WORKFLOW_FILES parsed empty"
	users = {
		path.name
		for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml")
		if ".github/actions/chatgpt-pool-token" in path.read_text(encoding="utf-8", errors="replace")
	}
	assert users <= allowed, sorted(users - allowed)
