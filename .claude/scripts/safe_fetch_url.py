#!/usr/bin/env python3
"""Download an HTTPS URL without allowing access to non-public networks."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

DEFAULT_MAX_BYTES = 100 * 1024 * 1024
MAX_ALLOWED_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
MAX_ALLOWED_REDIRECTS = 20
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_MAX_TIME = 120.0
MAX_ALLOWED_TIMEOUT = 600.0
MAX_URL_LENGTH = 8192
MAX_HEADER_BYTES = 1024 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_HTTP_STATUS_RE = re.compile(r"^[1-5][0-9]{2}$")


class FetchError(RuntimeError):
	"""A safe-fetch failure suitable for a concise user-facing diagnostic."""

	def __init__(self, message: str, *, status: int | None = None):
		super().__init__(message)
		self.status = status


@dataclass(frozen=True)
class FetchLimits:
	max_bytes: int = DEFAULT_MAX_BYTES
	max_redirects: int = DEFAULT_MAX_REDIRECTS
	connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
	max_time: float = DEFAULT_MAX_TIME


@dataclass(frozen=True)
class ParsedTarget:
	url: str
	hostname: str
	port: int
	is_ip_literal: bool


@dataclass(frozen=True)
class ResolvedTarget:
	parsed: ParsedTarget
	pinned_ip: str


Resolver = Callable[..., list[tuple[object, object, object, str, tuple[object, ...]]]]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _validate_authority(parsed_url: SplitResult) -> None:
	authority = parsed_url.netloc
	if parsed_url.username is not None or parsed_url.password is not None:
		raise FetchError("URL credentials are not allowed")
	if authority.startswith("["):
		if re.fullmatch(r"\[[^\[\]]+\](?::[0-9]+)?", authority) is None:
			raise FetchError("URL authority is malformed")
		return
	if "[" in authority or "]" in authority or authority.count(":") > 1:
		raise FetchError("URL authority is malformed")
	if ":" in authority and not authority.rsplit(":", 1)[1].isdigit():
		raise FetchError("URL port is malformed")


def _normalize_hostname(hostname: str) -> tuple[str, bool]:
	if "%" in hostname:
		raise FetchError("URL hostname is malformed")
	try:
		address = ipaddress.ip_address(hostname)
	except ValueError:
		try:
			ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
		except UnicodeError as exc:
			raise FetchError("URL hostname is malformed") from exc
		if len(ascii_hostname) > 253 or not ascii_hostname:
			raise FetchError("URL hostname is malformed")
		if any(_DNS_LABEL_RE.fullmatch(label) is None for label in ascii_hostname.split(".")):
			raise FetchError("URL hostname is malformed")
		return ascii_hostname, False
	if not _is_globally_routable(address):
		raise FetchError("URL destination is not globally routable")
	return address.compressed, True


def parse_https_url(raw_url: str) -> ParsedTarget:
	"""Validate and canonicalize one URL before any DNS or network operation."""
	if not raw_url or len(raw_url) > MAX_URL_LENGTH:
		raise FetchError("URL is empty or too long")
	if "\\" in raw_url or any(
		ord(character) <= 32 or ord(character) == 127 for character in raw_url
	):
		raise FetchError("URL contains forbidden characters")
	try:
		parsed_url = urlsplit(raw_url)
	except ValueError as exc:
		raise FetchError("URL is malformed") from exc
	if parsed_url.scheme.lower() != "https":
		raise FetchError("only HTTPS URLs are allowed")
	if not parsed_url.netloc or parsed_url.hostname is None:
		raise FetchError("URL hostname is required")
	_validate_authority(parsed_url)
	try:
		port = parsed_url.port or 443
	except ValueError as exc:
		raise FetchError("URL port is invalid") from exc
	if not 1 <= port <= 65535:
		raise FetchError("URL port is invalid")
	hostname, is_ip_literal = _normalize_hostname(parsed_url.hostname)
	host_for_url = f"[{hostname}]" if ":" in hostname else hostname
	netloc = host_for_url if port == 443 else f"{host_for_url}:{port}"
	canonical_url = urlunsplit(("https", netloc, parsed_url.path or "/", parsed_url.query, ""))
	return ParsedTarget(canonical_url, hostname, port, is_ip_literal)


def _is_globally_routable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
	if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
		return _is_globally_routable(address.ipv4_mapped)
	return address.is_global and not any(
		(
			address.is_loopback,
			address.is_private,
			address.is_link_local,
			address.is_reserved,
			address.is_multicast,
			address.is_unspecified,
		)
	)


def _require_global_ip(raw_address: object) -> str:
	try:
		address = ipaddress.ip_address(str(raw_address))
	except ValueError as exc:
		raise FetchError("DNS returned an invalid address") from exc
	if not _is_globally_routable(address):
		raise FetchError("URL destination is not globally routable")
	return address.compressed


def resolve_target(raw_url: str, *, resolver: Resolver = socket.getaddrinfo) -> ResolvedTarget:
	"""Resolve a validated URL and reject the entire result set if any IP is unsafe."""
	parsed_target = parse_https_url(raw_url)
	if parsed_target.is_ip_literal:
		return ResolvedTarget(parsed_target, _require_global_ip(parsed_target.hostname))
	try:
		address_records = resolver(
			parsed_target.hostname,
			parsed_target.port,
			type=socket.SOCK_STREAM,
			proto=socket.IPPROTO_TCP,
		)
	except (OSError, UnicodeError) as exc:
		raise FetchError("DNS resolution failed") from exc
	if not address_records:
		raise FetchError("DNS resolution returned no addresses")
	validated_addresses: list[str] = []
	for address_record in address_records:
		try:
			raw_address = address_record[4][0]
		except (IndexError, TypeError) as exc:
			raise FetchError("DNS returned a malformed result") from exc
		validated_address = _require_global_ip(raw_address)
		if validated_address not in validated_addresses:
			validated_addresses.append(validated_address)
	return ResolvedTarget(parsed_target, validated_addresses[0])


def _resolve_argument(resolved_target: ResolvedTarget) -> str | None:
	if resolved_target.parsed.is_ip_literal:
		return None
	pinned_address = resolved_target.pinned_ip
	if ":" in pinned_address:
		pinned_address = f"[{pinned_address}]"
	return f"{resolved_target.parsed.hostname}:{resolved_target.parsed.port}:{pinned_address}"


def build_curl_command(
	resolved_target: ResolvedTarget,
	*,
	body_path: Path,
	header_path: Path,
	limits: FetchLimits,
) -> list[str]:
	"""Build a one-hop curl request pinned to the already-validated destination."""
	command = [
		"curl",
		"-q",
		"--silent",
		"--show-error",
		"--globoff",
		"--request",
		"GET",
		"--proto",
		"=https",
		"--proto-redir",
		"=https",
		"--tlsv1.2",
		"--noproxy",
		"*",
		"--connect-timeout",
		str(limits.connect_timeout),
		"--max-time",
		str(limits.max_time),
		"--max-filesize",
		str(limits.max_bytes),
		"--dump-header",
		str(header_path),
		"--output",
		str(body_path),
		"--write-out",
		"%{http_code}",
	]
	resolve_argument = _resolve_argument(resolved_target)
	if resolve_argument is not None:
		command.extend(("--resolve", resolve_argument))
	command.extend(("--url", resolved_target.parsed.url))
	return command


def _parse_http_status(stdout: str) -> int | None:
	status_text = stdout.strip()
	if _HTTP_STATUS_RE.fullmatch(status_text) is None:
		return None
	return int(status_text)


def _read_redirect_location(header_path: Path) -> str | None:
	try:
		if header_path.stat().st_size > MAX_HEADER_BYTES:
			raise FetchError("response headers exceed the maximum size")
		header_text = header_path.read_bytes().decode("iso-8859-1")
	except OSError as exc:
		raise FetchError("response headers could not be read") from exc
	header_blocks = re.split(r"\r?\n\r?\n", header_text)
	response_blocks = [
		block
		for block in header_blocks
		if block.splitlines() and block.splitlines()[0].startswith("HTTP/")
	]
	if not response_blocks:
		raise FetchError("response headers are malformed")
	location_values: list[str] = []
	for header_line in response_blocks[-1].splitlines()[1:]:
		if header_line[:1].isspace() and location_values:
			raise FetchError("redirect location is malformed")
		if ":" not in header_line:
			continue
		header_name, header_value = header_line.split(":", 1)
		if header_name.lower() == "location":
			location_values.append(header_value.strip())
	if len(location_values) > 1:
		raise FetchError("response contains multiple redirect locations")
	return location_values[0] if location_values else None


def _run_single_hop(
	resolved_target: ResolvedTarget,
	*,
	body_path: Path,
	header_path: Path,
	limits: FetchLimits,
	runner: CommandRunner,
) -> tuple[int | None, str | None]:
	body_path.write_bytes(b"")
	header_path.write_bytes(b"")
	command = build_curl_command(
		resolved_target,
		body_path=body_path,
		header_path=header_path,
		limits=limits,
	)
	try:
		completed_request = runner(
			command,
			capture_output=True,
			text=True,
			check=False,
			timeout=limits.max_time + 5.0,
		)
	except subprocess.TimeoutExpired as exc:
		raise FetchError("network request exceeded the time limit") from exc
	except OSError as exc:
		raise FetchError("curl could not be executed") from exc
	status = _parse_http_status(completed_request.stdout or "")
	try:
		body_size = body_path.stat().st_size
	except OSError as exc:
		raise FetchError("downloaded response could not be inspected", status=status) from exc
	if body_size > limits.max_bytes or completed_request.returncode == 63:
		raise FetchError("response exceeds the maximum size", status=status)
	if completed_request.returncode != 0:
		raise FetchError(
			f"network request failed (curl exit {completed_request.returncode})",
			status=status,
		)
	if status is None:
		raise FetchError("server did not return a valid HTTP status")
	try:
		location = _read_redirect_location(header_path)
	except FetchError as exc:
		raise FetchError(str(exc), status=status) from exc
	return status, location


def _create_temporary_path(output_path: Path, suffix: str) -> Path:
	file_descriptor, temporary_name = tempfile.mkstemp(
		prefix=f".{output_path.name}.", suffix=suffix, dir=output_path.parent
	)
	os.close(file_descriptor)
	return Path(temporary_name)


def fetch_url(
	raw_url: str,
	output_path: Path,
	*,
	limits: FetchLimits | None = None,
	resolver: Resolver = socket.getaddrinfo,
	runner: CommandRunner = subprocess.run,
) -> int:
	"""Fetch ``raw_url`` into ``output_path`` and return the final HTTP status."""
	effective_limits = limits or FetchLimits()
	destination = Path(output_path)
	if not destination.parent.is_dir():
		raise FetchError("output directory does not exist")
	if destination.exists() and destination.is_dir():
		raise FetchError("output path is a directory")
	try:
		destination.unlink(missing_ok=True)
	except OSError as exc:
		raise FetchError("existing output could not be removed") from exc
	body_path: Path | None = None
	header_path: Path | None = None
	visited_urls: set[str] = set()
	redirect_count = 0
	last_status: int | None = None
	try:
		resolved_target = resolve_target(raw_url, resolver=resolver)
		body_path = _create_temporary_path(destination, ".body")
		header_path = _create_temporary_path(destination, ".headers")
		while True:
			if resolved_target.parsed.url in visited_urls:
				raise FetchError("redirect loop detected", status=last_status)
			visited_urls.add(resolved_target.parsed.url)
			status, location = _run_single_hop(
				resolved_target,
				body_path=body_path,
				header_path=header_path,
				limits=effective_limits,
				runner=runner,
			)
			last_status = status
			if status in REDIRECT_STATUSES:
				if not location:
					raise FetchError("redirect response is missing a location", status=status)
				if redirect_count >= effective_limits.max_redirects:
					raise FetchError("redirect limit exceeded", status=status)
				next_url = urljoin(resolved_target.parsed.url, location)
				try:
					resolved_target = resolve_target(next_url, resolver=resolver)
				except FetchError as exc:
					raise FetchError(str(exc), status=status) from exc
				redirect_count += 1
				continue
			if not 200 <= status <= 299:
				raise FetchError(f"HTTP request failed with status {status}", status=status)
			try:
				os.replace(body_path, destination)
			except OSError as exc:
				raise FetchError(
					"downloaded response could not be finalized", status=status
				) from exc
			return status
	finally:
		if body_path is not None:
			body_path.unlink(missing_ok=True)
		if header_path is not None:
			header_path.unlink(missing_ok=True)


def _bounded_integer(value: str, *, minimum: int, maximum: int, label: str) -> int:
	try:
		parsed_value = int(value)
	except ValueError as exc:
		raise argparse.ArgumentTypeError(f"{label} must be an integer") from exc
	if not minimum <= parsed_value <= maximum:
		raise argparse.ArgumentTypeError(f"{label} must be between {minimum} and {maximum}")
	return parsed_value


def _bounded_float(value: str, *, maximum: float, label: str) -> float:
	try:
		parsed_value = float(value)
	except ValueError as exc:
		raise argparse.ArgumentTypeError(f"{label} must be a number") from exc
	if not 0 < parsed_value <= maximum:
		raise argparse.ArgumentTypeError(f"{label} must be greater than 0 and at most {maximum}")
	return parsed_value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--url", required=True, help="HTTPS URL to download")
	parser.add_argument("--output", required=True, type=Path, help="destination file")
	parser.add_argument(
		"--max-bytes",
		type=lambda value: _bounded_integer(
			value, minimum=1, maximum=MAX_ALLOWED_BYTES, label="max bytes"
		),
		default=DEFAULT_MAX_BYTES,
	)
	parser.add_argument(
		"--max-redirects",
		type=lambda value: _bounded_integer(
			value, minimum=0, maximum=MAX_ALLOWED_REDIRECTS, label="max redirects"
		),
		default=DEFAULT_MAX_REDIRECTS,
	)
	parser.add_argument(
		"--connect-timeout",
		type=lambda value: _bounded_float(
			value, maximum=MAX_ALLOWED_TIMEOUT, label="connect timeout"
		),
		default=DEFAULT_CONNECT_TIMEOUT,
	)
	parser.add_argument(
		"--max-time",
		type=lambda value: _bounded_float(value, maximum=MAX_ALLOWED_TIMEOUT, label="max time"),
		default=DEFAULT_MAX_TIME,
	)
	return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
	arguments = parse_args(argv)
	limits = FetchLimits(
		max_bytes=arguments.max_bytes,
		max_redirects=arguments.max_redirects,
		connect_timeout=arguments.connect_timeout,
		max_time=arguments.max_time,
	)
	try:
		status = fetch_url(arguments.url, arguments.output, limits=limits)
	except FetchError as exc:
		if exc.status is not None:
			print(exc.status)
		print(f"safe_fetch_url: {exc}", file=sys.stderr)
		return 1
	print(status)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
