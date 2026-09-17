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
import socket
import ssl
import sys
import tempfile
import threading
import time
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
USAGE_INPUT_TOKEN_FIELDS = ("prompt_tokens", "input_tokens")
# Admission-time input-token estimate for a normalized request body. Model
# tokenizers can produce one token from a single UTF-8 byte, so the common
# four-bytes-per-token average is not safe for admission control. Reserving one
# token per byte is a tokenizer-independent upper bound for the normalized body;
# provider-reported `usage` replaces it after completion (see settle_request).
INPUT_TOKEN_ESTIMATE_BYTES_PER_TOKEN = 1
DEFAULT_MAX_INPUT_TOKENS = -(-MAX_REQUEST_BODY_BYTES // INPUT_TOKEN_ESTIMATE_BYTES_PER_TOKEN)
MAX_TOTAL_INPUT_TOKENS_CEILING = 100 * DEFAULT_MAX_INPUT_TOKENS
TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)
MAX_HEADER_COUNT = 64
MAX_HEADER_BYTES = 32 * 1024
MAX_REJECTIONS_PER_STATUS_CLASS = 100
BROKER_CLIENT_READ_TIMEOUT_SECONDS = 30
BROKER_MAX_ACTIVE_CONNECTIONS = 8
HOP_BY_HOP_HEADERS = frozenset(
	("connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade")
)
PRICE_FIELDS = ("prompt", "completion", "request", "image")
IMAGE_INPUT_TYPE_MARKERS = (b'"type":"image"', b'"type":"image_url"', b'"type":"input_image"')
MAX_IMAGE_INPUTS_PER_REQUEST = MAX_REQUEST_BODY_BYTES // min(len(marker) for marker in IMAGE_INPUT_TYPE_MARKERS)


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
		# Preserve exponent notation so compact inputs cannot expand into an
		# attacker-controlled fixed-point allocation during normalization.
		return str(value)
	if isinstance(value, list):
		return "[" + ",".join(_encode_json(item) for item in value) + "]"
	if isinstance(value, dict):
		return "{" + ",".join(
			f"{json.dumps(key, ensure_ascii=False)}:{_encode_json(item)}"
			for key, item in value.items()
		) + "}"
	raise BrokerRequestError("request JSON contains an unsupported value")


def _count_image_inputs(normalized_body: bytes) -> int:
	"""Count priced image input parts in a normalized provider request."""
	return sum(normalized_body.count(marker) for marker in IMAGE_INPUT_TYPE_MARKERS)


@dataclass(frozen=True)
class BrokerPolicy:
	allowed_models: frozenset[str]
	max_output_tokens: int
	max_total_output_tokens: int
	max_prices: tuple[Decimal, Decimal, Decimal, Decimal]
	# Input-token and decimal cost budgets (security-pass finding
	# `broker-authorizes-arbitrary-paid-requests`, #4090). The output budget
	# alone left input spend outside the phase budget: up to `max_requests`
	# near-context prompts were authorized as long as each response stayed
	# small. Defaults are derived so the ceilings never bite below what the
	# request cap and price ceilings already permit; operators tighten them.
	max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
	max_total_input_tokens: int = MAX_TOTAL_INPUT_TOKENS_CEILING
	max_total_cost_usd: Decimal | None = None

	def estimate_input_tokens(self, normalized_body: bytes) -> int:
		"""Return the admission-time input-token reservation for a normalized body.

		Raises BrokerRequestError when the estimate exceeds the per-request
		ceiling so an oversized prompt is rejected before it is forwarded.
		"""
		estimate = -(-len(normalized_body) // INPUT_TOKEN_ESTIMATE_BYTES_PER_TOKEN)
		if estimate > self.max_input_tokens:
			raise BrokerRequestError("request input exceeds the authorized input-token limit")
		return estimate

	def estimate_cost_usd(self, input_tokens: int, output_tokens: int, image_inputs: int = 0) -> Decimal:
		"""Return the worst-case USD cost of one request at the policy price ceilings.

		Uses the configured ceilings rather than the request's (possibly lower)
		`provider.max_price`, so the reservation is never below what the
		provider could charge under this policy.
		"""
		if image_inputs < 0:
			raise ValueError("image_inputs must be non-negative")
		prompt_price, completion_price, request_price, image_price = self.max_prices
		return (
			Decimal(input_tokens) * prompt_price / TOKENS_PER_PRICE_UNIT
			+ Decimal(output_tokens) * completion_price / TOKENS_PER_PRICE_UNIT
			+ request_price
			+ Decimal(image_inputs) * image_price
		)

	def normalize_request(self, path: str, body: bytes) -> tuple[bytes, int]:
		try:
			document = json.loads(
				body.decode("utf-8"),
				object_pairs_hook=_reject_duplicate_key,
				parse_float=Decimal,
				parse_constant=lambda _value: (_ for _ in ()).throw(BrokerRequestError("request JSON contains a non-finite number")),
			)
		except BrokerRequestError:
			raise
		except (InvalidOperation, ValueError, RecursionError) as exc:
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
			choice_count = document.get("n", 1)
			if isinstance(choice_count, bool) or not isinstance(choice_count, int) or choice_count != 1:
				raise BrokerRequestError("only single-choice chat requests are authorized")
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
		try:
			normalized_body = _encode_json(document).encode("utf-8")
		except RecursionError as exc:
			raise BrokerRequestError("request JSON is too deeply nested") from exc
		if len(normalized_body) > MAX_REQUEST_BODY_BYTES:
			raise BrokerRequestError("normalized request body is too large")
		return normalized_body, output_tokens

	def _bounded_token_limit(self, value: object, field_name: str) -> int:
		if value is None:
			return self.max_output_tokens
		if isinstance(value, bool) or not isinstance(value, int) or value < 1:
			raise BrokerRequestError(f"{field_name} must be a positive integer")
		return min(value, self.max_output_tokens)


def _usage_token_count(document: object, field_names: tuple[str, ...]) -> int | None:
	"""Return the first usable token count under `usage` for the given fields.

	Accepts a chat-completions object or chunk (`usage.*`), a Responses API
	object (`usage.*`), or a Responses API stream event that wraps the object
	(`response.usage.*`). Returns None when the document carries no usable
	usage so the caller keeps the pessimistic reservation instead of guessing.
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
	for field_name in field_names:
		value = usage.get(field_name)
		if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
			return value
	return None


def _usage_output_tokens(document: object) -> int | None:
	"""Return the provider-reported output token count in a response document
	(`usage.completion_tokens` or `usage.output_tokens`); see _usage_token_count."""
	return _usage_token_count(document, USAGE_OUTPUT_TOKEN_FIELDS)


def _usage_input_tokens(document: object) -> int | None:
	"""Return the provider-reported input token count in a response document
	(`usage.prompt_tokens` or `usage.input_tokens`); see _usage_token_count."""
	return _usage_token_count(document, USAGE_INPUT_TOKEN_FIELDS)


def _extract_token_usage(response_tail: bytes, content_type: str) -> tuple[int | None, int | None]:
	"""Parse the actual (output, input) token usage from the tail of an upstream body.

	`response_tail` is at most USAGE_SCAN_TAIL_BYTES of the end of the body.
	For SSE bodies the `data:` lines are scanned from the end so the final
	usage-bearing chunk wins and a truncated first line is simply skipped.
	Callers must pass complete non-streamed bodies; streamed bodies may be a
	tail. Each element is None whenever that count cannot be read
	(fail-closed: the corresponding reservation stays).
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
			output_usage = _usage_output_tokens(chunk)
			if output_usage is not None:
				return output_usage, _usage_input_tokens(chunk)
		return None, None
	try:
		document = json.loads(text)
	except json.JSONDecodeError:
		return None, None
	return _usage_output_tokens(document), _usage_input_tokens(document)


def _extract_output_token_usage(response_tail: bytes, content_type: str) -> int | None:
	"""Output-token half of _extract_token_usage; retained for existing callers."""
	return _extract_token_usage(response_tail, content_type)[0]


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
		self.input_tokens_reserved = 0
		self.cost_usd_reserved = Decimal(0)
		self.lock = threading.Lock()

	def reserve_request(self, output_tokens: int, input_tokens: int = 0, image_inputs: int = 0) -> bool:
		"""Admit one request, charging its output, input, and cost ceilings.

		`input_tokens` is the admission-time estimate from
		BrokerPolicy.estimate_input_tokens; the cost reservation is the
		worst case of both token counts and recognized image inputs at the
		policy price ceilings plus the per-request price. Any exhausted
		budget refuses admission.
		"""
		request_cost = self.policy.estimate_cost_usd(input_tokens, output_tokens, image_inputs)
		with self.lock:
			if (
				self.requests_started >= self.max_requests
				or self.output_tokens_reserved + output_tokens > self.policy.max_total_output_tokens
				or self.input_tokens_reserved + input_tokens > self.policy.max_total_input_tokens
				or (
					self.policy.max_total_cost_usd is not None
					and self.cost_usd_reserved + request_cost > self.policy.max_total_cost_usd
				)
			):
				return False
			self.requests_started += 1
			self.output_tokens_reserved += output_tokens
			self.input_tokens_reserved += input_tokens
			self.cost_usd_reserved += request_cost
			return True

	def settle_request(
		self,
		reserved_output_tokens: int,
		actual_output_tokens: int | None,
		reserved_input_tokens: int = 0,
		actual_input_tokens: int | None = None,
		billable: bool = True,
		reserved_image_inputs: int = 0,
	) -> None:
		"""True up a completed request's reservation against real usage.

		`reserve_request` charges the request's full output ceiling and its
		input estimate while it is in flight, which is the only safe
		assumption before the provider answers. Once the response is
		complete the provider-reported usage replaces those figures so the
		total budgets bound tokens actually processed rather than the number
		of requests: without this, every agentic turn stayed charged at the
		ceiling and the editor hit HTTP 429 after
		`max_total_output_tokens / max_output_tokens` (4 by default) turns.
		A `None` actual count means that usage could not be read (cut-off
		body, missing `usage`), and that reservation stays charged — fail
		closed, never fail open. The cost reservation is re-derived from the
		settled token counts and fixed image count at the policy price ceilings;
		`billable=False` (an observed upstream error response, or a failure
		before forwarding starts) settles the cost to zero, including the
		per-request price. An ambiguous transport failure after forwarding
		starts keeps the full reservation.
		"""
		settled_output_tokens = reserved_output_tokens if actual_output_tokens is None else actual_output_tokens
		settled_input_tokens = reserved_input_tokens if actual_input_tokens is None else actual_input_tokens
		if billable and settled_output_tokens == reserved_output_tokens and settled_input_tokens == reserved_input_tokens:
			return
		reserved_cost = self.policy.estimate_cost_usd(
			reserved_input_tokens, reserved_output_tokens, reserved_image_inputs,
		)
		settled_cost = self.policy.estimate_cost_usd(
			settled_input_tokens, settled_output_tokens, reserved_image_inputs,
		) if billable else Decimal(0)
		with self.lock:
			self.output_tokens_reserved = max(
				0,
				self.output_tokens_reserved - reserved_output_tokens + settled_output_tokens,
			)
			self.input_tokens_reserved = max(
				0,
				self.input_tokens_reserved - reserved_input_tokens + settled_input_tokens,
			)
			self.cost_usd_reserved = max(
				Decimal(0),
				self.cost_usd_reserved - reserved_cost + settled_cost,
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
		try:
			body = self.rfile.read(content_length)
		except OSError:
			try:
				self._reject(408, "request body read timed out")
			except OSError:
				self.close_connection = True
			return
		if len(body) != content_length:
			self._reject(400, "request body was truncated")
			return
		if self.headers.get("Content-Type", "").partition(";")[0].strip().lower() != "application/json":
			self._reject(415, "application/json content type required")
			return
		try:
			body, output_tokens = self.broker_state.policy.normalize_request(self.path, body)
			input_tokens = self.broker_state.policy.estimate_input_tokens(body)
			image_inputs = _count_image_inputs(body)
		except BrokerRequestError as exc:
			self._reject(400, str(exc))
			return
		if not self.broker_state.reserve_request(output_tokens, input_tokens, image_inputs):
			self._reject(429, "broker request or output-token limit reached (request, token, or cost budget exhausted)")
			return

		upstream_headers = {
			"Authorization": f"Bearer {self.broker_state.upstream_key}",
			"Content-Type": self.headers.get("Content-Type", "application/json"),
			"Content-Length": str(len(body)),
			"Accept": self.headers.get("Accept", "application/json"),
		}
		upstream_path = f"{self.broker_state.upstream_prefix}{self.path.removeprefix('/api/v1')}"
		connection: http.client.HTTPSConnection | None = None
		# Usage true-up state: the request is charged its full output ceiling
		# and its input estimate until the upstream body has been relayed
		# completely, then settled against the provider-reported usage (see
		# BrokerState.settle_request). An observed upstream error response or
		# pre-forward failure settles at 0; an ambiguous transport failure,
		# cut-off body, or unparseable body keeps the full reservation.
		upstream_status: int | None = None
		upstream_response_received = False
		upstream_request_started = False
		upstream_content_type = ""
		response_tail = bytearray()
		body_complete = False
		try:
			connection = http.client.HTTPSConnection(
				self.broker_state.upstream_host,
				self.broker_state.upstream_port,
				timeout=600,
				context=ssl.create_default_context(),
			)
			upstream_request_started = True
			connection.request("POST", upstream_path, body=body, headers=upstream_headers)
			response = connection.getresponse()
			upstream_response_received = True
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
			self.close_connection = True
			actual_output_tokens: int | None = None
			actual_input_tokens: int | None = None
			upstream_billed = True
			if not upstream_request_started or (
				upstream_response_received and upstream_status is not None and upstream_status >= 400
			):
				actual_output_tokens = 0
				actual_input_tokens = 0
				upstream_billed = False
			elif body_complete and (
				upstream_content_type.partition(";")[0].strip().lower() == "text/event-stream"
				or response_bytes <= USAGE_SCAN_TAIL_BYTES
			):
				actual_output_tokens, actual_input_tokens = _extract_token_usage(bytes(response_tail), upstream_content_type)
			self.broker_state.settle_request(
				output_tokens, actual_output_tokens, input_tokens, actual_input_tokens,
				billable=upstream_billed, reserved_image_inputs=image_inputs,
			)
			if connection is not None:
				connection.close()


class BrokerServer(ThreadingHTTPServer):
	daemon_threads = True
	allow_reuse_address = False

	def __init__(self, address: tuple[str, int], state: BrokerState, rejections_path: Path | None = None) -> None:
		super().__init__(address, BrokerHandler)
		self.broker_state = state
		self.rejections_path = rejections_path
		self.rejections_lock = threading.Lock()
		self.rejections_recorded_by_status_class = {4: 0, 5: 0}
		self._active_connection_slots = threading.BoundedSemaphore(BROKER_MAX_ACTIVE_CONNECTIONS)

	def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
		try:
			request.settimeout(BROKER_CLIENT_READ_TIMEOUT_SECONDS)
		except OSError:
			self.shutdown_request(request)
			return
		if not self._active_connection_slots.acquire(blocking=False):
			self.shutdown_request(request)
			return
		try:
			super().process_request(request, client_address)
		except BaseException:
			self._active_connection_slots.release()
			self.shutdown_request(request)
			raise

	def process_request_thread(self, request: socket.socket, client_address: tuple[str, int]) -> None:
		try:
			super().process_request_thread(request, client_address)
		finally:
			self._active_connection_slots.release()

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
	parser.add_argument("--allowed-model", action="append", required=True)
	parser.add_argument("--max-output-tokens", type=int, default=16_384)
	# Default = max-requests (100) x max-output-tokens (16384): the total budget
	# never bites below what the request cap already permits, while the price
	# ceilings still bound the spend of one phase.
	parser.add_argument("--max-total-output-tokens", type=int, default=1_638_400)
	# Input-token and cost budgets (#4090). Defaults derive from the other
	# limits so they never bite below what those already permit:
	#   --max-input-tokens        = MAX_REQUEST_BODY_BYTES = 16777216
	#   --max-total-input-tokens  = max-requests x max-input-tokens
	#   --max-total-cost-usd      = worst case of both total token budgets and
	#                               body-bounded image inputs at the price ceilings
	#                               + max-requests x request price
	parser.add_argument("--max-input-tokens", type=int, default=DEFAULT_MAX_INPUT_TOKENS)
	parser.add_argument("--max-total-input-tokens", type=int, default=None)
	parser.add_argument("--max-total-cost-usd", default=None)
	for price_field in PRICE_FIELDS:
		parser.add_argument(f"--max-{price_field}-price", default="0")
	parser.add_argument(
		"--rejections-file",
		default="",
		help=f"append up to {MAX_REJECTIONS_PER_STATUS_CLASS} JSON lines per HTTP error class so the launcher can detect deterministic policy rejections; empty disables the file",
	)
	args = parser.parse_args()
	if not 1 <= args.max_requests <= 100:
		parser.error("--max-requests must be between 1 and 100")
	if not 1 <= args.max_output_tokens <= 1_000_000:
		parser.error("--max-output-tokens must be between 1 and 1000000")
	if not args.max_output_tokens <= args.max_total_output_tokens <= 10_000_000:
		parser.error("--max-total-output-tokens must cover one request and be at most 10000000")
	if not 1 <= args.max_input_tokens <= DEFAULT_MAX_INPUT_TOKENS:
		parser.error(f"--max-input-tokens must be between 1 and {DEFAULT_MAX_INPUT_TOKENS}")
	if args.max_total_input_tokens is None:
		args.max_total_input_tokens = min(args.max_requests * args.max_input_tokens, MAX_TOTAL_INPUT_TOKENS_CEILING)
	if not args.max_input_tokens <= args.max_total_input_tokens <= MAX_TOTAL_INPUT_TOKENS_CEILING:
		parser.error(f"--max-total-input-tokens must cover one request and be at most {MAX_TOTAL_INPUT_TOKENS_CEILING}")
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
	policy = BrokerPolicy(
		allowed_models,
		args.max_output_tokens,
		args.max_total_output_tokens,
		max_prices,  # type: ignore[arg-type]
		args.max_input_tokens,
		args.max_total_input_tokens,
	)
	if args.max_total_cost_usd is None:
		max_total_cost_usd = policy.estimate_cost_usd(
			args.max_total_input_tokens,
			args.max_total_output_tokens,
			args.max_requests * MAX_IMAGE_INPUTS_PER_REQUEST,
		) + (
			(args.max_requests - 1) * max_prices[2]
		)
	else:
		try:
			max_total_cost_usd = _parse_decimal(args.max_total_cost_usd, "--max-total-cost-usd")
		except BrokerRequestError as exc:
			parser.error(str(exc))
		if max_total_cost_usd <= 0:
			parser.error("--max-total-cost-usd must be a positive decimal")
	policy = BrokerPolicy(
		allowed_models,
		args.max_output_tokens,
		args.max_total_output_tokens,
		max_prices,  # type: ignore[arg-type]
		args.max_input_tokens,
		args.max_total_input_tokens,
		max_total_cost_usd,
	)
	upstream_key = os.environ.get("OPENROUTER_API_KEY", "")
	if not upstream_key:
		print("model provider broker: OPENROUTER_API_KEY is unavailable", file=sys.stderr)
		return 2
	try:
		state = BrokerState(args.upstream_url, upstream_key, secrets.token_urlsafe(32), args.max_requests, policy)
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
