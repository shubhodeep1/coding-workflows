"""Exercise the dependency broker without granting tests outbound network."""

import importlib.util
import ipaddress
import socket
import threading
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("dependency_registry_proxy", Path(__file__).resolve().parents[1] / "scripts/dependency_registry_proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


@pytest.fixture()
def broker(tmp_path):
	path = str(tmp_path / "registry.sock")
	server = proxy.ThreadingUnixServer(path, proxy.BrokerHandler)
	server.allowed = {"pypi.org"}
	server.slots = threading.BoundedSemaphore(64)
	server.logged_hosts = set()
	server.log_lock = threading.Lock()
	worker = threading.Thread(target=server.serve_forever, daemon=True)
	worker.start()
	yield path
	server.shutdown()
	server.server_close()
	worker.join(timeout=2)


def request(path, head):
	with socket.socket(socket.AF_UNIX) as client:
		client.settimeout(2)
		client.connect(path)
		client.sendall(head)
		return client.recv(512)


def test_allowlist_defaults_validation_and_cap(capfd):
	assert proxy.parse_allowlist("") == list(proxy.DEFAULT_ALLOWED_HOSTS)
	assert proxy.parse_allowlist("PyPI.org. registry.npmjs.org,pypi.org") == ["pypi.org", "registry.npmjs.org"]
	assert proxy.parse_allowlist("127.0.0.1,*.bad.org host:443 https://bad.org") == list(proxy.DEFAULT_ALLOWED_HOSTS)
	assert len(proxy.parse_allowlist(" ".join(f"host{n}.example.org" for n in range(51)))) == 50
	output = capfd.readouterr().err
	assert "outcome=allowlist_entry_rejected" in output
	assert "outcome=allowlist_default_fallback" in output
	assert "https://bad.org" not in output
	assert proxy.sanitize_for_log("\n::error::x") == "_::error::x"
	assert len(proxy.sanitize_for_log("a" * 200)) == 120


@pytest.mark.parametrize("head,status,reason", [
	(b"CONNECT other.example:443 HTTP/1.1\r\n\r\n", b"403", "host_not_allowed"),
	(b"CONNECT pypi.org:80 HTTP/1.1\r\n\r\n", b"403", "port"),
	(b"GET http://pypi.org/ HTTP/1.1\r\n\r\n", b"405", "method"),
	(b"CONNECT pypi.org:443 HTTP/1.1\r\nX: " + b"a" * 8192 + b"\r\n\r\n", b"400", "malformed"),
])
def test_broker_rejects_invalid_requests(broker, capfd, head, status, reason):
	assert status in request(broker, head)
	assert "reason=" + reason in capfd.readouterr().err


def test_empty_probe_does_not_log_a_refusal(broker, capfd):
	with socket.socket(socket.AF_UNIX) as client:
		client.settimeout(2)
		client.connect(broker)
		client.shutdown(socket.SHUT_WR)
		assert client.recv(512) == b""
	assert "outcome=refused" not in capfd.readouterr().err


def test_broker_limits_threads_before_accepting_a_handler(tmp_path, monkeypatch):
	started = threading.Event()
	release = threading.Event()
	handler_calls = []

	def hold_handler(self):
		handler_calls.append(self)
		started.set()
		release.wait(timeout=10)

	monkeypatch.setattr(proxy.BrokerHandler, "handle_tunnel", hold_handler)
	path = str(tmp_path / "limited.sock")
	with proxy.ThreadingUnixServer(path, proxy.BrokerHandler) as server:
		server.slots = threading.BoundedSemaphore(1)
		worker = threading.Thread(target=server.serve_forever, daemon=True)
		worker.start()
		try:
			with socket.socket(socket.AF_UNIX) as first:
				first.connect(path)
				assert started.wait(timeout=2)
				assert b"503" in request(path, b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
				assert len(handler_calls) == 1
		finally:
			release.set()
			server.shutdown()
			worker.join(timeout=2)


@pytest.mark.parametrize("addresses", [
	["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"],
	["93.184.216.34", "127.0.0.1"], ["224.0.0.1"],
])
def test_dns_rejects_non_global_even_when_one_address_is_public(monkeypatch, broker, capfd, addresses):
	monkeypatch.setattr(proxy.socket, "getaddrinfo", lambda *_args, **_kwargs: [
		(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 443)) for addr in addresses
	])
	assert b"403" in request(broker, b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
	assert "reason=non_global_address" in capfd.readouterr().err


def test_broker_splices_to_vetted_address_and_bridge_forwards(monkeypatch, broker, capfd):
	vetted = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
	monkeypatch.setattr(proxy, "resolve_global", lambda _host: [vetted])
	upstream, peer = socket.socketpair()
	def connect_global(addresses):
		assert addresses == [vetted]
		return upstream
	monkeypatch.setattr(proxy, "connect_global", connect_global)
	with proxy.ThreadingTCPServer(("127.0.0.1", 0), proxy.BridgeHandler) as bridge:
		bridge.broker_path = broker
		worker = threading.Thread(target=bridge.serve_forever, daemon=True)
		worker.start()
		try:
			with socket.create_connection(bridge.server_address, timeout=2) as client:
				client.settimeout(2)
				peer.settimeout(2)
				client.sendall(b"CONNECT PyPI.org.:443 HTTP/1.1\r\n\r\n")
				assert b"200 Connection established" in client.recv(512)
				client.sendall(b"client bytes")
				assert peer.recv(512) == b"client bytes"
				peer.sendall(b"server bytes")
				assert client.recv(512) == b"server bytes"
		finally:
			peer.close()
			bridge.shutdown()
			worker.join(timeout=2)
	assert capfd.readouterr().err.count("outcome=allowed host=pypi.org") == 1


def test_bridge_reports_broker_connection_failure(tmp_path, capfd):
	with proxy.ThreadingTCPServer(("127.0.0.1", 0), proxy.BridgeHandler) as bridge:
		bridge.broker_path = str(tmp_path / "missing.sock")
		worker = threading.Thread(target=bridge.serve_forever, daemon=True)
		worker.start()
		try:
			with socket.create_connection(bridge.server_address, timeout=2) as client:
				client.settimeout(2)
				client.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
				assert client.recv(512).startswith(b"HTTP/1.1 502 Bad Gateway\r\n")
		finally:
			bridge.shutdown()
			worker.join(timeout=2)
	assert "reason=broker_unavailable" in capfd.readouterr().err


def test_global_resolution_preserves_vetted_ip_tuple(monkeypatch):
	address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
	monkeypatch.setattr(proxy.socket, "getaddrinfo", lambda *_args, **_kwargs: [address])
	assert ipaddress.ip_address(address[-1][0]).is_global
	assert proxy.resolve_global("pypi.org") == [address]
