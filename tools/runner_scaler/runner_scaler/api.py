"""Minimal JSON-over-HTTPS client (stdlib only).

Every call has a timeout and returns a typed error; nothing here retries, so a
failing API costs one call per reconcile pass, never a tight loop. Error
messages never include the request URL or headers, because the Telegram bot
URL and the Authorization header carry secrets.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

DEFAULT_TIMEOUT = 20
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ApiError(Exception):
	"""A non-2xx response or a transport failure."""

	def __init__(self, status: int, message: str):
		super().__init__(f"HTTP {status}: {message}")
		self.status = status
		self.message = message


class RateLimited(ApiError):
	"""HTTP 429, or 403 with an exhausted rate-limit budget."""


Opener = Callable[..., Any]


def request_json(
	method: str,
	url: str,
	*,
	headers: dict[str, str],
	body: Any = None,
	timeout: int = DEFAULT_TIMEOUT,
	opener: Opener = urllib.request.urlopen,
) -> Any:
	data = None
	all_headers = {"Accept": "application/json", "User-Agent": "runner-scaler", **headers}
	if body is not None:
		data = json.dumps(body).encode("utf-8")
		all_headers["Content-Type"] = "application/json"
	request = urllib.request.Request(url, data=data, method=method, headers=all_headers)
	try:
		with opener(request, timeout=timeout) as response:
			raw = response.read(MAX_RESPONSE_BYTES + 1)
	except urllib.error.HTTPError as exc:
		status = exc.code
		remaining = (exc.headers or {}).get("x-ratelimit-remaining") if exc.headers is not None else None
		detail = _error_detail(exc)
		if status == 429 or (status == 403 and remaining == "0"):
			raise RateLimited(status, detail) from None
		raise ApiError(status, detail) from None
	except (urllib.error.URLError, TimeoutError, OSError) as exc:
		raise ApiError(0, type(exc).__name__) from None
	if len(raw) > MAX_RESPONSE_BYTES:
		raise ApiError(0, "response_too_large")
	if not raw:
		return None
	try:
		return json.loads(raw)
	except ValueError:
		raise ApiError(0, "invalid_json") from None


def _error_detail(exc: urllib.error.HTTPError) -> str:
	try:
		payload = json.loads(exc.read(4096) or b"{}")
	except (ValueError, OSError):
		return "error"
	if isinstance(payload, dict):
		message = payload.get("message") or payload.get("id") or "error"
		return str(message)[:160]
	return "error"
