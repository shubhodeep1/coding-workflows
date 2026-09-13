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
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED_PATHS = frozenset(("/api/v1/responses", "/api/v1/chat/completions"))
MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BODY_BYTES = 64 * 1024 * 1024
# The provider reports the output tokens it actually generated in the `usage`
# object, which sits at the very end of both response shapes: the final
# `data:` chunk of a chat-completions SSE stream, the `response.completed`
# event of a Responses API stream, or the trailing key of a non-streamed JSON
# body. Only this many trailing bytes of the upstream body are retained for
# the usage true-up, so a large response does not pin its whole body in
# memory while it is relayed to the agent.
USAGE_SCAN_TAIL_BYTES = 1024 * 1024
USAGE_OUTPUT_TOKEN_FIELDS = ("completion_tokens", "output_tokens")
MAX_HEADER_COUNT = 64
MAX_HEADER_BYTES = 32 * 1024
HOP_BY_HOP_HEADERS = frozenset(
	("connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade")
)
PRICE_FIELDS = ("prompt", "completion", "request", "image")


class BrokerRequestError(ValueError):
	"""A safe-to-report request policy rejection."""


def _reject_duplicate_key(pairs: list[tuple[str, object]]) -> dict[str, object]:
	document: dict[str, object] = {}
	for key, value in pairs:
		if key in document:
			raise BrokerRequestError("request JSON contains a duplicate field")
		document[key] = value
	return document


def _parse_decimal(value: object, field_name: str) -> Decimal:
	if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
		raise BrokerRequestError(f"{field_name} must be a non-negative decimal")
	try:
		parsed = Decimal(str(value))
	except InvalidOperation as exc:
		raise BrokerRequestError(f"{field_name} must be a non-negative decimal") from exc
	if not parsed.is_finite() or parsed < 0:
		raise BrokerRequestError(f"{field_name} must be a non-negative decimal")
	return parsed


def _encode_json(value: object) -> str:
	if value is None:
		return "null"
	if value is True:
		return "true"
	if value is False:
		return "false"
	if isinstance(value, str):
		return json.dumps(value, ensure_ascii=False)
	if isinstance(value, int) and not isinstance(value, bool):
		return str(value)
	if isinstance(value, Decimal):
		return format(value, "f")
	if isinstance(value, list):
		return "[" + ",".join(_encode_json(item) for item in value) + "]"
	if isinstance(value, dict):
		return "{" + ",".join(
			f"{json.dumps(key, ensure_ascii=False)}:{_encode_json(item)}"
			for key, item in value.items()
		) + "}"
	raise BrokerRequestError("request JSON contains an unsupported value")


@dataclass(frozen=True)
class BrokerPolicy:
	allowed_models: frozenset[str]
	max_output_tokens: int
	max_total_output_tokens: int
	max_prices: tuple[Decimal, Decimal, Decimal, Decimal]

	def normalize_request(self, path: str, body: bytes) -> tuple[bytes, int]:
		try:
			document = json.loads(
				body.decode("utf-8"),
				object_pairs_hook=_reject_duplicate_key,
				parse_float=Decimal,
				parse_constant=lambda _value: (_ for _ in ()).throw(BrokerRequestError("request JSON contains a non-finite number")),
			)
		except (UnicodeDecodeError, json.JSONDecodeError, BrokerRequestError) as exc:
			if isinstance(exc, BrokerRequestError):
				raise
			raise BrokerRequestError("request body must be valid JSON") from exc
		if not isinstance(document, dict):
			raise BrokerRequestError("request JSON must be an object")
		if "models" in document:
			raise BrokerRequestError("fallback model arrays are not authorized")
		model = document.get("model")
		if not isinstance(model, str) or not model or model not in self.allowed_models:
			raise BrokerRequestError("request model is not authorized")
		if "plugins" in document:
			raise BrokerRequestError("request plugins are not authorized")
		modalities = document.get("modalities")
		if modalities is not None and modalities != ["text"]:
			raise BrokerRequestError("only text output is authorized")

		if path == "/api/v1/responses":
			if "max_tokens" in document or "max_completion_tokens" in document:
				raise BrokerRequestError("response request uses an unsupported token limit field")
			output_tokens = self._bounded_token_limit(document.get("max_output_tokens"), "max_output_tokens")
			document["max_output_tokens"] = output_tokens
		else:
			completion_limit = document.get("max_completion_tokens")
			legacy_limit = document.get("max_tokens")
			if completion_limit is not None and legacy_limit is not None and completion_limit != legacy_limit:
				raise BrokerRequestError("chat request contains conflicting token limits")
			output_tokens = self._bounded_token_limit(
				completion_limit if completion_limit is not None else legacy_limit,
				"max_completion_tokens",
			)
			document.pop("max_tokens", None)
			document["max_completion_tokens"] = output_tokens
			# A streamed chat completion only carries `usage` in its final
			# chunk when the request opts in. Without it the broker could
			# never true up the reservation below, so every streamed
			# agent turn would stay charged at the full per-request
			# ceiling and an agentic loop would exhaust the total budget
			# after `max_total_output_tokens / max_output_tokens` turns.
			if document.get("stream") is True:
				stream_options = document.get("stream_options")
				if stream_options is None:
					stream_options = {}
					document["stream_options"] = stream_options
				if not isinstance(stream_options, dict):
					raise BrokerRequestError("stream_options must be an object")
				stream_options["include_usage"] = True

		provider = document.get("provider")
		if provider is None:
			provider = {}
			document["provider"] = provider
		if not isinstance(provider, dict):
			raise BrokerRequestError("provider must be an object")
		provided_prices = provider.get("max_price")
		if provided_prices is None:
			provided_prices = {}
		if not isinstance(provided_prices, dict) or any(key not in PRICE_FIELDS for key in provided_prices):
			raise BrokerRequestError("provider.max_price contains unsupported fields")
		bounded_prices: dict[str, Decimal] = {}
		for price_name, policy_ceiling in zip(PRICE_FIELDS, self.max_prices, strict=True):
			requested_price = provided_prices.get(price_name)
			bounded_prices[price_name] = policy_ceiling if requested_price is None else min(
				_parse_decimal(requested_price, f"provider.max_price.{price_name}"),
				policy_ceiling,
			)
		provider["max_price"] = bounded_prices
		return _encode_json(document).encode("utf-8"), output_tokens

	def _bounded_token_limit(self, value: object, field_name: str) -> int:
		if value is None:
			return self.max_output_tokens
		if isinstance(value, bool) or not isinstance(value, int) or value < 1:
			raise BrokerRequestError(f"{field_name} must be a positive integer")
		return min(value, self.max_output_tokens)


def _usage_output_tokens(document: object) -> int | None:
	"""Return the provider-reported output token count in a response document.

	Accepts a chat-completions object or chunk (`usage.completion_tokens`),
	a Responses API object (`usage.output_tokens`), or a Responses API stream
	event that wraps the object (`response.usage.output_tokens`). Returns None
	when the document carries no usable usage so the caller keeps the
	pessimistic reservation instead of guessing.
	"""
	if not isinstance(document, dict):
		return None
	usage = document.get("usage")
	if usage is None:
		wrapped = document.get("response")
		if isinstance(wrapped, dict):
			usage = wrapped.get("usage")
	if not isinstance(usage, dict):
		return None
	for field_name in USAGE_OUTPUT_TOKEN_FIELDS:
		value = usage.get(field_name)
		if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
			return value
	return None


def _extract_output_token_usage(response_tail: bytes, content_type: str) -> int | None:
	"""Parse the actual output token usage from the tail of an upstream body.

	`response_tail` is at most USAGE_SCAN_TAIL_BYTES of the end of the body.
	For SSE bodies the `data:` lines are scanned from the end so the final
	usage-bearing chunk wins and a truncated first line is simply skipped.
	Non-streamed bodies must fit in the tail to be parsed. Returns None
	whenever no usage can be read (fail-closed: the reservation stays).
	"""
	text = response_tail.decode("utf-8", errors="replace")
	if content_type.partition(";")[0].strip().lower() == "text/event-stream":
		for line in reversed(text.splitlines()):
			stripped = line.strip()
			if not stripped.startswith("data:"):
				continue
			payload = stripped[len("data:"):].strip()
			if not payload or payload == "[DONE]":
				continue
			try:
				chunk = json.loads(payload)
			except json.JSONDecodeError:
				continue
			usage = _usage_output_tokens(chunk)
			if usage is not None:
				return usage
		return None
	if len(response_tail) >= USAGE_SCAN_TAIL_BYTES:
		return None
	try:
		document = json.loads(text)
	except json.JSONDecodeError:
		return None
	return _usage_output_tokens(document)


class BrokerState:
	def __init__(
		self,
		upstream_url: str,
		upstream_key: str,
		session_token: str,
		max_requests: int,
		policy: BrokerPolicy,
	) -> None:
		parsed = urlsplit(upstream_url)
		if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
			raise ValueError("upstream URL must be an HTTPS origin with an optional path")
		self.upstream_host = parsed.hostname
		self.upstream_port = parsed.port or 443
		self.upstream_prefix = parsed.path.rstrip("/")
		self.upstream_key = upstream_key
		self.session_token = session_token
		self.max_requests = max_requests
		self.policy = policy
		self.requests_started = 0
		self.output_tokens_reserved = 0
		self.lock = threading.Lock()

	def reserve_request(self, output_tokens: int) -> bool:
		with self.lock:
			if (
				self.requests_started >= self.max_requests
				or self.output_tokens_reserved + output_tokens > self.policy.max_total_output_tokens
			):
				return False
			self.requests_started += 1
			self.output_tokens_reserved += output_tokens
			return True

	def settle_request(self, reserved_output_tokens: int, actual_output_tokens: int | None) -> None:
		"""True up a completed request's reservation against real usage.

		`reserve_request` charges the request's full output ceiling while it
		is in flight, which is the only safe assumption before the provider
		answers. Once the response is complete the provider-reported usage
		replaces that ceiling so the total budget bounds tokens actually
		generated rather than the number of requests: without this, every
		agentic turn stayed charged at the ceiling and the editor hit HTTP
		429 after `max_total_output_tokens / max_output_tokens` (4 by
		default) turns. `actual_output_tokens=None` means usage could not
		be read (cut-off body, missing `usage`), and the full reservation
		stays charged — fail closed, never fail open.
		"""
		if actual_output_tokens is None:
			return
		with self.lock:
			self.output_tokens_reserved = max(
				0,
				self.output_tokens_reserved - reserved_output_tokens + actual_output_tokens,
			)


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
		body = self.rfile.read(content_length)
		if len(body) != content_length:
			self._reject(400, "request body was truncated")
			return
		if self.headers.get("Content-Type", "").partition(";")[0].strip().lower() != "application/json":
			self._reject(415, "application/json content type required")
			return
		try:
			body, output_tokens = self.broker_state.policy.normalize_request(self.path, body)
		except BrokerRequestError as exc:
			self._reject(400, str(exc))
			return
		if not self.broker_state.reserve_request(output_tokens):
			self._reject(429, "broker request or output-token limit reached")
			return

		upstream_headers = {
			"Authorization": f"Bearer {self.broker_state.upstream_key}",
			"Content-Type": self.headers.get("Content-Type", "application/json"),
			"Content-Length": str(len(body)),
			"Accept": self.headers.get("Accept", "application/json"),
		}
		upstream_path = f"{self.broker_state.upstream_prefix}{self.path.removeprefix('/api/v1')}"
		connection = http.client.HTTPSConnection(
			self.broker_state.upstream_host,
			self.broker_state.upstream_port,
			timeout=600,
			context=ssl.create_default_context(),
		)
		# Usage true-up state: the request is charged its full output ceiling
		# until the upstream body has been relayed completely, then settled
		# against the provider-reported usage (see BrokerState.settle_request).
		# An upstream error status generated no output, so it settles at 0;
		# a cut-off or unparseable body keeps the full reservation.
		upstream_status: int | None = None
		upstream_content_type = ""
		response_tail = bytearray()
		body_complete = False
		try:
			connection.request("POST", upstream_path, body=body, headers=upstream_headers)
			response = connection.getresponse()
			upstream_status = response.status
			upstream_content_type = response.getheader("Content-Type", "") or ""
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
					body_complete = True
					break
				response_bytes += len(chunk)
				if response_bytes > MAX_RESPONSE_BODY_BYTES:
					self.close_connection = True
					break
				response_tail += chunk
				if len(response_tail) > USAGE_SCAN_TAIL_BYTES:
					del response_tail[:-USAGE_SCAN_TAIL_BYTES]
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
			actual_output_tokens: int | None = None
			if upstream_status is not None and upstream_status >= 400:
				actual_output_tokens = 0
			elif body_complete:
				actual_output_tokens = _extract_output_token_usage(bytes(response_tail), upstream_content_type)
			self.broker_state.settle_request(output_tokens, actual_output_tokens)


class BrokerServer(ThreadingHTTPServer):
	daemon_threads = True
	allow_reuse_address = False

	def __init__(self, address: tuple[str, int], state: BrokerState) -> None:
		super().__init__(address, BrokerHandler)
		self.broker_state = state


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
	parser.add_argument("--allowed-model", action="append", required=True)
	parser.add_argument("--max-output-tokens", type=int, default=16_384)
	# Default = max-requests (100) x max-output-tokens (16384): the total budget
	# never bites below what the request cap already permits, while the price
	# ceilings still bound the spend of one phase.
	parser.add_argument("--max-total-output-tokens", type=int, default=1_638_400)
	for price_field in PRICE_FIELDS:
		parser.add_argument(f"--max-{price_field}-price", default="0")
	args = parser.parse_args()
	if not 1 <= args.max_requests <= 100:
		parser.error("--max-requests must be between 1 and 100")
	if not 1 <= args.max_output_tokens <= 1_000_000:
		parser.error("--max-output-tokens must be between 1 and 1000000")
	if not args.max_output_tokens <= args.max_total_output_tokens <= 10_000_000:
		parser.error("--max-total-output-tokens must cover one request and be at most 10000000")
	allowed_models = frozenset(args.allowed_model)
	if any(not model or len(model) > 256 for model in allowed_models):
		parser.error("--allowed-model values must be non-empty and at most 256 characters")
	try:
		max_prices = tuple(
			_parse_decimal(getattr(args, f"max_{price_field}_price"), f"--max-{price_field}-price")
			for price_field in PRICE_FIELDS
		)
	except BrokerRequestError as exc:
		parser.error(str(exc))
	policy = BrokerPolicy(allowed_models, args.max_output_tokens, args.max_total_output_tokens, max_prices)  # type: ignore[arg-type]
	upstream_key = os.environ.get("OPENROUTER_API_KEY", "")
	if not upstream_key:
		print("model provider broker: OPENROUTER_API_KEY is unavailable", file=sys.stderr)
		return 2
	try:
		state = BrokerState(args.upstream_url, upstream_key, secrets.token_urlsafe(32), args.max_requests, policy)
	except ValueError as exc:
		print(f"model provider broker: {exc}", file=sys.stderr)
		return 2
	server = BrokerServer(("127.0.0.1", 0), state)
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
