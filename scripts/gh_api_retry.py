#!/usr/bin/env python3
"""Rate-limit-aware ``gh api`` retry helper — Python twin of ``gh_api_retry``.

``scripts/gh_helpers.sh`` defines the bash ``gh_api_retry`` function; this
module mirrors its contract for Python callers (issue #5873, re-issued as
#6634). Both twins share the classification fixture
``tests/fixtures/gh_api_retry/classify_cases.json``.

Contract:
  * Each attempt runs ``gh api -i <args>`` (without ``-i`` for
    ``--paginate``). Only a successful attempt's body reaches stdout.
  * Failure classes:
      primary    403/429 with ``x-ratelimit-remaining: 0`` → wait until
                 ``x-ratelimit-reset`` (bucket from ``x-ratelimit-resource``,
                 else the endpoint's core / graphql / search bucket)
      secondary  numeric ``retry-after``, a "secondary rate limit" /
                 "abuse detection" message, or a bare 429 → wait
                 ``retry-after`` seconds (60 when absent)
      transient  5xx, 408, or no status (network error) → exponential
                 backoff with jitter, capped at ``GH_RETRY_BACKOFF_CAP_SECS``
      permanent  any other 4xx and a non-limit 403 → no retry
  * No sleep after the last attempt; a rate-limit wait over
    ``GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS`` gives up at once.
  * A POST create makes one attempt unless ``idempotent=True``
    (``--idempotent`` / ``GH_RETRY_IDEMPOTENT=true``).
  * ``optional=True`` returns ``RC_RATE_LIMITED`` without calling GitHub
    while the breaker file has a future reset for the bucket.
  * Exit codes: 0 ok, 1 transient exhausted, 2 permanent, 75 rate-limited
    (gave up or optional skip).

``gh`` is always invoked as an argument list (never through a shell).
Response headers, bodies and tokens are never logged.

CLI: ``python3 scripts/gh_api_retry.py [--idempotent] [--optional] -- <gh api args>``
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

RC_OK = 0
RC_TRANSIENT = 1
RC_PERMANENT = 2
RC_RATE_LIMITED = 75

DEFAULT_BREAKER_FILE = "/tmp/.gh_rate_limit_circuit_breaker"

_FLAGS_WITH_VALUE = frozenset({
	"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input", "-H",
	"--header", "-q", "--jq", "-t", "--template", "--hostname", "-p",
	"--preview", "--cache",
})
_BUCKET_RE = re.compile(r"^[a-z_]+$")
_STATUS_LINE_RE = re.compile(rb"^HTTP/[0-9.]+ ([0-9]{3})")
_STDERR_STATUS_RE = re.compile(r"HTTP ([0-9]{3})")
_PERMANENT_TEXT_RE = re.compile(
	r"""['"][^'"]+['"] not found|gh: Not Found|HTTP 404|404 Not Found|status code 404|HTTP 422|"""
	r"""Resource not accessible by (integration|personal access token)""",
	re.IGNORECASE,
)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
	raw = os.environ.get(name, "")
	if raw.isdigit() and int(raw) >= minimum:
		return int(raw)
	return default


@dataclass(frozen=True)
class Classification:
	kind: str
	wait_secs: int
	bucket: str


@dataclass(frozen=True)
class GhApiResult:
	rc: int
	stdout: bytes
	kind: str
	status: int | None


def endpoint(args: Sequence[str]) -> str:
	skip = False
	for arg in args:
		if skip:
			skip = False
			continue
		if arg in _FLAGS_WITH_VALUE:
			skip = True
			continue
		if arg.startswith("-"):
			continue
		return arg
	return ""


def endpoint_bucket(args: Sequence[str]) -> str:
	ep = endpoint(args).lstrip("/")
	if ep == "graphql":
		return "graphql"
	if ep.startswith("search/"):
		return "search"
	return "core"


def graphql_doc_is_mutation(text: str) -> bool:
	"""First operation keyword, after whitespace and # comments, is "mutation"."""
	stripped = " ".join(line.split("#", 1)[0] for line in text.splitlines()).lstrip()
	return stripped.lower().startswith("mutation")


def _read_doc(path: str) -> str | None:
	if path == "-":
		return None
	try:
		with open(path, "rb") as handle:
			return handle.read(65536).decode("utf-8", "replace")
	except OSError:
		return None


def _input_query(path: str) -> str | None:
	text = _read_doc(path)
	if text is None:
		return None
	try:
		payload = json.loads(text)
	except ValueError:
		return None
	if not isinstance(payload, dict):
		return None
	query = payload.get("query") or ""
	return query if isinstance(query, str) else None


def is_unsafe_post(args: Sequence[str]) -> bool:
	"""True for a non-idempotent POST create (mirrors _gh_api_args_unsafe_post).

	A graphql document that cannot be read (missing file, stdin, invalid
	--input JSON) counts as a mutation, so it is not retried.
	"""
	method = ""
	fields = False
	mutation = False
	pending = ""
	input_path: str | None = None
	for arg in args:
		value = None
		if pending == "method":
			method = arg
			pending = ""
			continue
		if pending == "field":
			fields = True
			value = arg
			pending = ""
		elif pending in ("input", "skip"):
			if pending == "input":
				fields = True
				input_path = arg
			pending = ""
			continue
		if value is None:
			if arg in ("-X", "--method"):
				pending = "method"
				continue
			if arg.startswith("--method="):
				method = arg[len("--method="):]
				continue
			if arg.startswith("-X"):
				method = arg[2:]
				continue
			if arg in ("-f", "-F", "--field", "--raw-field"):
				pending = "field"
				continue
			if arg.startswith("--field="):
				fields, value = True, arg[len("--field="):]
			elif arg.startswith("--raw-field="):
				fields, value = True, arg[len("--raw-field="):]
			elif len(arg) > 2 and arg[:2] in ("-f", "-F"):
				fields, value = True, arg[2:]
			elif arg == "--input":
				pending = "input"
				continue
			elif arg.startswith("--input="):
				fields = True
				input_path = arg[len("--input="):]
				continue
			else:
				if arg in _FLAGS_WITH_VALUE:
					pending = "skip"
				continue
		if value.startswith("query=@"):
			doc = _read_doc(value[len("query=@"):])
			if doc is None or graphql_doc_is_mutation(doc):
				mutation = True
		elif value.startswith("query=") and value[len("query="):].lstrip().lower().startswith("mutation"):
			mutation = True
	method = method.upper()
	if method and method != "POST":
		return False
	if not method and not fields:
		return False
	if endpoint(args).lstrip("/") == "graphql":
		if input_path is not None:
			query = _input_query(input_path)
			if query is None or graphql_doc_is_mutation(query):
				mutation = True
		return mutation
	return True


def split_include_output(raw: bytes) -> tuple[int | None, dict[str, str], bytes]:
	"""Split ``gh api -i`` output at the FIRST blank line."""
	if not _STATUS_LINE_RE.match(raw):
		return None, {}, raw
	lines = raw.split(b"\n")
	for index, line in enumerate(lines):
		if line.rstrip(b"\r") == b"":
			head = lines[:index]
			body = b"\n".join(lines[index + 1:])
			break
	else:
		return None, {}, raw
	match = _STATUS_LINE_RE.match(head[0])
	status = int(match.group(1)) if match else None
	headers: dict[str, str] = {}
	for line in head[1:]:
		text = line.decode("utf-8", "replace").rstrip("\r")
		if ":" not in text:
			continue
		name, value = text.split(":", 1)
		headers.setdefault(name.strip().lower(), value.strip())
	return status, headers, body


def classify(
	status: int | None,
	headers: Mapping[str, str],
	stderr_text: str,
	body_text: str,
	*,
	now: float,
) -> Classification:
	"""Mirror of bash ``_gh_api_classify`` (wait -1 = read /rate_limit)."""
	headers = {k.lower(): v for k, v in headers.items()}
	text = stderr_text + body_text
	if status is None:
		match = _STDERR_STATUS_RE.search(stderr_text)
		status = int(match.group(1)) if match else None
	resource = headers.get("x-ratelimit-resource", "")
	bucket = resource if _BUCKET_RE.match(resource) else "-"
	reset = headers.get("x-ratelimit-reset", "")
	wait = -1
	if reset.isdigit():
		wait = max(1, int(reset) - int(now) + 1)
	retry_after = headers.get("retry-after", "")
	if status in (None, 403, 429):
		if headers.get("x-ratelimit-remaining", "") == "0" and status is not None:
			return Classification("primary", wait, bucket)
		if retry_after.isdigit() and status is not None:
			return Classification("secondary", int(retry_after), bucket)
		if re.search(r"secondary rate|abuse detection", text, re.IGNORECASE):
			return Classification("secondary", 60, bucket)
		if re.search(r"rate limit", text, re.IGNORECASE):
			return Classification("primary", wait, bucket)
		if status == 429:
			return Classification("secondary", 60, bucket)
		if status == 403 or _PERMANENT_TEXT_RE.search(text):
			return Classification("permanent", 0, bucket)
		return Classification("transient", 0, bucket)
	if status == 408 or 500 <= status <= 599:
		return Classification("transient", 0, bucket)
	return Classification("permanent", 0, bucket)


def _breaker_path(path: str | None) -> str:
	return path or os.environ.get("GH_RATE_LIMIT_BREAKER_FILE") or DEFAULT_BREAKER_FILE


def breaker_active(bucket: str, *, path: str | None = None, now: float | None = None) -> bool:
	current = int(time.time() if now is None else now)
	try:
		with open(_breaker_path(path), encoding="utf-8", errors="replace") as handle:
			for line in handle:
				parts = line.split()
				if len(parts) >= 2 and parts[0] == bucket and parts[1].isdigit() and int(parts[1]) > current:
					return True
	except OSError:
		return False
	return False


def trip_breaker(bucket: str, reset_epoch: int, *, low: bool = False, path: str | None = None) -> None:
	target = _breaker_path(path)
	try:
		with open(target, "a", encoding="utf-8") as handle:
			if _BUCKET_RE.match(bucket):
				handle.write(f"{bucket} {int(reset_epoch)}{' low' if low else ''}\n")
	except OSError:
		pass


def _log(outcome: str, kind: str, bucket: str, status: int | None, attempt: int, total: int, wait: int, ep: str) -> None:
	safe_ep = ep.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
	print(
		f"GH_API_RETRY outcome={outcome} kind={kind} bucket={bucket} "
		f"status={status if status is not None else 'none'} attempt={attempt}/{total} "
		f"wait_secs={wait} endpoint={safe_ep}",
		file=sys.stderr,
	)


def _rate_limit_reset(bucket: str, runner: Callable[..., subprocess.CompletedProcess]) -> int | None:
	if not _BUCKET_RE.match(bucket):
		bucket = "core"
	try:
		result = runner(
			["gh", "api", "rate_limit", "--jq", f".resources.{bucket}.reset // empty"],
			capture_output=True,
			check=False,
		)
	except OSError:
		return None
	text = (result.stdout or b"").decode("utf-8", "replace").strip()
	return int(text) if result.returncode == 0 and text.isdigit() else None


def call(
	args: Sequence[str],
	*,
	idempotent: bool = False,
	optional: bool = False,
	max_attempts: int | None = None,
	runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
	sleep: Callable[[float], None] = time.sleep,
	clock: Callable[[], float] = time.time,
) -> GhApiResult:
	args = list(args)
	if os.environ.get("GH_RETRY_IDEMPOTENT", "false") == "true":
		idempotent = True
	total = max_attempts if max_attempts is not None else _env_int("GH_RETRY_MAX_ATTEMPTS", 5, minimum=1)
	max_wait = _env_int("GH_RETRY_RATE_LIMIT_MAX_WAIT_SECS", 600)
	cap = _env_int("GH_RETRY_BACKOFF_CAP_SECS", 120)
	low = _env_int("GH_API_RETRY_LOW_BUDGET_REMAINING", 100)
	bucket = endpoint_bucket(args)
	ep = endpoint(args)
	paginate = "--paginate" in args
	if not idempotent and is_unsafe_post(args):
		total = 1
	if optional and breaker_active(bucket, now=clock()):
		_log("skipped", "breaker", bucket, None, 0, total, 0, ep)
		return GhApiResult(RC_RATE_LIMITED, b"", "skipped", None)
	attempt = 1
	status: int | None = None
	while attempt <= total:
		cmd = ["gh", "api", *args] if paginate else ["gh", "api", "-i", *args]
		try:
			result = runner(cmd, capture_output=True, check=False)
			rc, raw, err = result.returncode, result.stdout or b"", result.stderr or b""
		except OSError as exc:
			rc, raw, err = 1, b"", str(exc).encode()
		if paginate:
			status, headers, body = None, {}, raw
		else:
			status, headers, body = split_include_output(raw)
		if rc == 0:
			remaining = headers.get("x-ratelimit-remaining", "")
			reset = headers.get("x-ratelimit-reset", "")
			resource = headers.get("x-ratelimit-resource", "")
			if remaining.isdigit() and reset.isdigit() and int(remaining) < low:
				trip_breaker(resource if _BUCKET_RE.match(resource) else bucket, int(reset), low=True)
			return GhApiResult(RC_OK, body, "ok", status)
		err_text = err.decode("utf-8", "replace")
		cls = classify(status, headers, err_text, body.decode("utf-8", "replace"), now=clock())
		if status is None:
			match = _STDERR_STATUS_RE.search(err_text)
			status = int(match.group(1)) if match else None
		eff_bucket = cls.bucket if cls.bucket != "-" else bucket
		if cls.kind == "permanent":
			_log("permanent", "permanent", eff_bucket, status, attempt, total, 0, ep)
			return GhApiResult(RC_PERMANENT, b"", "permanent", status)
		if cls.kind in ("primary", "secondary"):
			now = int(clock())
			wait = cls.wait_secs
			if wait < 0:
				reset_epoch = _rate_limit_reset(eff_bucket, runner)
				wait = max(1, reset_epoch - now + 1) if reset_epoch is not None else 60
			trip_breaker(eff_bucket, now + wait)
			if attempt >= total or wait > max_wait:
				_log("gave_up", cls.kind, eff_bucket, status, attempt, total, wait, ep)
				return GhApiResult(RC_RATE_LIMITED, b"", cls.kind, status)
			_log("retry", cls.kind, eff_bucket, status, attempt, total, wait, ep)
			sleep(wait)
		else:
			if attempt >= total:
				_log("gave_up", "transient", eff_bucket, status, attempt, total, 0, ep)
				return GhApiResult(RC_TRANSIENT, b"", "transient", status)
			wait = min(cap, 2 * (2 ** (attempt - 1)) + random.randint(0, 2))
			_log("retry", "transient", eff_bucket, status, attempt, total, wait, ep)
			sleep(wait)
		attempt += 1
	return GhApiResult(RC_TRANSIENT, b"", "transient", status)


def main(argv: Sequence[str] | None = None) -> int:
	argv = list(sys.argv[1:] if argv is None else argv)
	idempotent = optional = False
	while argv and argv[0] in ("--idempotent", "--optional"):
		if argv.pop(0) == "--idempotent":
			idempotent = True
		else:
			optional = True
	if argv and argv[0] == "--":
		argv.pop(0)
	if not argv:
		print("usage: gh_api_retry.py [--idempotent] [--optional] -- <gh api args>", file=sys.stderr)
		return RC_PERMANENT
	result = call(argv, idempotent=idempotent, optional=optional)
	if result.rc == RC_OK:
		sys.stdout.buffer.write(result.stdout)
		sys.stdout.buffer.flush()
	return result.rc


if __name__ == "__main__":
	raise SystemExit(main())
