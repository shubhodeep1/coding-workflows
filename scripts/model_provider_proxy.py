#!/usr/bin/env python3
"""Loopback-only, budget-bounded OpenRouter proxy for isolated model processes."""

from __future__ import annotations

import argparse
import ctypes
import http.client
import http.server
import json
import os
import re
import ssl
import sys
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit


ALLOWED_METHOD_PATHS = {
	"GET": {"/models"},
	"POST": {"/chat/completions", "/responses", "/embeddings"},
}
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_MODELS_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_USAGE_SCAN_BYTES = 1024 * 1024
MODEL_PATTERN = re.compile(
	r"^[A-Za-z0-9][A-Za-z0-9._:+-]*(?:/[A-Za-z0-9][A-Za-z0-9._:+-]*)+$"
)


def _positive_int(value: object, label: str) -> int:
	if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
		raise ValueError(f"{label} must be a positive integer")
	return value


def _nonnegative_decimal(value: object, label: str) -> Decimal:
	try:
		parsed = Decimal(str(value))
	except (InvalidOperation, ValueError) as exc:
		raise ValueError(f"{label} is invalid") from exc
	if not parsed.is_finite() or parsed < 0:
		raise ValueError(f"{label} is invalid")
	return parsed


def _normalized_provider_path(raw_path: str) -> str | None:
	parsed = urlsplit(raw_path)
	path = parsed.path
	if path.startswith("/api/v1/"):
		path = path[len("/api/v1") :]
	allowed_paths = ALLOWED_METHOD_PATHS["GET"] | ALLOWED_METHOD_PATHS["POST"]
	if path not in allowed_paths:
		return None
	return path + (("?" + parsed.query) if parsed.query else "")


@dataclass(frozen=True)
class ModelPolicy:
	model_id: str
	context_length: int
	max_completion_tokens: int
	prompt_price: Decimal
	completion_price: Decimal
	request_price: Decimal
	public_row: dict[str, object]


class ProxyAccounting:
	def __init__(self, max_requests: int, max_concurrency: int, max_spend: Decimal) -> None:
		self.max_requests = max_requests
		self.max_spend = max_spend
		self.request_count = 0
		self.reserved_spend = Decimal("0")
		self.settled_spend = Decimal("0")
		self.accounting_failed = False
		self.lock = threading.Lock()
		self.semaphore = threading.BoundedSemaphore(max_concurrency)

	def reserve(self, worst_case_cost: Decimal) -> bool:
		with self.lock:
			if self.accounting_failed or self.request_count >= self.max_requests:
				return False
			if self.settled_spend + self.reserved_spend + worst_case_cost > self.max_spend:
				return False
			self.request_count += 1
			self.reserved_spend += worst_case_cost
			return True

	def settle(self, reservation: Decimal, actual_cost: Decimal | None) -> None:
		with self.lock:
			self.reserved_spend = max(Decimal("0"), self.reserved_spend - reservation)
			if actual_cost is None or actual_cost < 0 or self.settled_spend + actual_cost > self.max_spend:
				self.accounting_failed = True
				return
			self.settled_spend += actual_cost


def _provider_connection() -> http.client.HTTPSConnection:
	return http.client.HTTPSConnection(
		"openrouter.ai", 443, timeout=300, context=ssl.create_default_context()
	)


def _provider_headers(credential: str, content_type: str = "application/json") -> dict[str, str]:
	return {
		"Authorization": f"Bearer {credential}",
		"Accept": "application/json",
		"Content-Type": content_type,
		"User-Agent": "coding-workflows-isolated-agent/1",
	}


def _load_model_policies(credential: str, allowed_models: set[str]) -> dict[str, ModelPolicy]:
	connection = _provider_connection()
	try:
		connection.request("GET", "/api/v1/models", headers=_provider_headers(credential))
		response = connection.getresponse()
		if response.status != 200:
			raise ValueError("provider model metadata request failed")
		body = response.read(MAX_MODELS_RESPONSE_BYTES + 1)
		if len(body) > MAX_MODELS_RESPONSE_BYTES:
			raise ValueError("provider model metadata response is too large")
		payload = json.loads(body)
	finally:
		connection.close()
	if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
		raise ValueError("provider model metadata is malformed")
	policies: dict[str, ModelPolicy] = {}
	for row in payload["data"]:
		if not isinstance(row, dict) or row.get("id") not in allowed_models:
			continue
		model_id = str(row["id"])
		pricing = row.get("pricing")
		top_provider = row.get("top_provider")
		if not isinstance(pricing, dict) or not isinstance(top_provider, dict):
			continue
		try:
			context_length = _positive_int(row.get("context_length"), "context length")
			completion_limit_value = top_provider.get("max_completion_tokens")
			if completion_limit_value is None:
				completion_limit_value = context_length
			max_completion_tokens = _positive_int(completion_limit_value, "completion limit")
			prompt_price = _nonnegative_decimal(pricing.get("prompt"), "prompt price")
			completion_price = _nonnegative_decimal(pricing.get("completion"), "completion price")
			request_price = _nonnegative_decimal(pricing.get("request", "0"), "request price")
		except ValueError:
			continue
		policies[model_id] = ModelPolicy(
			model_id=model_id,
			context_length=context_length,
			max_completion_tokens=max_completion_tokens,
			prompt_price=prompt_price,
			completion_price=completion_price,
			request_price=request_price,
			public_row=row,
		)
	missing = sorted(allowed_models - set(policies))
	if missing:
		raise ValueError("allowlisted model metadata is unavailable")
	return policies


def _request_models(payload: dict[str, object]) -> list[str]:
	models: list[str] = []
	primary = payload.get("model")
	if isinstance(primary, str):
		models.append(primary.removeprefix("openrouter/"))
	fallbacks = payload.get("models")
	if fallbacks is not None:
		if not isinstance(fallbacks, list) or any(not isinstance(item, str) for item in fallbacks):
			raise ValueError("models must be an array of strings")
		models.extend(item.removeprefix("openrouter/") for item in fallbacks)
	if not models:
		raise ValueError("model selection is required")
	return list(dict.fromkeys(models))


def _prepare_request(
	provider_path: str,
	request_body: bytes,
	model_policies: dict[str, ModelPolicy],
	proxy_output_limit: int,
) -> tuple[bytes, Decimal]:
	try:
		payload = json.loads(request_body)
	except (UnicodeDecodeError, json.JSONDecodeError) as exc:
		raise ValueError("request body must be a JSON object") from exc
	if not isinstance(payload, dict):
		raise ValueError("request body must be a JSON object")
	if payload.get("plugins") not in (None, []):
		raise ValueError("provider plugins are not permitted")
	requested_models = _request_models(payload)
	if any(model_id not in model_policies for model_id in requested_models):
		raise PermissionError("requested model is not allowlisted")
	policies = [model_policies[model_id] for model_id in requested_models]
	if len(request_body) > min(policy.context_length for policy in policies) * 4:
		raise ValueError("request exceeds the trusted model context bound")
	model_output_limit = min(policy.max_completion_tokens for policy in policies)
	effective_output_limit = min(proxy_output_limit, model_output_limit)
	path_only = provider_path.split("?", 1)[0]
	if path_only == "/chat/completions":
		token_fields = ("max_tokens", "max_completion_tokens")
	elif path_only == "/responses":
		token_fields = ("max_output_tokens",)
	else:
		token_fields = ()
	requested_output = effective_output_limit
	for token_field in token_fields:
		if token_field not in payload:
			continue
		requested_output = _positive_int(payload[token_field], token_field)
		if requested_output > effective_output_limit:
			raise ValueError("requested output token limit exceeds proxy policy")
	if token_fields and not any(field in payload for field in token_fields):
		payload[token_fields[-1]] = effective_output_limit
	usage_options = payload.get("usage")
	if usage_options is None:
		payload["usage"] = {"include": True}
	elif isinstance(usage_options, dict):
		usage_options["include"] = True
	else:
		raise ValueError("usage must be an object")
	worst_case_cost = max(
		policy.request_price
		+ Decimal(policy.context_length) * policy.prompt_price
		+ Decimal(requested_output) * policy.completion_price
		for policy in policies
	)
	return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"), worst_case_cost


def _usage_cost_from_tail(response_tail: bytes) -> Decimal | None:
	candidates: list[object] = []
	try:
		candidates.append(json.loads(response_tail))
	except (UnicodeDecodeError, json.JSONDecodeError):
		try:
			decoded_tail = response_tail.decode("utf-8")
		except UnicodeDecodeError:
			decoded_tail = ""
		usage_offsets = list(re.finditer(r'"usage"\s*:\s*', decoded_tail))
		decoder = json.JSONDecoder()
		for usage_match in reversed(usage_offsets):
			try:
				usage_payload, _end = decoder.raw_decode(decoded_tail[usage_match.end() :])
			except json.JSONDecodeError:
				continue
			if isinstance(usage_payload, dict):
				candidates.append({"usage": usage_payload})
				break
		for raw_line in response_tail.splitlines():
			if not raw_line.startswith(b"data:"):
				continue
			encoded = raw_line[5:].strip()
			if encoded in {b"", b"[DONE]"}:
				continue
			try:
				candidates.append(json.loads(encoded))
			except (UnicodeDecodeError, json.JSONDecodeError):
				continue
	for candidate in reversed(candidates):
		if not isinstance(candidate, dict) or not isinstance(candidate.get("usage"), dict):
			continue
		try:
			return _nonnegative_decimal(candidate["usage"].get("cost"), "usage cost")
		except ValueError:
			return None
	return None


class ProviderProxy(http.server.BaseHTTPRequestHandler):
	server_version = "model-provider-proxy/2"
	protocol_version = "HTTP/1.1"

	def log_message(self, format_string: str, *args: object) -> None:
		print(f"model_provider_proxy: {format_string % args}", file=sys.stderr)

	def do_GET(self) -> None:  # noqa: N802
		if self.path == "/healthz":
			self._send_json(200, {"status": "ok"})
			return
		if self.path in {"/models", "/api/v1/models"}:
			accounting: ProxyAccounting = self.server.accounting  # type: ignore[attr-defined]
			if not accounting.semaphore.acquire(blocking=False):
				self.send_error(429, "provider concurrency limit reached")
				return
			try:
				if not accounting.reserve(Decimal("0")):
					self.send_error(429, "provider request budget exhausted")
					return
				self._send_json(
					200,
					{"data": [policy.public_row for policy in self.server.model_policies.values()]},  # type: ignore[attr-defined]
				)
				accounting.settle(Decimal("0"), Decimal("0"))
			finally:
				accounting.semaphore.release()
			return
		self.send_error(403, "provider route is not allowlisted")

	def do_POST(self) -> None:  # noqa: N802
		self._proxy_request()

	def _send_json(self, status: int, payload: object) -> None:
		body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def _proxy_request(self) -> None:
		self.close_connection = True
		provider_path = _normalized_provider_path(self.path)
		if provider_path is None or provider_path.split("?", 1)[0] not in ALLOWED_METHOD_PATHS["POST"]:
			self.send_error(403, "provider route is not allowlisted")
			return
		try:
			content_length = int(self.headers.get("Content-Length", "0"))
		except ValueError:
			self.send_error(400, "invalid content length")
			return
		if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
			self.send_error(413, "request body size is invalid")
			return
		request_body = self.rfile.read(content_length)
		try:
			prepared_body, reservation = _prepare_request(
				provider_path,
				request_body,
				self.server.model_policies,  # type: ignore[attr-defined]
				self.server.max_output_tokens,  # type: ignore[attr-defined]
			)
		except PermissionError:
			self.send_error(403, "requested model is not allowlisted")
			return
		except ValueError:
			self.send_error(400, "request violates proxy policy")
			return
		accounting: ProxyAccounting = self.server.accounting  # type: ignore[attr-defined]
		if not accounting.semaphore.acquire(blocking=False):
			self.send_error(429, "provider concurrency limit reached")
			return
		reserved = False
		try:
			reserved = accounting.reserve(reservation)
			if not reserved:
				self.send_error(429, "provider request or spend budget exhausted")
				return
			self._forward_provider_request(provider_path, prepared_body, reservation)
		finally:
			accounting.semaphore.release()

	def _forward_provider_request(self, provider_path: str, request_body: bytes, reservation: Decimal) -> None:
		connection = _provider_connection()
		response_tail = bytearray()
		actual_cost: Decimal | None = None
		try:
			connection.request(
				self.command,
				f"/api/v1{provider_path}",
				request_body,
				_provider_headers(
					self.server.provider_credential,  # type: ignore[attr-defined]
					self.headers.get("Content-Type", "application/json"),
				),
			)
			response = connection.getresponse()
			self.send_response(response.status)
			for header_name, header_value in response.getheaders():
				if header_name.lower() in {
					"connection", "keep-alive", "proxy-authenticate",
					"proxy-authorization", "transfer-encoding", "upgrade",
				}:
					continue
				self.send_header(header_name, header_value)
			self.end_headers()
			while chunk := response.read(64 * 1024):
				self.wfile.write(chunk)
				self.wfile.flush()
				response_tail.extend(chunk)
				if len(response_tail) > MAX_USAGE_SCAN_BYTES:
					del response_tail[:-MAX_USAGE_SCAN_BYTES]
			actual_cost = _usage_cost_from_tail(bytes(response_tail))
		except (OSError, http.client.HTTPException):
			if not self.wfile.closed:
				try:
					self.send_error(502, "provider request failed")
				except OSError:
					pass
		finally:
			connection.close()
			self.server.accounting.settle(reservation, actual_cost)  # type: ignore[attr-defined]


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--credential-file", required=True)
	parser.add_argument("--ready-file", required=True)
	parser.add_argument("--bind", default="127.0.0.2")
	parser.add_argument("--allowed-model", action="append", default=[])
	parser.add_argument("--max-requests", type=int, default=64)
	parser.add_argument("--max-concurrency", type=int, default=1)
	parser.add_argument("--max-output-tokens", type=int, default=65536)
	parser.add_argument("--max-spend-usd", default="25")
	args = parser.parse_args()
	allowed_models = set(args.allowed_model)
	if not allowed_models or any(MODEL_PATTERN.fullmatch(model_id) is None for model_id in allowed_models):
		raise SystemExit("provider model allowlist is empty or malformed")
	try:
		max_requests = _positive_int(args.max_requests, "max requests")
		max_concurrency = _positive_int(args.max_concurrency, "max concurrency")
		max_output_tokens = _positive_int(args.max_output_tokens, "max output tokens")
		max_spend = _nonnegative_decimal(args.max_spend_usd, "max spend")
	except ValueError as exc:
		raise SystemExit(str(exc)) from exc
	if max_spend <= 0:
		raise SystemExit("max spend must be positive")
	credential = Path(args.credential_file).read_text(encoding="utf-8").strip()
	if not credential or "\n" in credential or "\r" in credential:
		raise SystemExit("provider credential file is empty or malformed")
	if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:
		raise SystemExit("could not make provider proxy non-dumpable")
	os.environ.pop("OPENROUTER_API_KEY", None)
	try:
		model_policies = _load_model_policies(credential, allowed_models)
	except (OSError, http.client.HTTPException, json.JSONDecodeError, ValueError) as exc:
		raise SystemExit(f"provider policy initialization failed: {type(exc).__name__}") from exc
	server = http.server.ThreadingHTTPServer((args.bind, 0), ProviderProxy)
	server.provider_credential = credential  # type: ignore[attr-defined]
	server.model_policies = model_policies  # type: ignore[attr-defined]
	server.max_output_tokens = max_output_tokens  # type: ignore[attr-defined]
	server.accounting = ProxyAccounting(max_requests, max_concurrency, max_spend)  # type: ignore[attr-defined]
	ready_path = Path(args.ready_file)
	ready_path.write_text(
		json.dumps({"host": args.bind, "port": server.server_port}) + "\n",
		encoding="utf-8",
	)
	os.chmod(ready_path, 0o600)
	try:
		server.serve_forever(poll_interval=0.2)
	finally:
		server.server_close()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
