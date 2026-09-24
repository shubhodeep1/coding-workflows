#!/usr/bin/env python3
"""Restricted Responses API relay: host Unix socket broker / container loopback bridge.

The host process alone holds the provider credential. Neither side accepts a
destination URL from the client. Never log requests, upstream bodies or headers.
"""

import http.client
import http.server
import json
import os
import socket
import socketserver
import ssl
import sys

MAX_BODY = 8 * 1024 * 1024
PROVIDER_PATH = "/api/v1/responses"


class UnixHTTPConnection(http.client.HTTPConnection):
	def __init__(self, socket_path):
		super().__init__("localhost", timeout=600)
		self.socket_path = socket_path

	def connect(self):
		self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		self.sock.settimeout(600)
		self.sock.connect(self.socket_path)


class UnixHTTPServer(socketserver.UnixStreamServer):
	allow_reuse_address = True


class Relay(http.server.BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.0"

	def log_message(self, *_args):
		pass

	def _reject(self, status):
		self.send_error(status, "Request rejected")
		self.close_connection = True

	def do_POST(self):
		# No alternate paths, query strings, chunked uploads, client-selected
		# hosts, or user-supplied authorization headers cross the boundary.
		length = self.headers.get("Content-Length", "")
		if (
			self.path != PROVIDER_PATH
			or self.headers.get("Transfer-Encoding")
			or (
				self.headers.get("Authorization") != "Bearer isolated-placeholder"
				if self.server.mode == "bridge"
				else self.headers.get("Authorization") is not None
			)
			or self.headers.get("Proxy-Authorization")
			or self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost")
			or self.headers.get("Content-Type", "").lower() != "application/json"
			or not length.isascii()
			or not length.isdecimal()
			or not 0 < int(length) <= MAX_BODY
		):
			return self._reject(400)
		body = self.rfile.read(int(length))
		if len(body) != int(length):
			return self._reject(400)
		if self.server.mode == "broker":
			try:
				request = json.loads(body)
			except (UnicodeError, ValueError):
				return self._reject(400)
			if not isinstance(request, dict) or request.get("model") != self.server.model:
				return self._reject(400)
		connection = None
		headers_sent = False
		try:
			if self.server.mode == "broker":
				connection = http.client.HTTPSConnection("openrouter.ai", timeout=600, context=ssl.create_default_context())
				connection.request("POST", PROVIDER_PATH, body, {
					"Content-Type": "application/json",
					"Authorization": "Bearer " + self.server.api_key,
				})
			else:
				connection = UnixHTTPConnection(self.server.socket_path)
				connection.request("POST", PROVIDER_PATH, body, {"Content-Type": "application/json"})
			response = connection.getresponse()
			self.send_response_only(response.status)
			self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
			self.send_header("Connection", "close")
			self.end_headers()
			headers_sent = True
			# read1 emits each SSE chunk promptly instead of waiting for 64 KiB.
			while chunk := response.read1(65536):
				self.wfile.write(chunk)
				self.wfile.flush()
		except (OSError, http.client.HTTPException):
			# Do not echo upstream diagnostics: they can include provider data.
			if not headers_sent and not self.wfile.closed:
				try:
					self._reject(502)
				except OSError:
					pass
		finally:
			if connection is not None:
				connection.close()

	def do_GET(self):
		self._reject(405)

	def do_CONNECT(self):
		self._reject(405)


def main():
	if len(sys.argv) != 3 or sys.argv[1] not in ("broker", "bridge"):
		raise SystemExit(2)
	if sys.argv[1] == "broker":
		key = os.environ.get("OPENROUTER_API_KEY", "")
		model = os.environ.get("CLARIFY_MODEL", "openai/gpt-6-sol")
		if not key or "\n" in key or "\r" in key or not model:
			raise SystemExit(2)
		server = UnixHTTPServer(sys.argv[2], Relay)
		server.mode = "broker"
		server.api_key = key
		server.model = model
		os.chmod(sys.argv[2], 0o600)  # The container uses the host UID to connect.
	else:
		server = http.server.HTTPServer(("127.0.0.1", 8765), Relay)
		server.mode = "bridge"
		server.socket_path = sys.argv[2]
	with server:
		server.serve_forever()


if __name__ == "__main__":
	main()
