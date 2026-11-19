#!/usr/bin/env python3
"""Restricted HTTPS CONNECT tunnel for network-isolated dependency installs.

The container bridge has no policy and no host network. Only the host broker
resolves names, vets every address and opens outbound connections.
"""

import ipaddress
import os
import re
import select
import socket
import socketserver
import sys
import threading
import time

DEFAULT_ALLOWED_HOSTS = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "registry.yarnpkg.com")
CONNECT_HEAD_MAX = 8192
HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
CONNECT_RE = re.compile(rb"^CONNECT ([A-Za-z0-9.-]+):([0-9]+) HTTP/1\.[01]$")


def sanitize_for_log(text: str) -> str:
	return re.sub(r"[^A-Za-z0-9._:-]", "_", text[:120])


def parse_allowlist(raw: str) -> list[str]:
	allowed = []
	for entry in re.split(r"[,\s]+", raw.strip()):
		if not entry:
			continue
		host = entry.lower().removesuffix(".")
		try:
			ipaddress.ip_address(host)
		except ValueError:
			ip_literal = False
		else:
			ip_literal = True
		if ip_literal or not HOST_RE.fullmatch(host) or len(allowed) >= 50:
			print(f"DEPENDENCY_PROXY outcome=allowlist_entry_rejected entry={sanitize_for_log(entry)}", file=sys.stderr, flush=True)
			continue
		if host not in allowed:
			allowed.append(host)
	if not allowed:
		print("DEPENDENCY_PROXY outcome=allowlist_default_fallback", file=sys.stderr, flush=True)
		return list(DEFAULT_ALLOWED_HOSTS)
	return allowed


def resolve_global(host: str) -> list[tuple]:
	addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
	if not addresses:
		raise ValueError("non_global_address")
	for family, kind, protocol, canonical, address in addresses:
		if family not in (socket.AF_INET, socket.AF_INET6) or not ipaddress.ip_address(address[0]).is_global or ipaddress.ip_address(address[0]).is_multicast:
			raise ValueError("non_global_address")
	return addresses


def connect_global(addresses: list[tuple]) -> socket.socket:
	for family, kind, protocol, canonical, address in addresses:
		upstream = None
		try:
			upstream = socket.socket(family, kind, protocol)
			upstream.settimeout(30)
			upstream.connect(address)  # Use the already-vetted IP; never resolve again.
			return upstream
		except OSError:
			if upstream is not None:
				upstream.close()
	raise OSError("connection failed")


def splice(left: socket.socket, right: socket.socket) -> None:
	peers = {left: right, right: left}
	while peers:
		ready, _, _ = select.select(list(peers), [], [], 120)
		if not ready:
			return
		for source in ready:
			data = source.recv(65536)
			if not data:
				other = peers.pop(source)
				try:
					other.shutdown(socket.SHUT_WR)
				except OSError:
					pass
				continue
			peers[source].sendall(data)


class BrokerHandler(socketserver.BaseRequestHandler):
	def handle(self) -> None:
		self.handle_tunnel()

	def refuse(self, status: int, reason: str, host: str = "", port: str = "") -> None:
		print(f"DEPENDENCY_PROXY outcome=refused host={sanitize_for_log(host)} port={sanitize_for_log(port)} reason={reason}", file=sys.stderr, flush=True)
		try:
			self.request.sendall(f"HTTP/1.1 {status} Rejected\r\nConnection: close\r\n\r\n".encode("ascii"))
		except OSError:
			pass

	def handle_tunnel(self) -> None:
		deadline = time.monotonic() + 15
		head = bytearray()
		try:
			while not head.endswith(b"\r\n\r\n") and len(head) <= CONNECT_HEAD_MAX:
				self.request.settimeout(max(0.001, deadline - time.monotonic()))
				if time.monotonic() >= deadline:
					break
				chunk = self.request.recv(1)
				if not chunk:
					if not head:
						return
					break
				head.extend(chunk)
		except (OSError, TimeoutError):
			self.refuse(400, "malformed")
			return
		if len(head) > CONNECT_HEAD_MAX or not head.endswith(b"\r\n\r\n"):
			self.refuse(400, "malformed")
			return
		request_line = bytes(head).split(b"\r\n", 1)[0]
		if not request_line.startswith(b"CONNECT "):
			self.refuse(405, "method")
			return
		match = CONNECT_RE.fullmatch(request_line)
		if match is None:
			self.refuse(400, "malformed")
			return
		host = match.group(1).decode("ascii").lower().removesuffix(".")
		port = match.group(2).decode("ascii")
		if port != "443":
			self.refuse(403, "port", host, port)
			return
		if not HOST_RE.fullmatch(host) or host not in self.server.allowed:
			self.refuse(403, "host_not_allowed", host, port)
			return
		try:
			addresses = resolve_global(host)
		except ValueError:
			self.refuse(403, "non_global_address", host, port)
			return
		except OSError:
			self.refuse(502, "upstream", host, port)
			return
		established = False
		try:
			with connect_global(addresses) as upstream:
				with self.server.log_lock:
					if host not in self.server.logged_hosts:
						self.server.logged_hosts.add(host)
						print(f"DEPENDENCY_PROXY outcome=allowed host={sanitize_for_log(host)}", file=sys.stderr, flush=True)
				self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
				established = True
				self.request.settimeout(None)
				upstream.settimeout(None)
				splice(self.request, upstream)
		except OSError:
			if not established:
				self.refuse(502, "upstream", host, port)


class BridgeHandler(socketserver.BaseRequestHandler):
	def handle(self) -> None:
		connected = False
		try:
			with socket.socket(socket.AF_UNIX) as broker:
				broker.settimeout(15)
				broker.connect(self.server.broker_path)
				connected = True
				broker.settimeout(None)
				splice(self.request, broker)
		except OSError:
			if not connected:
				print("DEPENDENCY_PROXY outcome=refused reason=broker_unavailable", file=sys.stderr, flush=True)
				try:
					self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
				except OSError:
					pass


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
	daemon_threads = True
	block_on_close = False

	def process_request(self, request: socket.socket, client_address: str) -> None:
		if not self.slots.acquire(blocking=False):
			print("DEPENDENCY_PROXY outcome=refused host= port= reason=busy", file=sys.stderr, flush=True)
			try:
				request.settimeout(1)
				request.sendall(b"HTTP/1.1 503 Rejected\r\nConnection: close\r\n\r\n")
			except OSError:
				pass
			finally:
				self.shutdown_request(request)
			return
		try:
			super().process_request(request, client_address)
		except BaseException:
			self.slots.release()
			raise

	def process_request_thread(self, request: socket.socket, client_address: str) -> None:
		try:
			super().process_request_thread(request, client_address)
		finally:
			self.slots.release()


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
	daemon_threads = True
	block_on_close = False
	allow_reuse_address = True


def main() -> int:
	if len(sys.argv) != 4 or sys.argv[1] not in ("broker", "bridge"):
		return 2
	mode, path, arg = sys.argv[1:]
	if mode == "broker":
		if not os.path.isdir(os.path.dirname(path)) or os.path.lexists(path):
			return 2
		with ThreadingUnixServer(path, BrokerHandler) as server:
			os.chmod(path, 0o600)
			server.allowed = set(parse_allowlist(arg))
			server.slots = threading.BoundedSemaphore(64)
			server.logged_hosts = set()
			server.log_lock = threading.Lock()
			server.serve_forever()
	else:
		try:
			port = int(arg)
		except ValueError:
			return 2
		if not 1 <= port <= 65535:
			return 2
		with ThreadingTCPServer(("127.0.0.1", port), BridgeHandler) as server:
			server.broker_path = path
			server.serve_forever()
	return 0


if __name__ == "__main__":
	sys.exit(main())
