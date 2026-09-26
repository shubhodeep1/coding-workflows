#!/usr/bin/env python3
"""Allowlisted CONNECT proxy for isolated dependency installation."""

from __future__ import annotations

import argparse
import select
import socket
import socketserver


ALLOWED_CONNECT_HOSTS = frozenset({"files.pythonhosted.org", "pypi.org"})
MAX_PROXY_HEADER_BYTES = 16 * 1024
PROXY_IDLE_TIMEOUT_SECONDS = 300


def _parse_connect_target(authority: str) -> tuple[str, int] | None:
	host, separator, port_text = authority.rpartition(":")
	host = host.rstrip(".").lower()
	if separator != ":" or host not in ALLOWED_CONNECT_HOSTS or port_text != "443":
		return None
	return host, 443


class PackageDownloadProxyHandler(socketserver.BaseRequestHandler):
	def handle(self) -> None:
		request_header = bytearray()
		while b"\r\n\r\n" not in request_header:
			chunk = self.request.recv(4096)
			if not chunk:
				return
			request_header.extend(chunk)
			if len(request_header) > MAX_PROXY_HEADER_BYTES:
				self._reply(431, "Request Header Fields Too Large")
				return
		try:
			request_line = bytes(request_header).split(b"\r\n", 1)[0].decode("ascii")
			method, authority, protocol = request_line.split(" ", 2)
		except (UnicodeDecodeError, ValueError):
			self._reply(400, "Bad Request")
			return
		connect_target = _parse_connect_target(authority)
		if method != "CONNECT" or protocol not in {"HTTP/1.0", "HTTP/1.1"} or connect_target is None:
			self._reply(403, "Forbidden")
			return
		try:
			upstream_socket = socket.create_connection(connect_target, timeout=30)
		except OSError:
			self._reply(502, "Bad Gateway")
			return
		with upstream_socket:
			try:
				self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
				self._relay(upstream_socket)
			except OSError:
				return

	def _relay(self, upstream_socket: socket.socket) -> None:
		peers = (self.request, upstream_socket)
		while True:
			readable, _, _ = select.select(peers, (), (), PROXY_IDLE_TIMEOUT_SECONDS)
			if not readable:
				return
			for source_socket in readable:
				payload = source_socket.recv(64 * 1024)
				if not payload:
					return
				destination_socket = upstream_socket if source_socket is self.request else self.request
				destination_socket.sendall(payload)

	def _reply(self, status_code: int, reason: str) -> None:
		self.request.sendall(
			f"HTTP/1.1 {status_code} {reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode("ascii")
		)


class PackageDownloadProxyServer(socketserver.ThreadingTCPServer):
	allow_reuse_address = True
	daemon_threads = True


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--bind", default="0.0.0.0")
	parser.add_argument("--port", type=int, default=8080)
	arguments = parser.parse_args()
	with PackageDownloadProxyServer(
		(arguments.bind, arguments.port), PackageDownloadProxyHandler
	) as proxy_server:
		proxy_server.serve_forever(poll_interval=0.2)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
