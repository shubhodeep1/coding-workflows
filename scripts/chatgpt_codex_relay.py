#!/usr/bin/env python3
"""Restricted ChatGPT Codex relay: host Unix socket broker / container loopback bridge.

The ChatGPT account review-panel slot (trial plan Phase 2, Refs #8969) runs
the Codex CLI in the ``--network none`` review sandbox
(scripts/review_untrusted_sandbox.sh, engine ``chatgpt``) with a placeholder
ChatGPT-mode ``auth.json`` and a custom provider whose base URL is
``http://127.0.0.1:8765/backend-api/codex``. A sibling of
scripts/claude_anthropic_relay.py, which pins the Anthropic host by contract:

  * ``bridge <socket>`` runs inside the container on 127.0.0.1:8765. It
    accepts only the placeholder bearer, drops it, and forwards the request
    over the bind-mounted Unix socket.
  * ``broker <socket> <codex_home_dir> <model>`` runs on the host. It alone
    reads the real access token and account id from ``<dir>/auth.json`` (a
    regular 0600 file, opened without following symlinks; never from the
    environment or argv), accepts no client credential, checks the requested
    model and tool types, and forwards to https://chatgpt.com.
  * ``placeholder-auth <out>`` writes the container's 0600 ``auth.json``:
    placeholder tokens and a synthetic unsigned ``id_token`` carrying only
    the claims Codex decodes. It holds no credential.

Neither side accepts a destination URL from the client. Only POST
``/backend-api/codex/responses`` crosses the boundary; every other method or
path is refused. Request and response headers pass through fixed allow
lists. Only client-executed tools (``function``, ``custom``, ``local_shell``)
are allowed: web search, MCP, code interpreter, file search, image
generation and computer use are refused. A compressed (``Content-Encoding``)
or chunked body is refused, so the model check cannot be bypassed. Never log
requests, upstream bodies, headers or the token.
"""

import base64
import datetime
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
MAX_AUTH_FILE = 65536
BODY_READ_TIMEOUT = 60
UPSTREAM_HOST = "chatgpt.com"
UPSTREAM_PATH = "/backend-api/codex/responses"
PLACEHOLDER = "isolated-placeholder"
PLACEHOLDER_ACCOUNT = "isolated-placeholder-account"
PLACEHOLDER_REFRESH = "isolated-placeholder-refresh"
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,8192}$")
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# Codex sends these; none of them carries a credential.
FORWARD_REQUEST_HEADERS = (
	"content-type", "accept", "openai-beta", "originator", "version", "session_id", "conversation_id", "user-agent",
)
FORWARD_RESPONSE_HEADERS = ("content-type", "retry-after", "x-request-id")
# Codex reads its usage windows from these.
FORWARD_RESPONSE_PREFIXES = ("x-codex-",)
ALLOWED_TOOL_TYPES = ("function", "custom", "local_shell")
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
		if key in FORWARD_REQUEST_HEADERS:
			if len(value) > MAX_HEADER_VALUE or "\r" in value or "\n" in value:
				return None
			kept[key] = value
	return kept


def request_has_forbidden_tools(request):
	"""True when a tool is not client-executed (or the tool list is malformed)."""
	if "tools" not in request:
		return False
	tools = request["tools"]
	if not isinstance(tools, list):
		return True
	for tool in tools:
		if not isinstance(tool, dict) or tool.get("type") not in ALLOWED_TOOL_TYPES:
			return True
	return False


def read_credentials(codex_home):
	"""(access_token, account_id) from ``<codex_home>/auth.json``: regular, owner-only, no symlink."""
	fd = os.open(os.path.join(codex_home, "auth.json"), os.O_RDONLY | os.O_NOFOLLOW)
	with os.fdopen(fd, "r", encoding="utf-8") as handle:
		info = os.fstat(handle.fileno())
		if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
			raise ValueError("auth.json must be a regular 0600 file")
		auth = json.loads(handle.read(MAX_AUTH_FILE + 1)[:MAX_AUTH_FILE])
	tokens = auth.get("tokens") if isinstance(auth, dict) else None
	if not isinstance(tokens, dict):
		raise ValueError("auth.json has no tokens")
	access = tokens.get("access_token")
	account = tokens.get("account_id") or auth.get("account_id")
	if not isinstance(access, str) or not TOKEN_RE.fullmatch(access) or access == PLACEHOLDER:
		raise ValueError("auth.json has no access token")
	if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account):
		raise ValueError("auth.json has no account id")
	return access, account


def _b64url(data):
	return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def placeholder_auth(now=None):
	"""The container's ChatGPT-mode auth.json; it holds no credential."""
	now = now or datetime.datetime.now(datetime.timezone.utc)
	claims = {"https://api.openai.com/auth": {"chatgpt_account_id": PLACEHOLDER_ACCOUNT, "chatgpt_plan_type": "pro"}}
	id_token = ".".join((
		_b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode()),
		_b64url(json.dumps(claims).encode()),
		_b64url(b"placeholder"),
	))
	return {
		"OPENAI_API_KEY": None,
		"tokens": {
			"id_token": id_token,
			"access_token": PLACEHOLDER,
			"refresh_token": PLACEHOLDER_REFRESH,
			"account_id": PLACEHOLDER_ACCOUNT,
		},
		# Recent, so Codex does not try to refresh (it has no network anyway).
		"last_refresh": now.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
	}


def write_placeholder_auth(path):
	fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
	with os.fdopen(fd, "w", encoding="utf-8") as handle:
		json.dump(placeholder_auth(), handle)
	os.chmod(path, 0o600)


def _declared_length(length):
	return length.isascii() and length.isdecimal() and len(length) <= len(str(MAX_BODY)) and 0 < int(length) <= MAX_BODY


class Relay(http.server.BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.0"

	def log_message(self, *_args):
		pass

	def _drain(self, remaining):
		"""Read up to ``remaining`` declared body bytes within one second."""
		previous_timeout = self.connection.gettimeout()
		deadline = time.monotonic() + 1
		try:
			while remaining and (seconds_left := deadline - time.monotonic()) > 0:
				self.connection.settimeout(seconds_left)
				chunk = self.rfile.read1(min(65536, remaining))
				if not chunk:
					break
				remaining -= len(chunk)
		except OSError:
			pass
		finally:
			try:
				self.connection.settimeout(previous_timeout)
			except OSError:
				pass

	def _reject(self, status, unread=0):
		# Let a sending client finish so it receives the error instead of EPIPE.
		if unread:
			self._drain(unread)
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
		mode = self.server.mode
		length = self.headers.get("Content-Length", "")
		declared = int(length) if _declared_length(length) else 0
		headers = forwarded_request_headers(self.headers)
		if (
			self.path != UPSTREAM_PATH
			or self.headers.get("Transfer-Encoding")
			or self.headers.get("Content-Encoding")
			or (
				self.headers.get("Authorization") != "Bearer " + PLACEHOLDER
				if mode == "bridge"
				else self.headers.get("Authorization") is not None
			)
			or self.headers.get("X-Api-Key") is not None
			or self.headers.get("Proxy-Authorization")
			or self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost")
			or self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json"
			or not declared
			or headers is None
		):
			return self._reject(400, declared)
		previous_timeout = self.connection.gettimeout()
		deadline = time.monotonic() + BODY_READ_TIMEOUT
		body_parts = []
		body_remaining = declared
		try:
			while body_remaining and (seconds_left := deadline - time.monotonic()) > 0:
				self.connection.settimeout(seconds_left)
				chunk = self.rfile.read1(min(65536, body_remaining))
				if not chunk:
					break
				body_parts.append(chunk)
				body_remaining -= len(chunk)
		except OSError:
			pass
		finally:
			self.connection.settimeout(previous_timeout)
		if body_remaining:
			return self._reject(400, body_remaining)
		body = b"".join(body_parts)
		if mode == "broker":
			try:
				request = json.loads(body)
			except (UnicodeError, ValueError):
				return self._reject(400)
			if not isinstance(request, dict) or request.get("model") != self.server.model:
				return self._reject(400)
			if request_has_forbidden_tools(request):
				return self._reject(400)
			headers["authorization"] = "Bearer " + self.server.token
			headers["chatgpt-account-id"] = self.server.account
		connection = None
		headers_sent = False
		try:
			if mode == "broker":
				connection = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=600, context=ssl.create_default_context())
			else:
				connection = UnixHTTPConnection(self.server.socket_path)
			connection.request("POST", UPSTREAM_PATH, body, headers)
			response = connection.getresponse()
			self.send_response_only(response.status)
			for name, value in response.getheaders():
				key = name.lower()
				if key in FORWARD_RESPONSE_HEADERS or key.startswith(FORWARD_RESPONSE_PREFIXES):
					self.send_header(name, value)
			self.send_header("Connection", "close")
			self.end_headers()
			headers_sent = True
			# read1 emits each server-sent event chunk promptly.
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

	def do_PUT(self):
		self._reject(405)

	def do_DELETE(self):
		self._reject(405)

	def do_CONNECT(self):
		self._reject(405)


def main():
	if len(sys.argv) < 2 or sys.argv[1] not in ("broker", "bridge", "placeholder-auth"):
		raise SystemExit(2)
	if sys.argv[1] == "placeholder-auth":
		if len(sys.argv) != 3:
			raise SystemExit(2)
		try:
			write_placeholder_auth(sys.argv[2])
		except OSError:
			raise SystemExit(1) from None
		return
	if sys.argv[1] == "broker":
		if len(sys.argv) != 5 or not MODEL_RE.fullmatch(sys.argv[4]):
			raise SystemExit(2)
		try:
			token, account = read_credentials(sys.argv[3])
		except (OSError, ValueError):
			raise SystemExit(2) from None
		server = UnixHTTPServer(sys.argv[2], Relay)
		server.mode = "broker"
		server.token = token
		server.account = account
		server.model = sys.argv[4]
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
