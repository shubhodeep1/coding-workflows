#!/usr/bin/env python3
"""scripts/claude_anthropic_relay.py: the token never leaves the host relay.

Runs the container-side bridge (loopback) and the host-side broker (Unix
socket) in threads against a fake upstream that stands in for
api.anthropic.com, and checks the token swap, the request and response
allow lists, and every rejection.
"""

from __future__ import annotations

import http.client
import http.server
import importlib.util
import json
import os
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "claude_anthropic_relay.py"
REAL_TOKEN = "sk-ant-oat01-REALTOKENVALUE-0123456789"
MODEL = "claude-opus-5-5"


def _load():
	spec = importlib.util.spec_from_file_location("claude_anthropic_relay", SCRIPT)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


relay = _load()


class _Upstream(http.server.BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.0"
	seen: list[dict] = []

	def log_message(self, *_args):
		pass

	def do_POST(self):
		body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
		_Upstream.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
		payload = b'event: message_start\ndata: {"ok": true}\n\n'
		self.send_response(200)
		self.send_header("Content-Type", "text/event-stream")
		self.send_header("anthropic-ratelimit-unified-5h-utilization", "0.25")
		self.send_header("request-id", "req_1")
		self.send_header("x-internal-debug", "secret-upstream-detail")
		self.send_header("Content-Length", str(len(payload)))
		self.end_headers()
		self.wfile.write(payload)


def _serve(server) -> threading.Thread:
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	return thread


@pytest.fixture()
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
	"""bridge (127.0.0.1:<port>) → broker (unix socket) → fake upstream."""
	_Upstream.seen = []
	upstream = http.server.HTTPServer(("127.0.0.1", 0), _Upstream)
	_serve(upstream)
	upstream_port = upstream.server_address[1]
	real_https = http.client.HTTPSConnection

	def fake_https(host, timeout=None, context=None):
		assert host == "api.anthropic.com"
		return http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=timeout)

	monkeypatch.setattr(http.client, "HTTPSConnection", fake_https)
	token_file = tmp_path / "token"
	token_file.write_text(f"  {REAL_TOKEN[:20]}\n{REAL_TOKEN[20:]}  \n", encoding="utf-8")
	os.chmod(token_file, 0o600)
	sock = str(tmp_path / "provider.sock")
	broker = relay.UnixHTTPServer(sock, relay.Relay)
	broker.mode = "broker"
	broker.token = relay.read_token(str(token_file))
	broker.models = (MODEL, "claude-haiku-4-5-20251001")
	_serve(broker)
	bridge = http.server.HTTPServer(("127.0.0.1", 0), relay.Relay)
	bridge.mode = "bridge"
	bridge.socket_path = sock
	_serve(bridge)
	yield {"bridge_port": bridge.server_address[1], "socket": sock}
	for server in (bridge, broker, upstream):
		server.shutdown()
		server.server_close()
	assert http.client.HTTPSConnection is not real_https


def _post(port: int, *, path: str = "/v1/messages?beta=true", body: dict | None = None, headers: dict | None = None, method: str = "POST"):
	data = json.dumps(body if body is not None else {"model": MODEL, "max_tokens": 5, "messages": []}).encode()
	sent = {
		"Content-Type": "application/json",
		"Authorization": "Bearer isolated-placeholder",
		"anthropic-version": "2023-06-01",
		"anthropic-beta": "claude-code-20250219",
		"x-stainless-lang": "js",
		"Cookie": "session=leak",
	}
	sent.update(headers or {})
	sent = {key: value for key, value in sent.items() if value is not None}
	connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
	connection.request(method, path, data if method == "POST" else None, sent)
	response = connection.getresponse()
	payload = response.read()
	result = (response.status, {k.lower(): v for k, v in response.getheaders()}, payload)
	connection.close()
	return result


def test_token_swap_end_to_end(chain) -> None:
	status, headers, payload = _post(chain["bridge_port"])
	assert status == 200
	assert payload.startswith(b"event: message_start")
	assert len(_Upstream.seen) == 1
	upstream = _Upstream.seen[0]
	assert upstream["path"] == "/v1/messages?beta=true"
	assert upstream["headers"]["authorization"] == "Bearer " + REAL_TOKEN
	assert "isolated-placeholder" not in json.dumps(upstream["headers"])
	assert upstream["headers"]["anthropic-beta"] == "claude-code-20250219,oauth-2025-04-20"
	assert upstream["headers"]["x-stainless-lang"] == "js"
	assert "cookie" not in upstream["headers"]
	assert json.loads(upstream["body"])["model"] == MODEL
	# Response allow list: rate-limit headers pass, upstream internals do not.
	assert headers["anthropic-ratelimit-unified-5h-utilization"] == "0.25"
	assert headers["request-id"] == "req_1"
	assert "x-internal-debug" not in headers
	assert REAL_TOKEN.encode() not in payload
	assert REAL_TOKEN not in json.dumps(headers)


def test_count_tokens_path_is_allowed(chain) -> None:
	status, _, _ = _post(chain["bridge_port"], path="/v1/messages/count_tokens?beta=true")
	assert status == 200


@pytest.mark.parametrize(
	"kwargs",
	[
		{"headers": {"Authorization": "Bearer something-else"}},
		{"headers": {"Authorization": None}},
		{"headers": {"X-Api-Key": "sk-ant-api"}},
		{"headers": {"Proxy-Authorization": "Basic x"}},
		{"headers": {"Content-Type": "text/plain"}},
		{"headers": {"Host": "api.anthropic.com"}},
		{"path": "/v1/complete"},
		{"path": "/v1/messages?beta=true&x=1"},
		{"path": "https://evil.example/v1/messages"},
		{"path": "/v1/messages/batches"},
	],
)
def test_bridge_rejects(chain, kwargs: dict) -> None:
	status, _, _ = _post(chain["bridge_port"], **kwargs)
	assert status == 400
	assert _Upstream.seen == []


def test_get_is_rejected(chain) -> None:
	status, _, _ = _post(chain["bridge_port"], method="GET")
	assert status == 405


@pytest.mark.parametrize("status", (400, 405, 502))
@pytest.mark.parametrize("timeout_error", (None, OSError("closed connection")))
def test_reject_binds_response_write_timeout(status, timeout_error) -> None:
	request = Mock()
	request.connection.settimeout.side_effect = timeout_error
	request.send_error.side_effect = lambda *_args: request.connection.settimeout.assert_called_once_with(1)
	relay.Relay._reject(request, status)
	assert request.close_connection is True


def test_broker_rejects_a_model_outside_the_allow_list(chain) -> None:
	status, _, _ = _post(chain["bridge_port"], body={"model": "claude-other-9", "messages": []})
	assert status == 400
	assert _Upstream.seen == []


def test_broker_rejects_client_authorization(chain) -> None:
	# A caller on the socket cannot bring its own credential.
	for _ in range(20):
		connection = relay.UnixHTTPConnection(chain["socket"])
		body = json.dumps({"model": MODEL}).encode()
		connection.request("POST", "/v1/messages", body, {"Content-Type": "application/json", "Authorization": "Bearer mine"})
		assert connection.getresponse().status == 400
		connection.close()
	assert _Upstream.seen == []


def test_broker_rejects_large_unauthorized_body_without_broken_pipe(chain) -> None:
	connection = relay.UnixHTTPConnection(chain["socket"])
	body = json.dumps({"model": MODEL, "padding": "x" * 262144}).encode()
	connection.request("POST", "/v1/messages", body, {"Content-Type": "application/json", "Authorization": "Bearer mine"})
	assert connection.getresponse().status == 400
	connection.close()
	assert _Upstream.seen == []


def test_rejection_restores_socket_timeout_before_reply(chain, monkeypatch) -> None:
	observed_timeouts = []
	original_send_error = relay.Relay.send_error

	def record_timeout(handler, *args, **kwargs):
		observed_timeouts.append(handler.connection.gettimeout())
		return original_send_error(handler, *args, **kwargs)

	monkeypatch.setattr(relay.Relay, "send_error", record_timeout)
	connection = relay.UnixHTTPConnection(chain["socket"])
	body = json.dumps({"model": MODEL, "padding": "x" * 262144}).encode()
	connection.request("POST", "/v1/messages", body, {"Content-Type": "application/json", "Authorization": "Bearer mine"})
	assert connection.getresponse().status == 400
	connection.close()
	assert observed_timeouts == [1]


def test_rejection_still_attempts_reply_when_timeout_reset_fails(monkeypatch) -> None:
	class BrokenSocket:
		def settimeout(self, _timeout):
			raise OSError("peer closed")

	handler = object.__new__(relay.Relay)
	handler.connection = BrokenSocket()
	replies = []
	monkeypatch.setattr(handler, "send_error", lambda status, _message: replies.append(status))
	handler._reject(400)
	assert replies == [400] and handler.close_connection is True


def test_early_rejection_does_not_wait_indefinitely_for_missing_body(chain) -> None:
	connection = relay.UnixHTTPConnection(chain["socket"])
	connection.putrequest("POST", "/v1/messages")
	connection.putheader("Content-Type", "application/json")
	connection.putheader("Authorization", "Bearer mine")
	connection.putheader("Content-Length", "100000")
	connection.endheaders()
	assert connection.getresponse().status == 400
	connection.close()
	assert _Upstream.seen == []


def test_oversized_content_length_is_rejected_without_integer_conversion(chain) -> None:
	connection = relay.UnixHTTPConnection(chain["socket"])
	connection.putrequest("POST", "/v1/messages")
	connection.putheader("Content-Type", "application/json")
	connection.putheader("Content-Length", "9" * 5000)
	connection.endheaders()
	assert connection.getresponse().status == 400
	connection.close()
	assert _Upstream.seen == []


def test_broker_rejects_without_waiting_forever_for_body(chain) -> None:
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
		client.settimeout(3)
		client.connect(chain["socket"])
		client.sendall(b"POST /v1/messages HTTP/1.0\r\nHost: localhost\r\nAuthorization: Bearer mine\r\nContent-Length: 4\r\n\r\n")
		with client.makefile("rb") as response:
			assert response.readline().startswith(b"HTTP/1.0 400")
	assert _Upstream.seen == []


def test_broker_consumes_rejected_body_before_responding(chain) -> None:
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
		client.settimeout(3)
		client.connect(chain["socket"])
		client.sendall(b"POST /v1/messages HTTP/1.0\r\nHost: localhost\r\nAuthorization: Bearer mine\r\nContent-Length: 4\r\n\r\nab")
		client.settimeout(0.2)
		with pytest.raises(socket.timeout):
			client.recv(1)
		client.settimeout(3)
		client.sendall(b"cd")
		with client.makefile("rb") as response:
			assert response.readline().startswith(b"HTTP/1.0 400")
	assert _Upstream.seen == []


def test_broker_consumes_body_for_rejected_forwarded_header(chain) -> None:
	with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
		client.settimeout(3)
		client.connect(chain["socket"])
		client.sendall(
			b"POST /v1/messages HTTP/1.0\r\nHost: localhost\r\nContent-Type: application/json\r\n"
			+ b"anthropic-version: " + b"x" * (relay.MAX_HEADER_VALUE + 1)
			+ b"\r\nContent-Length: 4\r\n\r\nab"
		)
		client.settimeout(0.2)
		with pytest.raises(socket.timeout):
			client.recv(1)
		client.settimeout(3)
		client.sendall(b"cd")
		with client.makefile("rb") as response:
			assert response.readline().startswith(b"HTTP/1.0 400")
	assert _Upstream.seen == []


def test_rejected_body_drain_has_total_deadline_and_bounded_reads(monkeypatch) -> None:
	clock = [0.0]
	requested = []
	def read_chunk(limit):
		requested.append(limit)
		clock[0] += 0.4
		return b"x"

	connection = Mock()
	connection.settimeout.side_effect = [None, None, None, OSError("closed connection")]
	def reject(status):
		connection.settimeout.assert_called_with(1)
		return status

	request = SimpleNamespace(
		server=SimpleNamespace(mode="broker"), path="/v1/messages",
		headers={"Authorization": "Bearer mine", "Content-Length": str(relay.MAX_BODY)},
		connection=connection, rfile=SimpleNamespace(read1=read_chunk), _reject=reject,
	)
	with monkeypatch.context() as patch:
		patch.setattr(relay.time, "monotonic", lambda: clock[0])
		assert relay.Relay.do_POST(request) == 400
	assert requested == [65536] * 3
	assert connection.settimeout.call_count == 4
	connection.settimeout.assert_called_with(1)


def test_rejected_body_drain_resets_timeout_after_read_error(monkeypatch) -> None:
	clock = [0.0]
	connection = Mock()

	def stalled_read(_limit):
		clock[0] = 0.999
		raise socket.timeout("timed out")

	def reject(status):
		connection.settimeout.assert_called_with(1)
		return status

	request = SimpleNamespace(
		server=SimpleNamespace(mode="broker"), path="/v1/messages",
		headers={"Authorization": "Bearer mine", "Content-Length": "4"},
		connection=connection, rfile=SimpleNamespace(read1=stalled_read), _reject=reject,
	)
	with monkeypatch.context() as patch:
		patch.setattr(relay.time, "monotonic", lambda: clock[0])
		assert relay.Relay.do_POST(request) == 400
	assert connection.settimeout.call_count == 2


def test_upstream_failure_is_a_bare_502(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	def refused(host, timeout=None, context=None):
		return http.client.HTTPConnection("127.0.0.1", 9, timeout=2)

	monkeypatch.setattr(http.client, "HTTPSConnection", refused)
	sock = str(tmp_path / "s.sock")
	broker = relay.UnixHTTPServer(sock, relay.Relay)
	broker.mode = "broker"
	broker.token = REAL_TOKEN
	broker.models = (MODEL,)
	_serve(broker)
	try:
		connection = relay.UnixHTTPConnection(sock)
		connection.request("POST", "/v1/messages", json.dumps({"model": MODEL}).encode(), {"Content-Type": "application/json"})
		response = connection.getresponse()
		body = response.read()
		assert response.status == 502
		assert REAL_TOKEN.encode() not in body
	finally:
		broker.shutdown()
		broker.server_close()


def test_read_token_strips_whitespace(tmp_path: Path) -> None:
	path = tmp_path / "t"
	path.write_text("ab c\nd\t", encoding="utf-8")
	os.chmod(path, 0o600)
	assert relay.read_token(str(path)) == "abcd"


def test_read_token_refuses_shared_symlinked_or_empty_files(tmp_path: Path) -> None:
	shared = tmp_path / "shared"
	shared.write_text("x", encoding="utf-8")
	os.chmod(shared, 0o644)
	with pytest.raises(ValueError):
		relay.read_token(str(shared))
	empty = tmp_path / "empty"
	empty.write_text("  \n", encoding="utf-8")
	os.chmod(empty, 0o600)
	with pytest.raises(ValueError):
		relay.read_token(str(empty))
	target = tmp_path / "target"
	target.write_text("x", encoding="utf-8")
	os.chmod(target, 0o600)
	link = tmp_path / "link"
	link.symlink_to(target)
	with pytest.raises(OSError):
		relay.read_token(str(link))


def test_oauth_beta_is_added_once() -> None:
	assert relay.with_oauth_beta("") == "oauth-2025-04-20"
	assert relay.with_oauth_beta("a, oauth-2025-04-20") == "a,oauth-2025-04-20"


def test_main_refuses_bad_arguments(tmp_path: Path) -> None:
	import subprocess
	import sys

	def run(*args: str) -> int:
		return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, check=False, timeout=10).returncode

	assert run() == 2
	assert run("proxy", "x") == 2
	assert run("broker", str(tmp_path / "s"), str(tmp_path / "absent"), MODEL) == 2
	token = tmp_path / "tok"
	token.write_text("t", encoding="utf-8")
	os.chmod(token, 0o600)
	assert run("broker", str(tmp_path / "s"), str(token), "gpt-5") == 2
	assert run("bridge") == 2
