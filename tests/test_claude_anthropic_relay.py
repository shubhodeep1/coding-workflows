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
import threading
from pathlib import Path

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


def test_broker_rejects_a_model_outside_the_allow_list(chain) -> None:
	status, _, _ = _post(chain["bridge_port"], body={"model": "claude-other-9", "messages": []})
	assert status == 400
	assert _Upstream.seen == []


def test_broker_rejects_client_authorization(chain) -> None:
	# A caller on the socket cannot bring its own credential.
	connection = relay.UnixHTTPConnection(chain["socket"])
	connection.timeout = 5
	body = json.dumps({"model": MODEL}).encode()
	# The broker rejects these headers before reading the body. Sending it
	# races the broker's close and can raise BrokenPipeError on the client.
	connection.putrequest("POST", "/v1/messages")
	connection.putheader("Content-Type", "application/json")
	connection.putheader("Authorization", "Bearer mine")
	connection.putheader("Content-Length", str(len(body)))
	connection.endheaders()
	connection.sock.settimeout(5)
	assert connection.getresponse().status == 400
	connection.close()
	assert _Upstream.seen == []


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
