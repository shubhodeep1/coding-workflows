#!/usr/bin/env python3
"""Webhook signature checks, body limits and /healthz of the runner scaler (plan Phase 2)."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.request

import pytest

import runner_scaler_fakes  # noqa: F401  (puts tools/runner_scaler on sys.path)
from runner_scaler.server import MAX_BODY_BYTES, App, make_server, signature_valid

SECRET = "webhook-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
	return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_valid_only_for_the_exact_body_and_secret() -> None:
	body = b'{"action":"queued"}'
	assert signature_valid(SECRET, body, _sign(body))
	assert not signature_valid(SECRET, body + b" ", _sign(body))
	assert not signature_valid(SECRET, body, _sign(body, "other"))
	assert not signature_valid(SECRET, body, None)
	assert not signature_valid(SECRET, body, "sha1=abc")
	assert not signature_valid("", body, _sign(body, ""))
	assert not signature_valid(SECRET, body, "sha256=\u00e9" + _sign(body)[8:])  # non-ASCII: False, not TypeError


class _Recorder:
	def __init__(self) -> None:
		self.events: list[tuple[str, object]] = []
		self.healthy = True

	def handle(self, event: str, payload: object) -> str:
		self.events.append((event, payload))
		return "queued" if event == "workflow_job" else "ping"


@pytest.fixture()
def server():
	recorder = _Recorder()
	wake = threading.Event()
	app = App(secret=SECRET, handle_event=recorder.handle, healthy=lambda: recorder.healthy, wake=wake, log=lambda *a, **k: None)
	httpd = make_server(app, 0)
	thread = threading.Thread(target=httpd.serve_forever, daemon=True)
	thread.start()
	yield f"http://127.0.0.1:{httpd.server_address[1]}", recorder, wake
	httpd.shutdown()


def _post(url: str, body: bytes, headers: dict[str, str]) -> int:
	request = urllib.request.Request(url, data=body, method="POST", headers=headers)
	try:
		with urllib.request.urlopen(request, timeout=5) as response:
			return response.status
	except urllib.error.HTTPError as exc:
		return exc.code


def test_signed_workflow_job_is_accepted_and_wakes_the_loop(server) -> None:
	base, recorder, wake = server
	body = json.dumps({"action": "queued"}).encode()
	status = _post(f"{base}/webhook", body, {"X-Hub-Signature-256": _sign(body), "X-GitHub-Event": "workflow_job", "Content-Type": "application/json"})
	assert status == 202
	assert recorder.events == [("workflow_job", {"action": "queued"})]
	assert wake.is_set()


def test_unsigned_or_wrongly_signed_webhook_is_rejected(server) -> None:
	base, recorder, wake = server
	body = b'{"action":"queued"}'
	assert _post(f"{base}/webhook", body, {"X-GitHub-Event": "workflow_job"}) == 401
	assert _post(f"{base}/webhook", body, {"X-GitHub-Event": "workflow_job", "X-Hub-Signature-256": _sign(body, "wrong")}) == 401
	assert recorder.events == [] and not wake.is_set()


def test_oversized_body_is_refused_before_reading(server) -> None:
	base, recorder, _ = server
	request = urllib.request.Request(f"{base}/webhook", data=b"x", method="POST", headers={"Content-Length": str(MAX_BODY_BYTES + 1)})
	try:
		urllib.request.urlopen(request, timeout=5)
		status = 200
	except urllib.error.HTTPError as exc:
		status = exc.code
	except (urllib.error.URLError, ConnectionError):
		status = 413  # the server may close before the client finishes sending
	assert status == 413
	assert recorder.events == []


def test_healthz_follows_the_reconcile_loop(server) -> None:
	base, recorder, _ = server
	with urllib.request.urlopen(f"{base}/healthz", timeout=5) as response:
		assert response.status == 200
	recorder.healthy = False
	with pytest.raises(urllib.error.HTTPError) as excinfo:
		urllib.request.urlopen(f"{base}/healthz", timeout=5)
	assert excinfo.value.code == 503


def test_unknown_paths_are_not_found(server) -> None:
	base, _, _ = server
	with pytest.raises(urllib.error.HTTPError) as excinfo:
		urllib.request.urlopen(f"{base}/", timeout=5)
	assert excinfo.value.code == 404
	assert _post(f"{base}/other", b"{}", {}) == 404
