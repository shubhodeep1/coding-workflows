#!/usr/bin/env python3
"""Restricted Anthropic Messages API relay: host Unix socket broker / container loopback bridge.

Follows scripts/clarify_openrouter_broker.py. The sandboxed Claude Code CLI
(clarify, the review editor) runs with ``--network none``, a dummy OAuth token
and ``ANTHROPIC_BASE_URL=http://127.0.0.1:8765``:

  * ``bridge <socket>`` runs inside the container. It accepts only the dummy
    bearer on loopback, drops it, and forwards the request over the
    bind-mounted Unix socket.
  * ``broker <socket> <token_file> <model>[,<model>…]`` runs on the host. It
    alone reads the real OAuth token (from a 0600 file, never from the
    environment or argv), accepts no client credential, checks the requested
    model, and forwards to https://api.anthropic.com.

Neither side accepts a destination URL from the client. Only POST
``/v1/messages`` and ``/v1/messages/count_tokens`` (optionally
``?beta=true``) cross the boundary. Request and response headers pass through
fixed allow lists. Never log requests, upstream bodies, headers or the token.
"""

import http.client
import http.server
import json
import os
import re
import socket
import socketserver
import ssl
import stat
import sys
import time

MAX_BODY = 32 * 1024 * 1024
UPSTREAM_HOST = "api.anthropic.com"
PLACEHOLDER = "isolated-placeholder"
PATH_RE = re.compile(r"^/v1/messages(?:/count_tokens)?(?:\?beta=true)?$")
MODEL_RE = re.compile(r"^claude-[a-z0-9][a-z0-9.-]{0,72}$")
# The CLI sends these; none of them carries a credential.
FORWARD_REQUEST_HEADERS = ("content-type", "accept", "anthropic-version", "anthropic-beta", "user-agent", "x-app")
FORWARD_REQUEST_PREFIXES = ("x-stainless-",)
# The CLI reads the unified rate-limit headers for its rate_limit_event.
FORWARD_RESPONSE_HEADERS = ("content-type", "request-id", "retry-after", "x-should-retry")
FORWARD_RESPONSE_PREFIXES = ("anthropic-ratelimit-",)
OAUTH_BETA = "oauth-2025-04-20"
MAX_HEADER_VALUE = 4096


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


def forwarded_request_headers(headers):
	"""The allow-listed request headers, lower-cased; None when one is malformed."""
	kept = {}
	for name, value in headers.items():
		key = name.lower()
		if key in FORWARD_REQUEST_HEADERS or key.startswith(FORWARD_REQUEST_PREFIXES):
			if len(value) > MAX_HEADER_VALUE or "\r" in value or "\n" in value:
				return None
			kept[key] = value
	return kept


def with_oauth_beta(value):
	"""``anthropic-beta`` with the OAuth beta flag present exactly once."""
	flags = [flag.strip() for flag in (value or "").split(",") if flag.strip()]
	if OAUTH_BETA not in flags:
		flags.append(OAUTH_BETA)
	return ",".join(flags)


def read_token(path):
	"""The OAuth token from a regular, owner-only file, whitespace removed (spike S3)."""
	fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
	with os.fdopen(fd, "r", encoding="utf-8") as handle:
		info = os.fstat(handle.fileno())
		if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
			raise ValueError("token file must be a regular 0600 file")
		token = re.sub(r"\s+", "", handle.read(16384))
	if not token:
		raise ValueError("token file is empty")
	return token


class Relay(http.server.BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.0"

	def log_message(self, *_args):
		pass

	def _reject(self, status):
		try:
			self.connection.settimeout(1)
		except OSError:
			pass
		try:
			self.send_error(status, "Request rejected")
		except (ConnectionError, TimeoutError):
			pass  # The rejection is terminal even when its response cannot be delivered.
		self.close_connection = True

	def do_POST(self):
		# No alternate paths, chunked uploads, client-selected hosts, API keys,
		# or (on the broker) any client authorization cross the boundary.
		mode = self.server.mode
		length = self.headers.get("Content-Length", "")
		headers = forwarded_request_headers(self.headers)
		if (
			not PATH_RE.match(self.path)
			or self.headers.get("Transfer-Encoding")
			or (
				self.headers.get("Authorization") != "Bearer " + PLACEHOLDER
				if mode == "bridge"
				else self.headers.get("Authorization") is not None
			)
			or self.headers.get("X-Api-Key") is not None
			or self.headers.get("Proxy-Authorization")
			or self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost")
			or self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json"
			or not length.isascii()
			or not length.isdecimal()
			or len(length) > 10
			or not 0 < int(length) <= MAX_BODY
			or headers is None
		):
			# Consume a bounded, declared body before closing so a rejected
			# client still sending it can receive the 400 instead of EPIPE.
			if len(length) <= 8 and length.isascii() and length.isdecimal() and 0 < int(length) <= MAX_BODY:
				drain_deadline = time.monotonic() + 1
				drain_remaining = int(length)
				try:
					while drain_remaining and (drain_wait := drain_deadline - time.monotonic()) > 0:
						self.connection.settimeout(drain_wait)
						drain_chunk = self.rfile.read1(min(drain_remaining, 65536))
						if not drain_chunk:
							break
						drain_remaining -= len(drain_chunk)
				except OSError:
					pass
				# Give the rejection write its own bounded timeout even if draining failed.
				try:
					self.connection.settimeout(1)
				except OSError:
					pass
			return self._reject(400)
		body = self.rfile.read(int(length))
		if len(body) != int(length):
			return self._reject(400)
		if mode == "broker":
			try:
				request = json.loads(body)
			except (UnicodeError, ValueError):
				return self._reject(400)
			if not isinstance(request, dict) or request.get("model") not in self.server.models:
				return self._reject(400)
			headers["anthropic-beta"] = with_oauth_beta(headers.get("anthropic-beta"))
			headers["authorization"] = "Bearer " + self.server.token
		connection = None
		headers_sent = False
		try:
			if mode == "broker":
				connection = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=600, context=ssl.create_default_context())
			else:
				connection = UnixHTTPConnection(self.server.socket_path)
			connection.request("POST", self.path, body, headers)
			response = connection.getresponse()
			self.send_response_only(response.status)
			for name, value in response.getheaders():
				key = name.lower()
				if key in FORWARD_RESPONSE_HEADERS or key.startswith(FORWARD_RESPONSE_PREFIXES):
					self.send_header(name, value)
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
				self._reject(502)
		finally:
			if connection is not None:
				connection.close()

	def do_GET(self):
		self._reject(405)

	def do_CONNECT(self):
		self._reject(405)


def main():
	if len(sys.argv) < 2 or sys.argv[1] not in ("broker", "bridge"):
		raise SystemExit(2)
	if sys.argv[1] == "broker":
		if len(sys.argv) != 5:
			raise SystemExit(2)
		models = tuple(model for model in sys.argv[4].split(",") if model)
		if not models or not all(MODEL_RE.match(model) for model in models):
			raise SystemExit(2)
		try:
			token = read_token(sys.argv[3])
		except (OSError, ValueError):
			raise SystemExit(2) from None
		server = UnixHTTPServer(sys.argv[2], Relay)
		server.mode = "broker"
		server.token = token
		server.models = models
		os.chmod(sys.argv[2], 0o600)  # The container uses the host UID to connect.
	else:
		if len(sys.argv) != 3:
			raise SystemExit(2)
		server = http.server.HTTPServer(("127.0.0.1", 8765), Relay)
		server.mode = "bridge"
		server.socket_path = sys.argv[2]
	with server:
		server.serve_forever()


if __name__ == "__main__":
	main()
