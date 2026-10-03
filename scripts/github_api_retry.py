"""Shared, bounded retry decisions for GitHub REST/gh API reads.

None means permanent/unsafe to retry; callers must check their attempt budget
before sleeping. The response's own headers, not /rate_limit's header, govern
the wait. A reset beyond the job wait budget is left for scheduled recovery.
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Callable, Mapping


def retry_delay(
	status: int | None,
	headers: Mapping[str, str] | None,
	message: str,
	attempt: int,
	*,
	method: str = "GET",
	idempotent: bool = False,
	max_wait: float = 600.0,
	now: float | None = None,
) -> float | None:
	"""Return seconds to wait, or None if retrying would be unsafe.

	A rate limit without a usable reset waits at least 60 seconds. Other
	transients use jittered exponential backoff. No untrusted header value
	can force a sleep longer than max_wait.
	"""
	if method.upper() != "GET" and not idempotent:
		return None
	if status in (404, 422) or "resource not accessible by" in message.lower():
		return None
	if status not in (403, 429) and (status is not None and status < 500):
		return None
	parsed_headers = {str(key).lower(): str(value).strip() for key, value in (headers or {}).items()}
	limited = status == 429 or (status == 403 and (
		"rate limit" in message.lower() or parsed_headers.get("x-ratelimit-remaining") == "0"
		or "retry-after" in parsed_headers
	))
	if status == 403 and not limited:
		return None
	if limited:
		retry_after = parsed_headers.get("retry-after", "")
		reset = parsed_headers.get("x-ratelimit-reset", "")
		if retry_after.isdecimal() and len(retry_after) > 10:
			return None
		if re.fullmatch(r"[0-9]{1,10}", retry_after):
			wait = float(retry_after)
		elif parsed_headers.get("x-ratelimit-remaining") == "0" and re.fullmatch(r"[0-9]{1,12}", reset):
			wait = float(reset) - (time.time() if now is None else now) + 1
		else:
			wait = 60.0
		wait = max(1.0, wait)
	else:
		wait = min(120.0, 2 ** min(attempt, 7) + random.uniform(0, 1))
	return wait if wait <= max_wait else None


def gh_failure_delay(
	stdout: str,
	stderr: str,
	attempt: int,
	*,
	max_wait: float = 600.0,
	endpoint: str = "",
	rate_limit_probe: Callable[[], str] | None = None,
) -> float | None:
	"""Classify a failed gh CLI GET without treating an error body as data."""
	message = f"{stderr}\n{stdout}"
	match = re.search(r"\b(?:HTTP |status code )(\d{3})\b", message, re.IGNORECASE)
	status = int(match.group(1)) if match else None
	if status is None:
		if "rate limit" in message.lower() or "abuse detection" in message.lower():
			status = 429
		elif "bad gateway" in message.lower():
			status = 502
		elif not any(term in message.lower() for term in ("timeout", "connection", "temporarily unavailable")):
			return None
	probe_headers: dict[str, str] = {}
	if status in (403, 429) and "rate limit" in message.lower() and "secondary" not in message.lower() and rate_limit_probe is not None:
		# The CLI returned only stderr. Probe the *body* of /rate_limit once
		# on this failure, not its own response headers. A missing bucket is
		# not permission to retry before the exhausted window resets.
		try:
			bucket_payload = json.loads(rate_limit_probe())
			bucket_name = "graphql" if endpoint == "graphql" else "search" if endpoint.startswith("search/") else "core"
			bucket_reset = bucket_payload["resources"][bucket_name]["reset"]
			if type(bucket_reset) is not int or bucket_reset < 1:
				return None
			probe_headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(bucket_reset)}
		except (TypeError, ValueError, KeyError):
			return None
	return retry_delay(status, probe_headers, message, attempt, max_wait=max_wait)
