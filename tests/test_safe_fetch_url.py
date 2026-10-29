#!/usr/bin/env python3
"""Tests for the synced safe-fetch helper (.claude/scripts/safe_fetch_url.py).

The helper ships to consumers through the `.claude/` sync
(`workflow-templates/.claude/scripts/safe_fetch_url.py`). The behaviour tests
below are ported unchanged from shubhodeep1/radateeree-resort.com
(`tests/test_safe_fetch_url.py`, Refs shubhodeep1/radateeree-resort.com#151);
only the module import differs. No test makes a network request: DNS and curl
are replaced by fakes.

The last tests check that the three synced command templates fetch external
URLs through the helper, using the exact text the consumer test looks for.
"""

from __future__ import annotations

import importlib.util
import socket
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SAFE_FETCH_TEMPLATE_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "scripts" / "safe_fetch_url.py"
SAFE_FETCH_COMMAND_TEMPLATES = tuple(
	REPO_ROOT / "workflow-templates" / ".claude" / "commands" / name
	for name in ("investigate-issue.md", "analyze-log.md", "apply-url.md")
)
SAFE_FETCH_COMMAND_LITERAL = "python scripts/safe_fetch_url.py"

_SAFE_FETCH_SPEC = importlib.util.spec_from_file_location("safe_fetch_url", SAFE_FETCH_TEMPLATE_PATH)
assert _SAFE_FETCH_SPEC is not None and _SAFE_FETCH_SPEC.loader is not None
safe_fetch_url = importlib.util.module_from_spec(_SAFE_FETCH_SPEC)
# Register before exec so the module's dataclasses can resolve their own module.
sys.modules[_SAFE_FETCH_SPEC.name] = safe_fetch_url
_SAFE_FETCH_SPEC.loader.exec_module(safe_fetch_url)


def _dns_record(address: str) -> tuple[object, object, object, str, tuple[object, ...]]:
	family = socket.AF_INET6 if ":" in address else socket.AF_INET
	return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))


class FakeResolver:
	def __init__(self, addresses_by_host: dict[str, list[str]]):
		self.addresses_by_host = addresses_by_host
		self.calls: list[tuple[str, int]] = []

	def __call__(self, hostname: str, port: int, **_kwargs: object):
		self.calls.append((hostname, port))
		return [_dns_record(address) for address in self.addresses_by_host[hostname]]


class FakeCurlRunner:
	def __init__(self, responses: Iterable[tuple[int, str | None, bytes, int]]):
		self.responses = iter(responses)
		self.calls: list[list[str]] = []

	def __call__(self, command: list[str], **_kwargs: object):
		self.calls.append(command)
		status, location, body, returncode = next(self.responses)
		body_path = Path(command[command.index("--output") + 1])
		header_path = Path(command[command.index("--dump-header") + 1])
		body_path.write_bytes(body)
		headers = [f"HTTP/1.1 {status} Test"]
		if location is not None:
			headers.append(f"Location: {location}")
		header_path.write_text("\r\n".join(headers) + "\r\n\r\n", encoding="iso-8859-1")
		return subprocess.CompletedProcess(command, returncode, stdout=str(status), stderr="")


def test_parse_accepts_and_canonicalizes_public_https_url():
	target = safe_fetch_url.parse_https_url("HTTPS://ExAmPle.COM:443/path?q=1#fragment")

	assert target.url == "https://example.com/path?q=1"
	assert target.hostname == "example.com"
	assert target.port == 443
	assert target.is_ip_literal is False


@pytest.mark.parametrize(
	"url",
	[
		"http://example.com/log",
		"https:///missing-host",
		"https://user:secret@example.com/log",
		"https://example.com:/log",
		"https://example.com:70000/log",
		"https://example.com\\@127.0.0.1/log",
		"https://bad_host.example/log",
	],
)
def test_parse_rejects_unsafe_or_malformed_urls(url: str):
	with pytest.raises(safe_fetch_url.FetchError):
		safe_fetch_url.parse_https_url(url)


@pytest.mark.parametrize(
	"url",
	[
		"https://127.0.0.1/",
		"https://10.0.0.1/",
		"https://169.254.169.254/latest/meta-data/",
		"https://224.0.0.1/",
		"https://240.0.0.1/",
		"https://[::1]/",
		"https://[fd00::1]/",
		"https://[fe80::1]/",
	],
)
def test_resolve_rejects_non_global_ip_literals(url: str):
	with pytest.raises(safe_fetch_url.FetchError, match="not globally routable"):
		safe_fetch_url.resolve_target(url)


def test_resolve_rejects_mixed_public_and_private_dns_results():
	resolver = FakeResolver({"example.com": ["8.8.8.8", "10.0.0.1"]})

	with pytest.raises(safe_fetch_url.FetchError, match="not globally routable"):
		safe_fetch_url.resolve_target("https://example.com/log", resolver=resolver)


def test_curl_command_pins_dns_and_disables_proxy_and_config(tmp_path: Path):
	resolver = FakeResolver({"example.com": ["2001:4860:4860::8888"]})
	target = safe_fetch_url.resolve_target("https://example.com/log", resolver=resolver)
	limits = safe_fetch_url.FetchLimits(max_bytes=99, connect_timeout=2.0, max_time=3.0)

	command = safe_fetch_url.build_curl_command(
		target,
		body_path=tmp_path / "body",
		header_path=tmp_path / "headers",
		limits=limits,
	)

	assert command[:2] == ["curl", "-q"]
	assert command[command.index("--resolve") + 1] == "example.com:443:[2001:4860:4860::8888]"
	assert command[command.index("--noproxy") + 1] == "*"
	assert command[command.index("--proto") + 1] == "=https"
	assert "--tlsv1.2" in command
	assert command[command.index("--max-filesize") + 1] == "99"
	assert command[command.index("--connect-timeout") + 1] == "2.0"
	assert command[command.index("--max-time") + 1] == "3.0"
	assert "--location" not in command
	assert "--globoff" in command


def test_public_redirect_and_relative_location_are_revalidated(tmp_path: Path):
	resolver = FakeResolver({"first.example": ["8.8.8.8"], "second.example": ["1.1.1.1"]})
	runner = FakeCurlRunner(
		[
			(302, "https://second.example/base/", b"redirect", 0),
			(307, "final.log", b"redirect", 0),
			(200, None, b"complete log", 0),
		]
	)
	output = tmp_path / "log.txt"

	status = safe_fetch_url.fetch_url(
		"https://first.example/start", output, resolver=resolver, runner=runner
	)

	assert status == 200
	assert output.read_bytes() == b"complete log"
	assert resolver.calls == [
		("first.example", 443),
		("second.example", 443),
		("second.example", 443),
	]
	assert len(runner.calls) == 3


def test_public_to_private_redirect_is_rejected_before_second_request(tmp_path: Path):
	resolver = FakeResolver({"public.example": ["8.8.8.8"], "internal.example": ["127.0.0.1"]})
	runner = FakeCurlRunner([(302, "https://internal.example/admin", b"redirect", 0)])
	output = tmp_path / "log.txt"

	with pytest.raises(safe_fetch_url.FetchError, match="not globally routable") as error:
		safe_fetch_url.fetch_url(
			"https://public.example/start", output, resolver=resolver, runner=runner
		)

	assert error.value.status == 302
	assert len(runner.calls) == 1
	assert not output.exists()


def test_unsafe_direct_url_removes_stale_output_without_network_request(tmp_path: Path):
	output = tmp_path / "log.txt"
	output.write_text("stale", encoding="utf-8")
	runner = FakeCurlRunner([])

	with pytest.raises(safe_fetch_url.FetchError, match="not globally routable"):
		safe_fetch_url.fetch_url("https://127.0.0.1/log", output, runner=runner)

	assert runner.calls == []
	assert not output.exists()


def test_redirect_loop_is_rejected(tmp_path: Path):
	resolver = FakeResolver({"public.example": ["8.8.8.8"]})
	runner = FakeCurlRunner(
		[
			(302, "/two", b"redirect", 0),
			(302, "/one", b"redirect", 0),
		]
	)

	with pytest.raises(safe_fetch_url.FetchError, match="redirect loop"):
		safe_fetch_url.fetch_url(
			"https://public.example/one",
			tmp_path / "log.txt",
			resolver=resolver,
			runner=runner,
		)


def test_redirect_limit_is_enforced(tmp_path: Path):
	resolver = FakeResolver({"public.example": ["8.8.8.8"]})
	runner = FakeCurlRunner([(302, "/two", b"redirect", 0)])
	limits = safe_fetch_url.FetchLimits(max_redirects=0)

	with pytest.raises(safe_fetch_url.FetchError, match="redirect limit"):
		safe_fetch_url.fetch_url(
			"https://public.example/one",
			tmp_path / "log.txt",
			limits=limits,
			resolver=resolver,
			runner=runner,
		)


def test_oversized_response_removes_stale_output_and_temporary_files(tmp_path: Path):
	resolver = FakeResolver({"public.example": ["8.8.8.8"]})
	runner = FakeCurlRunner([(200, None, b"too large", 0)])
	output = tmp_path / "log.txt"
	output.write_text("stale", encoding="utf-8")
	limits = safe_fetch_url.FetchLimits(max_bytes=3)

	with pytest.raises(safe_fetch_url.FetchError, match="maximum size"):
		safe_fetch_url.fetch_url(
			"https://public.example/log",
			output,
			limits=limits,
			resolver=resolver,
			runner=runner,
		)

	assert not output.exists()
	assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
def test_non_success_http_status_is_reported_without_publishing_body(tmp_path: Path, status: int):
	resolver = FakeResolver({"public.example": ["8.8.8.8"]})
	runner = FakeCurlRunner([(status, None, b"sensitive error body", 0)])
	output = tmp_path / "log.txt"

	with pytest.raises(safe_fetch_url.FetchError) as error:
		safe_fetch_url.fetch_url(
			"https://public.example/log", output, resolver=resolver, runner=runner
		)

	assert error.value.status == status
	assert not output.exists()


def test_success_atomically_replaces_existing_output(tmp_path: Path):
	resolver = FakeResolver({"public.example": ["8.8.8.8"]})
	runner = FakeCurlRunner([(200, None, b"new payload", 0)])
	output = tmp_path / "log.txt"
	output.write_text("stale", encoding="utf-8")

	safe_fetch_url.fetch_url("https://public.example/log", output, resolver=resolver, runner=runner)

	assert output.read_bytes() == b"new payload"
	assert list(tmp_path.iterdir()) == [output]


def test_cli_prints_received_status_on_failure(monkeypatch: pytest.MonkeyPatch, capsys):
	def fail_fetch(*_args: object, **_kwargs: object):
		raise safe_fetch_url.FetchError("request failed", status=429)

	monkeypatch.setattr(safe_fetch_url, "fetch_url", fail_fetch)

	exit_code = safe_fetch_url.main(["--url", "https://example.com/", "--output", "log"])

	output = capsys.readouterr()
	assert exit_code == 1
	assert output.out == "429\n"
	assert output.err == "safe_fetch_url: request failed\n"


def test_cli_rejects_unbounded_limit_values():
	with pytest.raises(SystemExit):
		safe_fetch_url.parse_args(
			["--url", "https://example.com/", "--output", "log", "--max-bytes", "0"]
		)
	with pytest.raises(SystemExit):
		safe_fetch_url.parse_args(
			[
				"--url",
				"https://example.com/",
				"--output",
				"log",
				"--max-redirects",
				"21",
			]
		)


def test_live_copy_matches_template():
	live_copy = REPO_ROOT / ".claude" / "scripts" / "safe_fetch_url.py"
	assert live_copy.read_bytes() == SAFE_FETCH_TEMPLATE_PATH.read_bytes()


@pytest.mark.parametrize("command_file", SAFE_FETCH_COMMAND_TEMPLATES, ids=lambda path: path.name)
def test_command_templates_use_safe_fetch_helper_for_external_urls(command_file: Path):
	command_text = command_file.read_text(encoding="utf-8")
	assert SAFE_FETCH_COMMAND_LITERAL in command_text
	# The helper is synced to .claude/scripts/, so every call runs from .claude.
	assert command_text.count(SAFE_FETCH_COMMAND_LITERAL) == command_text.count(
		"(cd .claude && " + SAFE_FETCH_COMMAND_LITERAL
	)
	assert "curl --fail-with-body -sSL" not in command_text


def test_apply_url_bash_permission_names_the_helper():
	apply_url_text = SAFE_FETCH_COMMAND_TEMPLATES[2].read_text(encoding="utf-8")
	assert "only for `curl`/`pdftotext`/`agent-browser` fetches" not in apply_url_text
	bash_bullets = [line for line in apply_url_text.splitlines() if line.startswith("- **`Bash`**")]
	assert len(bash_bullets) == 1
	assert "safe_fetch_url.py" in bash_bullets[0]
