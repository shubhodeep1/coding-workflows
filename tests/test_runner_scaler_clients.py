#!/usr/bin/env python3
"""GitHub App, DigitalOcean, Telegram and HTTP plumbing of the runner scaler, plus a process smoke test."""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from runner_scaler_fakes import SCALER_ROOT, Clock
from runner_scaler.alerts import Alerter
from runner_scaler.api import ApiError, RateLimited, request_json
from runner_scaler.do_client import DigitalOcean
from runner_scaler.github_app import GitHubApp


class Recorder:
	def __init__(self, responses: dict[tuple[str, str], object]):
		self.responses = responses
		self.calls: list[tuple[str, str, dict, object]] = []

	def __call__(self, method: str, url: str, *, headers: dict, body: object = None) -> object:
		self.calls.append((method, url, headers, body))
		for (want_method, fragment), response in self.responses.items():
			if method == want_method and fragment in url:
				if isinstance(response, Exception):
					raise response
				return response
		raise AssertionError(f"unexpected call {method} {url}")


def _app(recorder: Recorder, clock: Clock) -> GitHubApp:
	return GitHubApp(app_id="123", private_key="key", clock=clock, request=recorder, jwt_encode=lambda payload, key: f"jwt-{payload['iss']}-{payload['exp'] - payload['iat']}")


def test_installation_token_is_minted_once_and_reused_until_near_expiry() -> None:
	clock = Clock()
	recorder = Recorder({
		("GET", "/repos/o/r/installation"): {"id": 77},
		("POST", "/app/installations/77/access_tokens"): {"token": "inst-token"},
		("GET", "/repos/o/r/actions/runners"): {"runners": [{"name": "do-runner-r-1", "status": "online"}]},
	})
	app = _app(recorder, clock)
	assert app.list_runners("o/r")[0]["name"] == "do-runner-r-1"
	app.list_runners("o/r")
	assert [call[0] + " " + call[1].split("?")[0] for call in recorder.calls] == [
		"GET https://api.github.com/repos/o/r/installation",
		"POST https://api.github.com/app/installations/77/access_tokens",
		"GET https://api.github.com/repos/o/r/actions/runners",
		"GET https://api.github.com/repos/o/r/actions/runners",
	]
	assert recorder.calls[0][2]["Authorization"] == "Bearer jwt-123-600"
	assert recorder.calls[2][2]["Authorization"] == "Bearer inst-token"
	clock.advance(3600)
	app.list_runners("o/r")
	assert sum(1 for call in recorder.calls if "access_tokens" in call[1]) == 2


def test_jit_config_rerun_annotations_and_jobs_requests() -> None:
	clock = Clock()
	recorder = Recorder({
		("GET", "/installation"): {"id": 1},
		("POST", "/access_tokens"): {"token": "t"},
		("POST", "/actions/runners/generate-jitconfig"): {"runner": {"id": 9}, "encoded_jit_config": "abc"},
		("POST", "/actions/jobs/5/rerun"): None,
		("GET", "/check-runs/5/annotations"): [{"message": "m"}],
		("GET", "/actions/runs/4/jobs"): {"jobs": [{"id": 5}]},
		("GET", "/actions/runs?"): {"workflow_runs": [{"id": 4}]},
	})
	app = _app(recorder, clock)
	assert app.generate_jit_config("o/r", name="do-runner-r-1", label="do-runner") == (9, "abc")
	jit_call = next(call for call in recorder.calls if "generate-jitconfig" in call[1])
	assert jit_call[3] == {"name": "do-runner-r-1", "runner_group_id": 1, "labels": ["do-runner"], "work_folder": "_work"}
	app.rerun_job("o/r", 5)
	assert app.job_annotations("o/r", 5) == [{"message": "m"}]
	assert app.list_run_jobs("o/r", 4) == [{"id": 5}]
	assert app.list_runs("o/r", status="queued", per_page=20) == [{"id": 4}]
	runs_call = next(call for call in recorder.calls if "/actions/runs?" in call[1])
	assert "status=queued" in runs_call[1] and "per_page=20" in runs_call[1]


def test_unauthorized_response_drops_the_cached_token() -> None:
	clock = Clock()
	recorder = Recorder({("GET", "/installation"): {"id": 1}, ("POST", "/access_tokens"): {"token": "t"}, ("GET", "/actions/runners"): ApiError(401, "bad credentials")})
	app = _app(recorder, clock)
	with pytest.raises(ApiError):
		app.list_runners("o/r")
	assert not app._tokens


def test_installation_tokens_are_kept_per_owner() -> None:
	clock = Clock()
	recorder = Recorder({
		("GET", "/repos/a/r/installation"): {"id": 1},
		("GET", "/repos/b/r/installation"): {"id": 2},
		("POST", "/app/installations/1/access_tokens"): {"token": "token-a"},
		("POST", "/app/installations/2/access_tokens"): {"token": "token-b"},
		("GET", "/actions/runners"): {"runners": []},
	})
	app = _app(recorder, clock)
	app.list_runners("a/r")
	app.list_runners("b/r")
	app.list_runners("a/r")
	auth = [call[2]["Authorization"] for call in recorder.calls if "/actions/runners" in call[1]]
	assert auth == ["Bearer token-a", "Bearer token-b", "Bearer token-a"]
	assert sum(1 for call in recorder.calls if "access_tokens" in call[1]) == 2


def test_digitalocean_requests() -> None:
	recorder = Recorder({("POST", "/v2/droplets"): {"droplet": {"id": 3}}, ("GET", "/v2/droplets?tag_name=gh-runner"): {"droplets": [{"id": 3}]}, ("DELETE", "/v2/droplets/3"): None})
	do = DigitalOcean(token="do-token", request=recorder)
	assert do.create_droplet(name="n", region="nyc3", size="s", image="i", tags=["gh-runner"], user_data="#!", vpc_uuid="v-1")["id"] == 3
	body = recorder.calls[0][3]
	assert body["user_data"] == "#!" and body["tags"] == ["gh-runner"] and body["monitoring"] is False and body["vpc_uuid"] == "v-1"
	with pytest.raises(ValueError):
		do.create_droplet(name="n", region="nyc3", size="s", image="i", tags=["gh-runner"], user_data="#!", vpc_uuid="")
	assert recorder.calls[0][2] == {"Authorization": "Bearer do-token"}
	assert do.list_droplets(tag="gh-runner") == [{"id": 3}]
	do.delete_droplet(3)


def test_alerts_are_deduplicated_and_never_log_the_bot_token() -> None:
	clock = Clock()
	lines: list[tuple[str, dict]] = []
	sent: list[tuple[str, object]] = []
	alerter = Alerter(bot_token="SECRET-BOT", chat_id="42", cooldown_seconds=3600, clock=clock, request=lambda method, url, *, headers, body: sent.append((url, body)), log=lambda outcome, **fields: lines.append((outcome, fields)))
	assert alerter.alert("breaker_open", "x")
	assert not alerter.alert("breaker_open", "x")
	clock.advance(3601)
	assert alerter.alert("breaker_open", "x")
	assert len(sent) == 2 and sent[0][1]["chat_id"] == "42"
	assert "SECRET-BOT" not in json.dumps(lines)
	failing = Alerter(bot_token="SECRET-BOT", chat_id="42", cooldown_seconds=1, clock=clock, request=lambda *a, **k: (_ for _ in ()).throw(ApiError(500, "x")), log=lambda outcome, **fields: lines.append((outcome, fields)))
	failing.alert("k", "x")
	assert lines[-1] == ("alert_not_sent", {"key": "k", "reason": "telegram_error", "status": 500})


class _Response(io.BytesIO):
	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False


def _http_error(code: int, body: bytes, headers: dict | None = None) -> urllib.error.HTTPError:
	return urllib.error.HTTPError("https://api.example/secret-path", code, "x", headers or {}, io.BytesIO(body))


def test_request_json_maps_errors_without_leaking_the_url() -> None:
	assert request_json("GET", "https://x", headers={}, opener=lambda request, timeout: _Response(b'{"a": 1}')) == {"a": 1}
	assert request_json("DELETE", "https://x", headers={}, opener=lambda request, timeout: _Response(b"")) is None
	with pytest.raises(RateLimited):
		request_json("GET", "https://x", headers={}, opener=lambda request, timeout: (_ for _ in ()).throw(_http_error(429, b"{}")))
	with pytest.raises(RateLimited):
		request_json("GET", "https://x", headers={}, opener=lambda request, timeout: (_ for _ in ()).throw(_http_error(403, b"{}", {"x-ratelimit-remaining": "0"})))
	with pytest.raises(ApiError) as excinfo:
		request_json("GET", "https://x", headers={}, opener=lambda request, timeout: (_ for _ in ()).throw(_http_error(404, b'{"message": "Not Found"}')))
	assert excinfo.value.status == 404 and "secret-path" not in str(excinfo.value)
	with pytest.raises(ApiError) as excinfo:
		request_json("GET", "https://x", headers={}, opener=lambda request, timeout: (_ for _ in ()).throw(urllib.error.URLError("dns")))
	assert excinfo.value.status == 0


def _free_port() -> int:
	with socket.socket() as sock:
		sock.bind(("127.0.0.1", 0))
		return sock.getsockname()[1]


def test_process_serves_healthz_and_stops_cleanly_on_sigterm() -> None:
	port = _free_port()
	env = {"PATH": os.environ.get("PATH", ""), "PORT": str(port), "PYTHONDONTWRITEBYTECODE": "1"}
	proc = subprocess.Popen([sys.executable, "-m", "runner_scaler"], cwd=SCALER_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
	try:
		deadline = time.time() + 15
		status = None
		while time.time() < deadline:
			try:
				with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
					status = response.status
					break
			except (urllib.error.URLError, ConnectionError):
				time.sleep(0.2)
		assert status == 200
		proc.send_signal(signal.SIGTERM)
		output, _ = proc.communicate(timeout=20)
	finally:
		if proc.poll() is None:
			proc.kill()
	assert proc.returncode == 0, output
	assert "RUNNER_SCALER outcome=started repos=0" in output
	assert "RUNNER_SCALER outcome=stopped" in output


def test_process_refuses_to_start_with_repos_but_no_credentials() -> None:
	env = {"PATH": os.environ.get("PATH", ""), "RUNNER_SCALER_REPOS": "o/r", "PYTHONDONTWRITEBYTECODE": "1"}
	result = subprocess.run([sys.executable, "-m", "runner_scaler"], cwd=SCALER_ROOT, env=env, capture_output=True, text=True, timeout=20)
	assert result.returncode == 2
	assert "outcome=config_error reason=missing_required" in result.stdout
	assert "RUNNER_SCALER_VPC_UUID" in result.stdout


def test_process_refuses_to_start_without_a_vpc_even_with_credentials() -> None:
	env = {
		"PATH": os.environ.get("PATH", ""),
		"RUNNER_SCALER_REPOS": "o/r",
		"RUNNER_SCALER_DO_TOKEN": "t",
		"RUNNER_SCALER_GH_APP_ID": "1",
		"RUNNER_SCALER_GH_APP_PRIVATE_KEY": "k",
		"RUNNER_SCALER_WEBHOOK_SECRET": "s",
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	result = subprocess.run([sys.executable, "-m", "runner_scaler"], cwd=SCALER_ROOT, env=env, capture_output=True, text=True, timeout=20)
	assert result.returncode == 2
	assert "names=RUNNER_SCALER_VPC_UUID" in result.stdout
