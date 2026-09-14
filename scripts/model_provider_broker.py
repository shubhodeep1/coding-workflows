#!/usr/bin/env python3
"""Bounded loopback proxy that keeps the upstream model credential from agents."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import secrets
import signal
import ssl
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ALLOWED_PATHS = frozenset(("/api/v1/responses", "/api/v1/chat/completions"))
MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BODY_BYTES = 64 * 1024 * 1024
MAX_HEADER_COUNT = 64
MAX_HEADER_BYTES = 32 * 1024
MAX_REJECTIONS_PER_STATUS_CLASS = 100
HOP_BY_HOP_HEADERS = frozenset(
	("connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade")
)


class BrokerState:
	def __init__(self, upstream_url: str, upstream_key: str, session_token: str, max_requests: int) -> None:
		parsed = urlsplit(upstream_url)
		if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
			raise ValueError("upstream URL must be an HTTPS origin with an optional path")
		self.upstream_host = parsed.hostname
		self.upstream_port = parsed.port or 443
		self.upstream_prefix = parsed.path.rstrip("/")
		self.upstream_key = upstream_key
		self.session_token = session_token
		self.max_requests = max_requests
		self.requests_started = 0
		self.lock = threading.Lock()

	def reserve_request(self) -> bool:
		with self.lock:
			if self.requests_started >= self.max_requests:
				return False
			self.requests_started += 1
			return True


class BrokerHandler(BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.1"
	server_version = "ModelProviderBroker/1"
	sys_version = ""

	@property
	def broker_state(self) -> BrokerState:
		return self.server.broker_state  # type: ignore[attr-defined, no-any-return]

	def log_message(self, _format: str, *_args: object) -> None:
		return

	def _reject(self, status: int, message: str) -> None:
		rejection_path = self.path.split("?", 1)[0].split("#", 1)[0]
		if rejection_path not in ALLOWED_PATHS:
			rejection_path = "<redacted>"
		self.server.record_rejection(status, message, rejection_path)  # type: ignore[attr-defined]
		payload = json.dumps({"error": {"message": message, "type": "broker_rejection"}}).encode("utf-8")
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(payload)))
		self.send_header("Connection", "close")
		self.end_headers()
		self.wfile.write(payload)
		self.close_connection = True

	def _authorized(self) -> bool:
		authorization = self.headers.get("Authorization", "")
		return secrets.compare_digest(authorization, f"Bearer {self.broker_state.session_token}")

	def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
		self._reject(405, "method not allowed")

	def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
		self._reject(405, "method not allowed")

	def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
		self._reject(405, "method not allowed")

	def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
		self._reject(405, "method not allowed")

	def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
		try:
			if not ipaddress.ip_address(self.client_address[0]).is_loopback:
				self._reject(403, "loopback clients only")
				return
		except ValueError:
			self._reject(403, "invalid client address")
			return
		if self.path not in ALLOWED_PATHS:
			self._reject(404, "path not allowed")
			return
		if len(self.headers) > MAX_HEADER_COUNT or sum(len(key) + len(value) for key, value in self.headers.items()) > MAX_HEADER_BYTES:
			self._reject(431, "request headers too large")
			return
		if not self._authorized():
			self._reject(401, "invalid broker token")
			return
		if self.headers.get("Transfer-Encoding"):
			self._reject(400, "transfer encoding is not accepted")
			return
		try:
			content_length = int(self.headers.get("Content-Length", ""))
		except ValueError:
			self._reject(411, "valid content length required")
			return
		if not 0 < content_length <= MAX_REQUEST_BODY_BYTES:
			self._reject(413, "request body size is invalid")
			return
		if not self.broker_state.reserve_request():
			self._reject(429, "broker request limit reached")
			return
		body = self.rfile.read(content_length)
		if len(body) != content_length:
			self._reject(400, "request body was truncated")
			return

		upstream_headers = {
			"Authorization": f"Bearer {self.broker_state.upstream_key}",
			"Content-Type": self.headers.get("Content-Type", "application/json"),
			"Content-Length": str(content_length),
			"Accept": self.headers.get("Accept", "application/json"),
		}
		upstream_path = f"{self.broker_state.upstream_prefix}{self.path.removeprefix('/api/v1')}"
		connection = http.client.HTTPSConnection(
			self.broker_state.upstream_host,
			self.broker_state.upstream_port,
			timeout=600,
			context=ssl.create_default_context(),
		)
		try:
			connection.request("POST", upstream_path, body=body, headers=upstream_headers)
			response = connection.getresponse()
			self.send_response(response.status, response.reason)
			for header_name, header_value in response.getheaders():
				if header_name.lower() not in HOP_BY_HOP_HEADERS:
					self.send_header(header_name, header_value)
			self.send_header("Connection", "close")
			self.end_headers()
			response_bytes = 0
			while True:
				chunk = response.read(64 * 1024)
				if not chunk:
					break
				response_bytes += len(chunk)
				if response_bytes > MAX_RESPONSE_BODY_BYTES:
					self.close_connection = True
					break
				self.wfile.write(chunk)
				self.wfile.flush()
		except (OSError, http.client.HTTPException, ssl.SSLError):
			if not getattr(self, "_headers_buffer", None):
				self._reject(502, "upstream request failed")
			else:
				self.close_connection = True
		finally:
			connection.close()
			self.close_connection = True


class BrokerServer(ThreadingHTTPServer):
	daemon_threads = True
	allow_reuse_address = False

	def __init__(self, address: tuple[str, int], state: BrokerState, rejections_path: Path | None = None) -> None:
		super().__init__(address, BrokerHandler)
		self.broker_state = state
		self.rejections_path = rejections_path
		self.rejections_lock = threading.Lock()
		self.rejections_recorded_by_status_class = {4: 0, 5: 0}

	def record_rejection(self, status: int, message: str, path: str) -> None:
		"""Make a policy rejection observable to the launching shell.

		The handler answers the agent with the rejection body, but that body
		only ever reaches the model runtime, which logs it as an opaque
		``AI_APICallError`` (PR #4077 editor runs: three attempts and the
		model fallback all burned on the same deterministic HTTP 429). One
		structured stderr line lands in the job log, and one JSON line is
		appended to ``--rejections-file`` so ``codex_helpers.sh`` can count
		4xx policy rejections between attempts and stop retrying a broker
		that will keep saying no. Each HTTP error class is capped separately
		so transient 502s cannot consume the 4xx evidence budget. Both writes
		are best-effort: a logging failure must never turn into a second
		failure mode for the request.
		"""
		status_class = status // 100
		with self.rejections_lock:
			if self.rejections_recorded_by_status_class.get(status_class, MAX_REJECTIONS_PER_STATUS_CLASS) >= MAX_REJECTIONS_PER_STATUS_CLASS:
				return
			self.rejections_recorded_by_status_class[status_class] += 1
			try:
				print(f"MODEL_PROVIDER_BROKER_REJECT status={status} path={path} message={json.dumps(message)}", file=sys.stderr, flush=True)
			except (OSError, ValueError):
				pass
			if self.rejections_path is None:
				return
			record = json.dumps(
				{"ts": int(time.time()), "status": int(status), "path": path, "message": message},
				sort_keys=True,
				separators=(",", ":"),
			)
			try:
				file_descriptor = os.open(self.rejections_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
				try:
					os.write(file_descriptor, (record + "\n").encode("utf-8"))
				finally:
					os.close(file_descriptor)
			except OSError:
				pass


def _atomic_write_ready_file(path: Path, document: dict[str, object]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
	try:
		os.fchmod(file_descriptor, 0o600)
		with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
			json.dump(document, handle, sort_keys=True, separators=(",", ":"))
			handle.write("\n")
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary_name, path)
		os.chmod(path, 0o600)
	except BaseException:
		try:
			os.close(file_descriptor)
		except OSError:
			pass
		try:
			os.unlink(temporary_name)
		except OSError:
			pass
		raise


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--ready-file", required=True)
	parser.add_argument("--upstream-url", default="https://openrouter.ai/api/v1")
	parser.add_argument("--max-requests", type=int, default=100)
	parser.add_argument(
		"--rejections-file",
		default="",
		help=f"append up to {MAX_REJECTIONS_PER_STATUS_CLASS} JSON lines per HTTP error class so the launcher can detect deterministic policy rejections; empty disables the file",
	)
	args = parser.parse_args()
	if not 1 <= args.max_requests <= 100:
		parser.error("--max-requests must be between 1 and 100")
	upstream_key = os.environ.get("OPENROUTER_API_KEY", "")
	if not upstream_key:
		print("model provider broker: OPENROUTER_API_KEY is unavailable", file=sys.stderr)
		return 2
	try:
		state = BrokerState(args.upstream_url, upstream_key, secrets.token_urlsafe(32), args.max_requests)
	except ValueError as exc:
		print(f"model provider broker: {exc}", file=sys.stderr)
		return 2
	rejections_path = Path(args.rejections_file) if args.rejections_file else None
	if rejections_path is not None:
		try:
			rejections_path.parent.mkdir(parents=True, exist_ok=True)
			try:
				rejections_path.unlink()
			except FileNotFoundError:
				pass
		except OSError:
			try:
				print("model provider broker: rejection file unavailable; continuing without file recording", file=sys.stderr)
			except OSError:
				pass
			rejections_path = None
	server = BrokerServer(("127.0.0.1", 0), state, rejections_path)
	stop_event = threading.Event()

	def request_stop(_signum: int, _frame: object) -> None:
		stop_event.set()

	signal.signal(signal.SIGTERM, request_stop)
	signal.signal(signal.SIGINT, request_stop)
	ready_path = Path(args.ready_file)
	_atomic_write_ready_file(
		ready_path,
		{
			"schema_version": "model_provider_broker_ready.v1",
			"base_url": f"http://127.0.0.1:{server.server_port}/api/v1",
			"token": state.session_token,
			"pid": os.getpid(),
		},
	)
	server.timeout = 0.5
	try:
		while not stop_event.is_set():
			server.handle_request()
	finally:
		server.server_close()
		try:
			ready_path.unlink()
		except FileNotFoundError:
			pass
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
