#!/usr/bin/env python3
"""scripts/clarify_openrouter_broker.py: the container-side bridge modes.

The sandbox containers run this file as `/bridge.py bridge|review-bridge
/socket/provider.sock` (clarify, heal editor, OpenCode review) and as
`/support/clarify_openrouter_broker.py bridge` (codex-isolated). The bridge
listens on 127.0.0.1 and forwards one fixed path to the host broker's Unix
socket. These tests replace the host broker with a fake Unix-socket server, so
they need no network and no credential, and they run both on CI's pinned
Python and inside the sandbox images (`tests-sandbox-python`, #9233).
"""

from __future__ import annotations

import errno
import http.client
import http.server
import importlib.util
import json
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "clarify_openrouter_broker.py"
FIXED_BRIDGE_PORT = 8765


def _load():
	spec = importlib.util.spec_from_file_location("clarify_openrouter_broker", SCRIPT)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


broker = _load()

MODE_PATHS = {"bridge": broker.PROVIDER_PATH, "review-bridge": broker.REVIEW_PATH}


class _FakeHostBroker(http.server.BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.0"
	seen: list[dict] = []

	def log_message(self, *_args):
		pass

	def do_POST(self):
		body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
		_FakeHostBroker.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
		payload = b'data: {"ok": true}\n\n'
		self.send_response(200)
		self.send_header("Content-Type", "text/event-stream")
		self.send_header("Content-Length", str(len(payload)))
		self.end_headers()
		self.wfile.write(payload)


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
	daemon_threads = True


def _serve(server) -> threading.Thread:
	thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
	thread.start()
	return thread


@pytest.fixture()
def host_socket(tmp_path: Path):
	_FakeHostBroker.seen = []
	path = str(tmp_path / "provider.sock")
	server = _UnixServer(path, _FakeHostBroker)
	_serve(server)
	yield path
	server.shutdown()
	server.server_close()


@pytest.fixture(params=sorted(MODE_PATHS))
def bridge(request, host_socket: str):
	server = http.server.HTTPServer(("127.0.0.1", 0), broker.Relay)
	server.mode = request.param
	server.socket_path = host_socket
	_serve(server)
	yield {"mode": request.param, "port": server.server_address[1], "path": MODE_PATHS[request.param]}
	server.shutdown()
	server.server_close()


def _post(port: int, path: str, *, headers: dict | None = None, body: bytes | None = None, method: str = "POST"):
	sent = {
		"Content-Type": "application/json",
		"Authorization": "Bearer isolated-placeholder",
	}
	sent.update(headers or {})
	sent = {key: value for key, value in sent.items() if value is not None}
	data = body if body is not None else json.dumps({"model": "m", "input": "hi"}).encode()
	connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
	try:
		connection.request(method, path, data if method == "POST" else None, sent)
		response = connection.getresponse()
		return response.status, response.read()
	finally:
		connection.close()


def test_bridge_forwards_the_mode_path_without_the_placeholder(bridge) -> None:
	status, payload = _post(bridge["port"], bridge["path"])
	assert status == 200
	assert payload == b'data: {"ok": true}\n\n'
	assert len(_FakeHostBroker.seen) == 1
	seen = _FakeHostBroker.seen[0]
	assert seen["path"] == bridge["path"]
	assert "authorization" not in seen["headers"]
	assert json.loads(seen["body"]) == {"model": "m", "input": "hi"}


def test_bridge_rejects_the_other_modes_path(bridge) -> None:
	other = next(path for mode, path in MODE_PATHS.items() if mode != bridge["mode"])
	status, _ = _post(bridge["port"], other)
	assert status == 400
	assert _FakeHostBroker.seen == []


@pytest.mark.parametrize(
	"kwargs",
	[
		{"headers": {"Host": "evil.example"}},
		{"headers": {"Authorization": "Bearer real-key"}},
		{"headers": {"Authorization": None}},
		{"headers": {"Proxy-Authorization": "Basic x"}},
		{"headers": {"Content-Type": "text/plain"}},
		{"suffix": "?x=1"},
		{"body": b""},
	],
)
def test_bridge_rejects_requests_outside_the_contract(bridge, kwargs: dict) -> None:
	path = bridge["path"] + kwargs.get("suffix", "")
	status, _ = _post(bridge["port"], path, headers=kwargs.get("headers"), body=kwargs.get("body"))
	assert status == 400
	assert _FakeHostBroker.seen == []


def test_bridge_rejects_get(bridge) -> None:
	status, _ = _post(bridge["port"], bridge["path"], method="GET")
	assert status == 405


def test_bridge_reports_a_missing_host_socket_as_502(tmp_path: Path) -> None:
	server = http.server.HTTPServer(("127.0.0.1", 0), broker.Relay)
	server.mode = "bridge"
	server.socket_path = str(tmp_path / "absent.sock")
	_serve(server)
	try:
		status, payload = _post(server.server_address[1], broker.PROVIDER_PATH)
	finally:
		server.shutdown()
		server.server_close()
	assert status == 502
	assert b"absent.sock" not in payload


def test_main_refuses_bad_arguments() -> None:
	for args in ([], ["bridge"], ["proxy", "/tmp/x"], ["bridge", "a", "b"]):
		completed = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, check=False, timeout=10)
		assert completed.returncode == 2, args


def _fixed_port_free() -> bool:
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
		try:
			probe.bind(("127.0.0.1", FIXED_BRIDGE_PORT))
		except OSError as error:
			if error.errno in (errno.EADDRINUSE, errno.EACCES):
				return False
			raise
	return True


@pytest.mark.parametrize("mode", sorted(MODE_PATHS))
def test_entry_point_serves_the_fixed_loopback_port(host_socket: str, mode: str) -> None:
	"""Run the file the way the containers do: `python3 /bridge.py <mode> <socket>`."""
	if not _fixed_port_free():
		pytest.skip(f"127.0.0.1:{FIXED_BRIDGE_PORT} is already bound; the bridge entry point listens only there")
	process = subprocess.Popen(
		[sys.executable, str(SCRIPT), mode, host_socket],
		stdout=subprocess.DEVNULL,
		stderr=subprocess.PIPE,
	)
	try:
		deadline = time.monotonic() + 10
		while True:
			try:
				with socket.create_connection(("127.0.0.1", FIXED_BRIDGE_PORT), timeout=1):
					break
			except OSError:
				if process.poll() is not None or time.monotonic() > deadline:
					raise AssertionError(f"bridge did not start: {process.stderr.read().decode(errors='replace') if process.poll() is not None else 'timeout'}")
				time.sleep(0.05)
		status, _ = _post(FIXED_BRIDGE_PORT, MODE_PATHS[mode])
		assert status == 200
		assert [seen["path"] for seen in _FakeHostBroker.seen] == [MODE_PATHS[mode]]
	finally:
		process.terminate()
		try:
			process.wait(timeout=5)
		except subprocess.TimeoutExpired:
			process.kill()
			process.wait(timeout=5)
		if process.stderr is not None:
			process.stderr.close()
