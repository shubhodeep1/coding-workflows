#!/usr/bin/env python3
"""Restricted Anthropic Messages API relay: host Unix socket broker / container loopback bridge.

Follows scripts/clarify_openrouter_broker.py. The sandboxed Claude Code CLI
(clarify, the review editor) runs with ``--network none``, a dummy OAuth token
and ``ANTHROPIC_BASE_URL=http://127.0.0.1:8765``:

  * ``bridge <socket>`` runs inside the container. It accepts only the dummy
    bearer on loopback, drops it, and forwards the request over the
    bind-mounted Unix socket.
  * ``broker <socket> <token_file> <model>[,<model>…] [--allow-web-search]``
    runs on the host. It alone reads the real OAuth token (from a 0600 file,
    never from the environment or argv), accepts no client credential, checks
    the requested model, and forwards to https://api.anthropic.com.

Neither side accepts a destination URL from the client. Only POST
``/v1/messages`` and ``/v1/messages/count_tokens`` (optionally
``?beta=true``) cross the boundary. Request and response headers pass through
fixed allow lists. Only client-executed tools are allowed: the host broker
refuses provider-side web, code-execution and MCP connector tools. The one
exception is ``--allow-web-search``: the provider's web search tool, in
exactly the shape the pinned Claude Code CLI's WebSearch tool sends
(WEB_SEARCH_TOOL_TYPE, name ``web_search``, at most WEB_SEARCH_MAX_USES
searches, optional domain lists). Web fetch, code execution and MCP stay
refused either way. Never log requests, upstream bodies, headers or the token.
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
BODY_READ_TIMEOUT = 60
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
PROVIDER_EGRESS_TOOL_PREFIXES = ("web_search_", "web_fetch_", "code_execution_")
PROVIDER_EGRESS_TOOL_TYPES = ("mcp_toolset",)
PROVIDER_EGRESS_REQUEST_KEYS = ("mcp_servers", "container")
# The provider-side search the CLI's WebSearch tool sends (CLI 2.1.289:
# {type, name, allowed_domains?, blocked_domains?, max_uses: 8,
# search_profile?}). Accepted only when the broker runs with
# WEB_SEARCH_BROKER_FLAG. A newer tool version stays refused until reviewed.
WEB_SEARCH_BROKER_FLAG = "--allow-web-search"
WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
WEB_SEARCH_TOOL_NAME = "web_search"
WEB_SEARCH_TOOL_KEYS = frozenset(("type", "name", "allowed_domains", "blocked_domains", "max_uses", "search_profile"))
WEB_SEARCH_MAX_USES = 8
WEB_SEARCH_MAX_DOMAINS = 100
WEB_SEARCH_DOMAIN_RE = re.compile(r"^[A-Za-z0-9*][A-Za-z0-9.*:/_-]{0,252}$")
WEB_SEARCH_PROFILE_RE = re.compile(r"^[a-z][a-z_]{0,31}$")


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


def _is_allowed_web_search_tool(tool: dict) -> bool:
	"""True only for the exact provider web search tool shape the CLI sends."""
	if tool.get("type") != WEB_SEARCH_TOOL_TYPE or tool.get("name") != WEB_SEARCH_TOOL_NAME:
		return False
	if not set(tool) <= WEB_SEARCH_TOOL_KEYS:
		return False
	max_uses = tool.get("max_uses")
	if max_uses is not None and (type(max_uses) is not int or not 1 <= max_uses <= WEB_SEARCH_MAX_USES):
		return False
	for key in ("allowed_domains", "blocked_domains"):
		domains = tool.get(key)
		if domains is None:
			continue
		if not isinstance(domains, list) or len(domains) > WEB_SEARCH_MAX_DOMAINS \
			or not all(isinstance(domain, str) and WEB_SEARCH_DOMAIN_RE.match(domain) for domain in domains):
			return False
	profile = tool.get("search_profile")
	if profile is not None and not (isinstance(profile, str) and WEB_SEARCH_PROFILE_RE.match(profile)):
		return False
	return True


def request_has_provider_egress(request: dict, allow_web_search: bool = False) -> bool:
	"""Reject provider-executed tools at the host trust boundary.

	With ``allow_web_search`` the provider web search tool passes when it has
	exactly the shape the CLI sends (``_is_allowed_web_search_tool``); every
	other provider-executed tool is still refused.
	"""
	if any(key in request for key in PROVIDER_EGRESS_REQUEST_KEYS):
		return True
	if "tools" not in request:
		return False
	tools = request["tools"]
	if not isinstance(tools, list):
		return True
	for tool in tools:
		if not isinstance(tool, dict):
			return True
		if "type" in tool:
			tool_type = tool["type"]
			if allow_web_search and _is_allowed_web_search_tool(tool):
				continue
			# Unknown typed tools may execute on the provider; only custom tools run in the client.
			if (
				not isinstance(tool_type, str)
				or tool_type.startswith(PROVIDER_EGRESS_TOOL_PREFIXES)
				or tool_type in PROVIDER_EGRESS_TOOL_TYPES
				or tool_type != "custom"
			):
				return True
	return False


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
		if getattr(self, "command", None) == "POST" and not self._request_body_consumed:
			length = self.headers.get("Content-Length", "")
			if length.isascii() and length.isdecimal() and len(length) <= len(str(MAX_BODY)) and 0 < int(length) <= MAX_BODY:
				# Let a sending client finish without letting an incomplete body stall the relay.
				previous_timeout = self.connection.gettimeout()
				deadline = time.monotonic() + 1
				body_remaining = getattr(self, "_unread_body_bytes", int(length))
				try:
					while body_remaining and (seconds_left := deadline - time.monotonic()) > 0:
						self.connection.settimeout(seconds_left)
						# read1 avoids retrying receives past the overall deadline.
						drained_chunk = self.rfile.read1(min(65536, body_remaining))
						if not drained_chunk:
							break
						body_remaining -= len(drained_chunk)
				except OSError:
					pass
				finally:
					self.connection.settimeout(previous_timeout)
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
		self._request_body_consumed = False
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
			or len(length) > len(str(MAX_BODY))
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
		previous_timeout = self.connection.gettimeout()
		deadline = time.monotonic() + BODY_READ_TIMEOUT
		body_parts = []
		body_remaining = int(length)
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
		self._request_body_consumed = body_remaining == 0
		if body_remaining:
			self._unread_body_bytes = body_remaining
			return self._reject(400)
		body = b"".join(body_parts)
		if mode == "broker":
			try:
				request = json.loads(body)
			except (UnicodeError, ValueError):
				return self._reject(400)
			if not isinstance(request, dict) or request.get("model") not in self.server.models:
				return self._reject(400)
			if request_has_provider_egress(request, getattr(self.server, "allow_web_search", False)):
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
		if len(sys.argv) not in (5, 6) or (len(sys.argv) == 6 and sys.argv[5] != WEB_SEARCH_BROKER_FLAG):
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
		server.allow_web_search = len(sys.argv) == 6
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
