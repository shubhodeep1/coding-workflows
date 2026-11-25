"""HTTP endpoints: POST /webhook (GitHub App `workflow_job` events) and GET /healthz.

A webhook is accepted only when its `X-Hub-Signature-256` HMAC-SHA256 over the
raw body verifies against RUNNER_SCALER_WEBHOOK_SECRET; bodies over 1 MiB are
refused before they are read. Accepted events only update in-memory state and
wake the reconcile loop, so the handler answers within GitHub's 10-second
delivery timeout whatever the APIs are doing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .log import LogFn, log as default_log

MAX_BODY_BYTES = 1024 * 1024
# Total time a client may take to send the webhook body. The handler's socket
# timeout only bounds each read, so a client trickling bytes would otherwise
# hold a thread for as long as it likes.
BODY_READ_DEADLINE_SECONDS = 10


def signature_valid(secret: str, body: bytes, header: str | None) -> bool:
	if not secret or not header or not header.startswith("sha256="):
		return False
	expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
	# Compare bytes: compare_digest raises TypeError on a non-ASCII str, and a
	# malformed header must get the plain 401 and the webhook_rejected line.
	return hmac.compare_digest(expected.encode("ascii"), header.encode("utf-8", "replace"))


class App:
	"""What the handler needs; kept separate from the HTTP plumbing for tests."""

	def __init__(self, *, secret: str, handle_event: Callable[[str, Any], str], healthy: Callable[[], bool], wake: threading.Event, log: LogFn = default_log):
		self.secret = secret
		self.handle_event = handle_event
		self.healthy = healthy
		self.wake = wake
		self.log = log

	def webhook(self, headers: Any, body: bytes) -> tuple[int, str]:
		if not signature_valid(self.secret, body, headers.get("X-Hub-Signature-256")):
			self.log("webhook_rejected", reason="bad_signature")
			return 401, "bad signature"
		event = str(headers.get("X-GitHub-Event") or "")
		try:
			payload = json.loads(body or b"{}")
		except ValueError:
			return 400, "invalid json"
		outcome = self.handle_event(event, payload)
		if outcome in ("queued", "in_progress", "waiting", "completed"):
			self.wake.set()
		return 202, outcome

	def health(self) -> tuple[int, str]:
		return (200, "ok") if self.healthy() else (503, "reconcile stalled")


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
	class Handler(BaseHTTPRequestHandler):
		server_version = "runner-scaler"
		# A client that stalls mid-request is dropped after 10 s (no slowloris).
		timeout = 10

		def _reply(self, status: int, text: str) -> None:
			body = (text + "\n").encode("utf-8")
			self.send_response(status)
			self.send_header("Content-Type", "text/plain; charset=utf-8")
			self.send_header("Content-Length", str(len(body)))
			self.end_headers()
			self.wfile.write(body)

		def do_GET(self) -> None:  # noqa: N802 (http.server naming)
			if self.path == "/healthz":
				self._reply(*app.health())
			else:
				self._reply(404, "not found")

		def do_POST(self) -> None:  # noqa: N802
			if self.path != "/webhook":
				self._reply(404, "not found")
				return
			try:
				length = int(self.headers.get("Content-Length") or 0)
			except ValueError:
				length = -1
			if length < 0 or length > MAX_BODY_BYTES:
				self._reply(413, "body too large")
				return
			body = bytearray()
			deadline = time.monotonic() + BODY_READ_DEADLINE_SECONDS
			while len(body) < length:
				left = deadline - time.monotonic()
				if left <= 0:
					self._reply(408, "request timeout")
					return
				# A socket timeout here ends the request (handle_one_request drops it).
				self.connection.settimeout(left)
				chunk = self.rfile.read1(min(length - len(body), 65536))
				if not chunk:
					break
				body += chunk
			self._reply(*app.webhook(self.headers, bytes(body)))

		def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
			# Request lines are not logged: decisions are logged by the scaler.
			return

	return Handler


def make_server(app: App, port: int) -> ThreadingHTTPServer:
	return ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
