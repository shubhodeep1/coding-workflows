#!/usr/bin/env python3
"""Restricted Responses API relay: host Unix socket broker / container loopback bridge.

The host process alone holds the provider credential. Neither side accepts a
destination URL from the client. Never log requests, upstream bodies or headers.

The broker modes enforce the same request, token and cost budgets as
model_provider_broker.py (#4090), configured by the MODEL_PROVIDER_BROKER_MAX_*
environment variables. The bridge runs alone inside the container as a single
mounted file, so the budget module is loaded only by the broker modes.
"""

import http.client
import http.server
import os
import socket
import socketserver
import ssl
import sys

MAX_BODY = 8 * 1024 * 1024
PROVIDER_PATH = "/api/v1/responses"
REVIEW_PATH = "/api/v1/chat/completions"
UPSTREAM_URL = "https://openrouter.ai/api/v1"
# Same defaults codex_helpers.sh passes to model_provider_broker.py; the
# derived budgets (total input tokens, total cost) follow its main().
BUDGET_DEFAULTS = {
	"MAX_REQUESTS": "100",
	"MAX_OUTPUT_TOKENS": "16384",
	"MAX_TOTAL_OUTPUT_TOKENS": "1638400",
	"MAX_PROMPT_PRICE": "10",
	"MAX_COMPLETION_PRICE": "30",
	"MAX_REQUEST_PRICE": "0.10",
	"MAX_IMAGE_PRICE": "1",
}


def _load_budget_module():
	"""Load model_provider_broker.py from this script's own directory.

	An explicit file location, not sys.path, so neither the working directory
	nor PYTHONPATH can substitute the module that enforces the budgets.
	"""
	import importlib.util
	from pathlib import Path

	location = Path(__file__).resolve().with_name("model_provider_broker.py")
	spec = importlib.util.spec_from_file_location("model_provider_broker", location)
	if spec is None or spec.loader is None or not location.is_file():
		raise SystemExit(2)
	module = importlib.util.module_from_spec(spec)
	# dataclass resolution looks the defining module up in sys.modules.
	sys.modules["model_provider_broker"] = module
	spec.loader.exec_module(module)
	return module


def _budget_int(name, default=None):
	value = os.environ.get("MODEL_PROVIDER_BROKER_" + name, "").strip() or default
	if value is None:
		return None
	if not value.isascii() or not value.isdecimal():
		raise SystemExit(2)
	return int(value)


def build_budget_state(model):
	"""Return a model_provider_broker.BrokerState for one relay process.

	Applies model_provider_broker.py main()'s bounds to the
	MODEL_PROVIDER_BROKER_MAX_* variables; any invalid value exits with
	status 2 so the launcher never runs an unbudgeted relay.
	"""
	budget = _load_budget_module()
	max_requests = _budget_int("MAX_REQUESTS", BUDGET_DEFAULTS["MAX_REQUESTS"])
	max_output = _budget_int("MAX_OUTPUT_TOKENS", BUDGET_DEFAULTS["MAX_OUTPUT_TOKENS"])
	max_total_output = _budget_int("MAX_TOTAL_OUTPUT_TOKENS", BUDGET_DEFAULTS["MAX_TOTAL_OUTPUT_TOKENS"])
	max_input = _budget_int("MAX_INPUT_TOKENS", str(budget.DEFAULT_MAX_INPUT_TOKENS))
	max_total_input = _budget_int("MAX_TOTAL_INPUT_TOKENS")
	if (
		not 1 <= max_requests <= 100
		or not 1 <= max_output <= 1_000_000
		or not max_output <= max_total_output <= 10_000_000
		or not 1 <= max_input <= budget.DEFAULT_MAX_INPUT_TOKENS
	):
		raise SystemExit(2)
	if max_total_input is None:
		max_total_input = min(max_requests * max_input, budget.MAX_TOTAL_INPUT_TOKENS_CEILING)
	if not max_input <= max_total_input <= budget.MAX_TOTAL_INPUT_TOKENS_CEILING:
		raise SystemExit(2)
	try:
		max_prices = tuple(
			budget._parse_decimal(
				os.environ.get(f"MODEL_PROVIDER_BROKER_MAX_{field.upper()}_PRICE", "").strip()
				or BUDGET_DEFAULTS[f"MAX_{field.upper()}_PRICE"],
				field,
			)
			for field in budget.PRICE_FIELDS
		)
		policy = budget.BrokerPolicy(
			frozenset((model,)), max_output, max_total_output, max_prices, max_input, max_total_input,
		)
		configured_cost = os.environ.get("MODEL_PROVIDER_BROKER_MAX_TOTAL_COST_USD", "").strip()
		if configured_cost:
			max_total_cost = budget._parse_decimal(configured_cost, "max_total_cost_usd")
			if max_total_cost <= 0:
				raise SystemExit(2)
		else:
			max_total_cost = policy.estimate_cost_usd(
				max_total_input, max_total_output, max_requests * budget.MAX_IMAGE_INPUTS_PER_REQUEST,
			) + (max_requests - 1) * max_prices[2]
	except budget.BrokerRequestError:
		raise SystemExit(2) from None
	policy = budget.BrokerPolicy(
		frozenset((model,)), max_output, max_total_output, max_prices, max_input, max_total_input, max_total_cost,
	)
	state = budget.BrokerState(UPSTREAM_URL, "", "", max_requests, policy)
	state.module = budget
	return state


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
			self.path != (REVIEW_PATH if self.server.mode in ("review-broker", "review-bridge") else PROVIDER_PATH)
			or self.headers.get("Transfer-Encoding")
			or (
				self.headers.get("Authorization") != "Bearer isolated-placeholder"
				if self.server.mode in ("bridge", "review-bridge")
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
		brokered = self.server.mode in ("broker", "review-broker")
		if brokered:
			state = self.server.budget
			try:
				# The model check, token limits and provider price ceilings of
				# model_provider_broker.py; the normalized body is forwarded.
				body, output_tokens = state.policy.normalize_request(self.path, body)
				input_tokens = state.policy.estimate_input_tokens(body)
				image_inputs = state.module._count_image_inputs(body)
			except state.module.BrokerRequestError:
				return self._reject(400)
			if not state.reserve_request(output_tokens, input_tokens, image_inputs):
				return self._reject(429)
		connection = None
		headers_sent = False
		# Budget true-up, as in model_provider_broker.py: an observed upstream
		# error or a failure before forwarding settles at zero; an ambiguous
		# transport failure, cut-off or unreadable body keeps the reservation.
		upstream_started = False
		upstream_status = None
		content_type = ""
		response_tail = bytearray()
		response_bytes = 0
		body_complete = False
		try:
			if brokered:
				connection = http.client.HTTPSConnection("openrouter.ai", timeout=600, context=ssl.create_default_context())
				upstream_started = True
				connection.request("POST", self.path, body, {
					"Content-Type": "application/json",
					"Authorization": "Bearer " + self.server.api_key,
				})
			else:
				connection = UnixHTTPConnection(self.server.socket_path)
				connection.request("POST", self.path, body, {"Content-Type": "application/json"})
			response = connection.getresponse()
			upstream_status = response.status
			content_type = response.getheader("Content-Type", "application/json")
			self.send_response_only(response.status)
			self.send_header("Content-Type", content_type)
			self.send_header("Connection", "close")
			self.end_headers()
			headers_sent = True
			# read1 emits each SSE chunk promptly instead of waiting for 64 KiB.
			while True:
				chunk = response.read1(65536)
				if not chunk:
					body_complete = True
					break
				if brokered:
					response_bytes += len(chunk)
					response_tail += chunk
					if len(response_tail) > state.module.USAGE_SCAN_TAIL_BYTES:
						del response_tail[:-state.module.USAGE_SCAN_TAIL_BYTES]
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
			if brokered:
				actual_output = actual_input = None
				billable = True
				if not upstream_started or (upstream_status is not None and upstream_status >= 400):
					actual_output = actual_input = 0
					billable = False
				elif body_complete and (
					content_type.partition(";")[0].strip().lower() == "text/event-stream"
					or response_bytes <= state.module.USAGE_SCAN_TAIL_BYTES
				):
					actual_output, actual_input = state.module._extract_token_usage(bytes(response_tail), content_type)
				state.settle_request(
					output_tokens, actual_output, input_tokens, actual_input,
					billable=billable, reserved_image_inputs=image_inputs,
				)
			if connection is not None:
				connection.close()

	def do_GET(self):
		self._reject(405)

	def do_CONNECT(self):
		self._reject(405)


def main():
	if len(sys.argv) != 3 or sys.argv[1] not in ("broker", "bridge", "review-broker", "review-bridge"):
		raise SystemExit(2)
	if sys.argv[1] in ("broker", "review-broker"):
		key = os.environ.get("OPENROUTER_API_KEY", "")
		model = os.environ.get("CLARIFY_MODEL", "openai/gpt-6-sol")
		if not key or "\n" in key or "\r" in key or not model:
			raise SystemExit(2)
		budget = build_budget_state(model)
		server = UnixHTTPServer(sys.argv[2], Relay)
		server.mode = sys.argv[1]
		server.api_key = key
		server.model = model
		server.budget = budget
		os.chmod(sys.argv[2], 0o600)  # The container uses the host UID to connect.
	else:
		server = http.server.HTTPServer(("127.0.0.1", 8765), Relay)
		server.mode = sys.argv[1]
		server.socket_path = sys.argv[2]
	with server:
		server.serve_forever()


if __name__ == "__main__":
	main()
