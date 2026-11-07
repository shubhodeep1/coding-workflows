#!/usr/bin/env python3
"""scripts/chatgpt_pool_token.sh and .github/actions/chatgpt-pool-token.

docs/plans/chatgpt-account-reviewer-trial-plan.md, Phase 1. A stub HTTP server
plays the Actions OIDC endpoint and the claude-pool-broker Worker's
`POST /v1/chatgpt` and `GET /v1/chatgpt/status` on loopback. Checks the
auth.json layout and mode, refresh-token stripping, masking, every
available=false reason (including broker_refused_chatgpt_pool_empty), the
status-output allowlist, the post-step cleanup, and that every workflow that
uses the action is in the broker's ALLOWED_WORKFLOW_FILES.
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
OUTPUT_KEYS = ["available", "reason", "account", "codex_home", "status"]

# A loopback-only stand-in for the curl flags scripts/chatgpt_pool_token.sh
# uses (-sS -o FILE -w '%{http_code}' --max-time N -X METHOD -H VALUE|@FILE URL).
# Installed only when the test host has no curl (the credential-free pre-push
# container), so the script's own HTTP paths still run against the stub;
# GitHub runners have curl and exercise the real binary.
CURL_SHIM = """#!/usr/bin/env python3
import sys, urllib.error, urllib.request
args = sys.argv[1:]
out, fmt, method, timeout, headers, url = None, "", None, 30.0, {}, None
i = 0
while i < len(args):
	arg = args[i]
	if arg in ("-o", "-w", "-X", "-H", "--max-time"):
		value = args[i + 1]
		i += 2
		if arg == "-o":
			out = value
		elif arg == "-w":
			fmt = value
		elif arg == "-X":
			method = value
		elif arg == "--max-time":
			timeout = float(value)
		else:
			lines = open(value[1:], encoding="utf-8").read().splitlines() if value.startswith("@") else [value]
			for line in lines:
				key, _, val = line.partition(":")
				if key.strip() and key.strip().lower() != "content-length":
					headers[key.strip()] = val.strip()
		continue
	if not arg.startswith("-"):
		url = arg
	i += 1
data = b"" if method in ("POST", "PUT") else None
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
	try:
		response = opener.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=timeout)
	except urllib.error.HTTPError as error:
		response = error
	code, body = response.getcode(), response.read()
except Exception:
	sys.stdout.write(fmt.replace("%{http_code}", "000"))
	sys.exit(7)
if out:
	open(out, "wb").write(body)
sys.stdout.write(fmt.replace("%{http_code}", "%03d" % code))
"""


def _install_curl_shim_if_missing(tmp_path: Path, values: dict) -> None:
	if shutil.which("curl", path=values.get("PATH")) is not None:
		return
	shim_dir = tmp_path / "curl-shim"
	shim_dir.mkdir()
	shim = shim_dir / "curl"
	shim.write_text(CURL_SHIM, encoding="utf-8")
	shim.chmod(0o755)
	values["PATH"] = f"{shim_dir}{os.pathsep}{values.get('PATH', '')}"


def auth_json() -> dict:
	return {
		"OPENAI_API_KEY": None,
		"tokens": {
			"id_token": "idtok-alpha-secret",
			"access_token": "accesstok-alpha-secret",
			"refresh_token": "refreshtok-alpha-secret",
			"account_id": "acct-alpha-secret",
		},
		"last_refresh": "2026-10-04T00:00:00Z",
	}


def status_body() -> dict:
	return {
		"gate": 0.9,
		"accounts": [
			{
				"account": "ALPHA", "generation": 3, "last_refresh": "2026-10-04T12:00:00.000Z", "last_outcome": "ok",
				"forced_refresh": False, "checked_at": "2026-10-04T11:59:00.000Z",
				"usage": {"five_hour": 0.1, "weekly": 0.2, "resets": {"five_hour": None, "weekly": "2026-10-08T00:00:00.000Z"}},
				"usable": True, "reason": "ok",
			},
			{
				"account": "BETA", "generation": 1, "last_refresh": "2026-09-20T00:00:00.000Z", "last_outcome": "auth_failed",
				"forced_refresh": True, "checked_at": None, "usage": None, "usable": False, "reason": "stale",
			},
		],
	}


class _Stub(http.server.BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.0"
	pick_status = 200
	pick_body: dict = {}
	status_status = 200
	status_body: dict = {}
	seen: list[dict] = []
	oidc_statuses: list[int] = []

	def log_message(self, *_args):
		pass

	def _send(self, status: int, body) -> None:
		payload = (body if isinstance(body, str) else json.dumps(body)).encode()
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(payload)))
		self.end_headers()
		self.wfile.write(payload)

	def _authorized(self) -> bool:
		return self.headers.get("Authorization") == f"Bearer {JWT}"

	def do_GET(self):
		_Stub.seen.append({"method": "GET", "path": self.path})
		if self.path.startswith("/oidc?") and self.headers.get("Authorization") == f"bearer {REQUEST_TOKEN}":
			if _Stub.oidc_statuses:
				status = _Stub.oidc_statuses.pop(0)
				self._send(status, {"value": JWT} if status == 200 else {})
			else:
				self._send(200, {"value": JWT})
		elif self.path == "/v1/chatgpt/status" and self._authorized():
			self._send(_Stub.status_status, _Stub.status_body)
		else:
			self._send(401, {})

	def do_POST(self):
		_Stub.seen.append({"method": "POST", "path": self.path})
		if self.path == "/v1/chatgpt" and self._authorized():
			self._send(_Stub.pick_status, _Stub.pick_body)
		else:
			self._send(403, {"error": "missing_token"})


@pytest.fixture()
def env(tmp_path: Path):
	server = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	port = server.server_address[1]
	_Stub.seen = []
	_Stub.oidc_statuses = []
	_Stub.pick_status = 200
	_Stub.pick_body = {"account": "ALPHA", "auth_json": auth_json()}
	_Stub.status_status = 200
	_Stub.status_body = status_body()
	config = tmp_path / "claude_engine.json"
	config.write_text(json.dumps({"broker_url": f"http://127.0.0.1:{port}/v1/pool"}), encoding="utf-8")
	runner_temp = tmp_path / "rt"
	runner_temp.mkdir()
	values = {
		key: value
		for key, value in os.environ.items()
		if not key.startswith(("CLAUDE_", "CHATGPT_", "ACTIONS_ID_TOKEN", "GITHUB_OUTPUT")) and key != "GITHUB_ACTIONS"
		and key.lower() not in ("http_proxy", "https_proxy", "all_proxy")
	}
	values.update(
		{
			"GITHUB_ACTIONS": "false",
			"RUNNER_TEMP": str(runner_temp),
			"GITHUB_OUTPUT": str(tmp_path / "output.txt"),
			"CLAUDE_ENGINE_CONFIG": str(config),
			"ACTIONS_ID_TOKEN_REQUEST_URL": f"http://127.0.0.1:{port}/oidc?api-version=2.0",
			"ACTIONS_ID_TOKEN_REQUEST_TOKEN": REQUEST_TOKEN,
			"NO_PROXY": "127.0.0.1,localhost",
			"no_proxy": "127.0.0.1,localhost",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	_install_curl_shim_if_missing(tmp_path, values)
	yield {"env": values, "tmp": tmp_path, "pool": runner_temp / "chatgpt-pool", "config": config}
	server.shutdown()
	server.server_close()


def _read_outputs(path: Path) -> dict[str, str]:
	outputs: dict[str, str] = {}
	if path.exists():
		for line in path.read_text(encoding="utf-8").splitlines():
			key, _, value = line.partition("=")
			outputs[key] = value
	return outputs


def _run(env: dict, **extra) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
	values = dict(env["env"], **extra)
	for key in [k for k, v in values.items() if v is None]:
		values.pop(key)
	result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=values, timeout=120, check=False)
	return result, _read_outputs(Path(values["GITHUB_OUTPUT"]))


def _without_masks(text: str) -> str:
	return "\n".join(line for line in text.splitlines() if not line.startswith("::add-mask::"))


def _broker_calls() -> list[dict]:
	return [entry for entry in _Stub.seen if entry["path"].startswith("/v1/")]


def test_token_mode_writes_auth_json_without_the_refresh_token(env: dict) -> None:
	result, outputs = _run(env)
	assert result.returncode == 0, result.stderr
	home = env["pool"] / "ALPHA"
	assert outputs == {"available": "true", "reason": "selected", "account": "ALPHA", "codex_home": str(home), "status": "{}"}
	assert list(outputs) == OUTPUT_KEYS
	written = json.loads((home / "auth.json").read_text(encoding="utf-8"))
	expected = auth_json()
	del expected["tokens"]["refresh_token"]
	assert written == expected
	assert stat.S_IMODE((home / "auth.json").stat().st_mode) == 0o600
	for path in (env["pool"], home):
		assert stat.S_IMODE(path.stat().st_mode) == 0o700
	assert sorted(p.name for p in env["pool"].iterdir()) == ["ALPHA"]
	# Every secret is masked, and nothing else prints it.
	for secret in (JWT, "idtok-alpha-secret", "accesstok-alpha-secret", "acct-alpha-secret"):
		assert f"::add-mask::{secret}" in result.stdout
	rest = _without_masks(result.stdout + result.stderr)
	for secret in (JWT, REQUEST_TOKEN, "idtok-alpha-secret", "accesstok-alpha-secret", "refreshtok-alpha-secret", "acct-alpha-secret"):
		assert secret not in rest
	assert "refreshtok-alpha-secret" not in Path(env["env"]["GITHUB_OUTPUT"]).read_text(encoding="utf-8")
	assert "CHATGPT_POOL available=true reason=selected account=ALPHA" in result.stdout
	assert [(e["method"], e["path"]) for e in _broker_calls()] == [("POST", "/v1/chatgpt")]
	assert "audience=coding-workflows-claude-pool" in _Stub.seen[0]["path"]


def test_empty_pool_is_broker_refused_chatgpt_pool_empty(env: dict) -> None:
	_Stub.pick_status = 503
	_Stub.pick_body = {"error": "chatgpt_pool_empty"}
	result, outputs = _run(env)
	assert result.returncode == 0
	assert outputs == {"available": "false", "reason": "broker_refused_chatgpt_pool_empty", "account": "", "codex_home": "", "status": "{}"}
	assert not env["pool"].exists()


@pytest.mark.parametrize(
	"status, body, reason",
	[
		(403, {"error": "workflow_not_allowed"}, "broker_refused_workflow_not_allowed"),
		(403, {"error": "Weird Reason!"}, "broker_refused_unknown"),
		(503, {"error": "jwks_unavailable"}, "broker_unavailable_503"),
		(503, {"error": "chatgpt_store_unavailable"}, "broker_unavailable_503"),
		(500, {"error": "internal"}, "broker_unavailable_500"),
		(404, {"error": "not_found"}, "broker_unavailable_404"),
		(200, "not json", "broker_response_invalid"),
		(200, {"account": "bad name", "auth_json": auth_json()}, "broker_response_invalid"),
		(200, {"account": "../ALPHA", "auth_json": auth_json()}, "broker_response_invalid"),
		(200, {"account": "ALPHA", "auth_json": {"tokens": {"refresh_token": "r"}}}, "broker_response_invalid"),
		(200, {"account": "ALPHA", "auth_json": "x"}, "broker_response_invalid"),
	],
)
def test_broker_failures(env: dict, status: int, body, reason: str) -> None:
	_Stub.pick_status = status
	_Stub.pick_body = body
	result, outputs = _run(env)
	assert result.returncode == 0
	assert (outputs["available"], outputs["reason"], outputs["account"], outputs["codex_home"]) == ("false", reason, "", "")
	assert not env["pool"].exists()


def test_unconfigured_broker(env: dict) -> None:
	env["config"].write_text(json.dumps({"broker_url": ""}), encoding="utf-8")
	_, outputs = _run(env)
	assert (outputs["available"], outputs["reason"]) == ("false", "broker_not_configured")
	assert _Stub.seen == []


def test_no_oidc_permission(env: dict) -> None:
	_, outputs = _run(env, ACTIONS_ID_TOKEN_REQUEST_URL=None, ACTIONS_ID_TOKEN_REQUEST_TOKEN=None)
	assert (outputs["available"], outputs["reason"]) == ("false", "oidc_unavailable")
	assert _Stub.seen == []


def test_untrusted_broker_url_is_refused_before_requesting_oidc(env: dict) -> None:
	env["config"].write_text(json.dumps({"broker_url": "https://attacker.example/v1/pool"}), encoding="utf-8")
	_, outputs = _run(env)
	assert outputs["available"] == "false"
	assert outputs["reason"] in ("broker_not_configured", "broker_url_invalid")
	assert _Stub.seen == []


def test_actions_job_cannot_use_loopback_broker(env: dict) -> None:
	_, outputs = _run(env, GITHUB_ACTIONS="true")
	assert (outputs["available"], outputs["reason"]) == ("false", "broker_url_invalid")
	assert _Stub.seen == []


def test_missing_config(env: dict) -> None:
	_, outputs = _run(env, CLAUDE_ENGINE_CONFIG=str(env["tmp"] / "absent.json"))
	assert (outputs["available"], outputs["reason"]) == ("false", "config_missing")


def test_oidc_401_is_not_retried(env: dict) -> None:
	result, outputs = _run(env, ACTIONS_ID_TOKEN_REQUEST_TOKEN="wrong", CHATGPT_POOL_OIDC_RETRY_SLEEPS="0 0")
	assert (outputs["available"], outputs["reason"]) == ("false", "oidc_request_failed_401")
	assert len(_Stub.seen) == 1
	assert "oidc_retry" not in result.stdout


def test_oidc_503_is_retried(env: dict) -> None:
	_Stub.oidc_statuses = [503, 200]
	result, outputs = _run(env, CHATGPT_POOL_OIDC_RETRY_SLEEPS="0 0")
	assert (outputs["available"], outputs["reason"]) == ("true", "selected")
	assert "CHATGPT_POOL oidc_retry attempt=2 code=503" in result.stdout


def test_oidc_retry_gives_up_after_three_attempts(env: dict) -> None:
	_Stub.oidc_statuses = [503, 503, 503, 200]
	result, outputs = _run(env, CHATGPT_POOL_OIDC_RETRY_SLEEPS="0 0")
	assert (outputs["available"], outputs["reason"]) == ("false", "oidc_request_failed_503")
	assert _broker_calls() == []
	assert "attempt=4" not in result.stdout


@pytest.mark.parametrize("sleeps", ["0", "0 0 0", "x y", "61 0"])
def test_oidc_retry_invalid_sleeps_fall_back(env: dict, sleeps: str) -> None:
	result, outputs = _run(env, CHATGPT_POOL_OIDC_RETRY_SLEEPS=sleeps)
	assert outputs["available"] == "true"
	assert [line for line in result.stdout.splitlines() if "oidc_retry_sleeps_invalid" in line] == ["CHATGPT_POOL oidc_retry_sleeps_invalid fallback=2,5"]


def test_status_mode_outputs_allowlisted_json_and_no_files(env: dict) -> None:
	body = status_body()
	body["accounts"][0]["auth_json"] = auth_json()
	body["accounts"][0]["seed_sha256"] = "a" * 64
	body["accounts"][0]["tokens"] = auth_json()["tokens"]
	body["accounts"][1]["reason"] = "Bad\nreason"
	body["auth_json"] = auth_json()
	_Stub.status_body = body
	result, outputs = _run(env, CHATGPT_POOL_MODE="status")
	assert result.returncode == 0, result.stderr
	assert (outputs["available"], outputs["reason"], outputs["account"], outputs["codex_home"]) == ("true", "status_ok", "", "")
	status = json.loads(outputs["status"])
	expected = status_body()
	expected["accounts"][1]["reason"] = "unknown"
	assert status == expected
	text = outputs["status"]
	for forbidden in ("auth_json", "seed_sha256", "tokens", "secret"):
		assert forbidden not in text
	assert not env["pool"].exists()
	assert [(e["method"], e["path"]) for e in _broker_calls()] == [("GET", "/v1/chatgpt/status")]
	assert "CHATGPT_POOL status accounts=2 usable=1" in result.stdout


def test_status_mode_with_no_usable_account(env: dict) -> None:
	_Stub.status_body = {"gate": 0.9, "accounts": []}
	_, outputs = _run(env, CHATGPT_POOL_MODE="status")
	assert (outputs["available"], outputs["reason"]) == ("false", "status_ok")
	assert json.loads(outputs["status"]) == {"gate": 0.9, "accounts": []}
	assert not env["pool"].exists()


def test_status_mode_broker_failure(env: dict) -> None:
	_Stub.status_status = 503
	_Stub.status_body = {"error": "chatgpt_store_unavailable"}
	_, outputs = _run(env, CHATGPT_POOL_MODE="status")
	assert (outputs["available"], outputs["reason"], outputs["status"]) == ("false", "broker_unavailable_503", "{}")


def test_invalid_mode(env: dict) -> None:
	_, outputs = _run(env, CHATGPT_POOL_MODE="steal")
	assert (outputs["available"], outputs["reason"]) == ("false", "mode_invalid")
	assert _Stub.seen == []


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


def test_action_main_and_post(env: dict) -> None:
	values = dict(env["env"], INPUT_CONFIG_PATH=str(env["config"]), INPUT_MODE="token")
	values.pop("CLAUDE_ENGINE_CONFIG")
	main = subprocess.run(["node", str(ACTION_DIR / "main.js")], capture_output=True, text=True, env=values, timeout=120, check=False)
	assert main.returncode == 0, main.stderr
	assert "CHATGPT_POOL available=true reason=selected account=ALPHA" in main.stdout
	assert (env["pool"] / "ALPHA" / "auth.json").exists()
	post = subprocess.run(["node", str(ACTION_DIR / "post.js")], capture_output=True, text=True, env=values, timeout=60, check=False)
	assert post.returncode == 0
	assert "CHATGPT_POOL cleanup removed=true" in post.stdout
	assert not env["pool"].exists()


def test_action_main_status_mode(env: dict) -> None:
	values = dict(env["env"], INPUT_MODE="status")
	main = subprocess.run(["node", str(ACTION_DIR / "main.js")], capture_output=True, text=True, env=values, timeout=120, check=False)
	assert main.returncode == 0, main.stderr
	outputs = _read_outputs(Path(values["GITHUB_OUTPUT"]))
	assert (outputs["available"], outputs["reason"]) == ("true", "status_ok")
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
	out = tmp_path / "out"
	values = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "RUNNER_TEMP": str(runner_temp), "GITHUB_OUTPUT": str(out)}
	# scripts/chatgpt_pool_token.sh is absent here, so main.js's own fallback must run.
	main = subprocess.run(["node", str(action / "main.js")], capture_output=True, text=True, env=values, timeout=60, check=False)
	assert main.returncode == 0, main.stderr
	assert "require is not defined" not in main.stderr
	assert "CHATGPT_POOL available=false reason=script_failed" in main.stdout
	assert _read_outputs(out) == {"available": "false", "reason": "script_failed", "account": "", "codex_home": "", "status": "{}"}
	pool = runner_temp / "chatgpt-pool"
	(pool / "ALPHA").mkdir(parents=True)
	(pool / "ALPHA" / "auth.json").write_text("{}", encoding="utf-8")
	post = subprocess.run(["node", str(action / "post.js")], capture_output=True, text=True, env=values, timeout=60, check=False)
	assert post.returncode == 0, post.stderr
	assert "CHATGPT_POOL cleanup removed=true" in post.stdout
	assert not pool.exists()


def test_action_metadata() -> None:
	text = (ACTION_DIR / "action.yml").read_text(encoding="utf-8")
	assert "using: node24" in text
	assert "main: main.js" in text and "post: post.js" in text and "post-if: always()" in text
	assert re.search(r"^  mode:\n(?:    .*\n)*?    default: token$", text, re.M)
	assert re.search(r"^  config_path:$", text, re.M)
	outputs = re.search(r"^outputs:\n(.*?)^runs:", text, re.M | re.S)
	assert outputs
	assert re.findall(r"^  ([a-z_]+):$", outputs.group(1), re.M) == ["available", "reason", "account", "codex_home", "status"]


def test_script_env_reads_all_have_defaults() -> None:
	"""Unattended §8: every variable the helper reads with ${VAR...} has a default in the script."""
	text = SCRIPT.read_text(encoding="utf-8")
	for name in ("CHATGPT_POOL_MODE", "CLAUDE_ENGINE_CONFIG", "CHATGPT_POOL_DIR", "CHATGPT_POOL_OIDC_RETRY_SLEEPS", "GITHUB_OUTPUT", "GITHUB_ACTIONS", "RUNNER_TEMP"):
		assert re.search(r"\$\{" + name + r":-", text), name
	reads = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)(?=[}:])", text))
	runner_provided = {"ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"}
	for name in reads - runner_provided:
		assert re.search(r"\$\{" + name + r":-", text), f"{name} is read without a default"


def test_no_workflow_or_template_references_chatgpt_pool_secrets() -> None:
	pattern = re.compile(r"CHATGPT_POOL_AUTH_")
	offenders = []
	for base in (REPO_ROOT / ".github", REPO_ROOT / "workflow-templates"):
		if not base.exists():
			continue
		for path in base.rglob("*"):
			if path.is_file() and path.suffix in (".yml", ".yaml", ".js", ".sh", ".py", ".json"):
				if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
					offenders.append(str(path.relative_to(REPO_ROOT)))
	assert offenders == []


def test_every_workflow_using_the_action_is_in_the_broker_allowlist() -> None:
	source = (REPO_ROOT / "tools" / "claude-pool-broker" / "src" / "index.ts").read_text(encoding="utf-8")
	match = re.search(r"ALLOWED_WORKFLOW_FILES[^=]*=\s*Object\.freeze\(\[(.*?)\]\)", source, re.S)
	assert match, "ALLOWED_WORKFLOW_FILES not found in the broker source"
	allowed = set(re.findall(r'"([^"]+)"', match.group(1)))
	assert "review_autofix.yml" in allowed
	users = {
		path.name
		for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml")
		if ".github/actions/chatgpt-pool-token" in path.read_text(encoding="utf-8", errors="replace")
	}
	assert users <= allowed, sorted(users - allowed)
