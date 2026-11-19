#!/usr/bin/env python3
"""Tests for scripts/gh_api_retry.py, the Python twin of bash ``gh_api_retry``.

The classification fixture is shared with scripts/gh_helpers.sh: the parity
test runs every case through both ``classify()`` and bash
``_gh_api_classify`` and requires identical results (issue #5873 / #6634).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gh_api_retry as gar  # noqa: E402

GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"
CASES = json.loads((REPO_ROOT / "tests" / "fixtures" / "gh_api_retry" / "classify_cases.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	for name in list(os.environ):
		if name.startswith(("GH_RETRY", "GH_API_RETRY")):
			monkeypatch.delenv(name, raising=False)
	monkeypatch.setenv("GH_RATE_LIMIT_BREAKER_FILE", str(tmp_path / "breaker"))


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_classify_matches_fixture(case: dict) -> None:
	got = gar.classify(case["status"], case["headers"], case["stderr"], case["body"], now=case["now"])
	assert (got.kind, got.wait_secs, got.bucket) == (case["expected"]["kind"], case["expected"]["wait_secs"], case["expected"]["bucket"])


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_bash_classify_parity(case: dict, tmp_path: Path) -> None:
	hdr = tmp_path / "h"
	hdr.write_text("".join(f"{k}: {v}\n" for k, v in case["headers"].items()), encoding="utf-8")
	(tmp_path / "e").write_text(case["stderr"], encoding="utf-8")
	(tmp_path / "b").write_text(case["body"], encoding="utf-8")
	status = "" if case["status"] is None else str(case["status"])
	script = f"source '{GH_HELPERS}'; _gh_api_classify '{status}' '{hdr}' '{tmp_path}/e' '{tmp_path}/b'"
	env = dict(os.environ, GH_API_RETRY_NOW=str(case["now"]))
	out = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=True).stdout.split()
	expected = case["expected"]
	assert out == [expected["kind"], str(expected["wait_secs"]), expected["bucket"]]


@pytest.mark.parametrize(
	"args,unsafe",
	[
		(["-X", "POST", "repos/o/r/issues/1/comments", "-f", "body=x"], True),
		(["-XPOST", "repos/o/r/issues"], True),
		(["--method=post", "repos/o/r/issues"], True),
		(["repos/o/r/issues/1/comments", "-fbody=x"], True),
		(["repos/o/r/issues", "--input", "payload.json"], True),
		(["--method=GET", "repos/o/r/pulls", "-f", "state=open"], False),
		(["-X", "PATCH", "repos/o/r/issues/1", "-f", "state=closed"], False),
		(["repos/o/r/issues/1"], False),
		(["graphql", "-f", "query={ viewer { login } }"], False),
		(["graphql", "-f", "query=  mutation { x }"], True),
		(["graphql", "-f", "query=query A { a } mutation B { b }", "-f", "operationName=B"], True),
		(["graphql", "-f", "query={ repository { mutationCount } }"], False),
		(["graphql", "-F", "query=@does-not-exist.graphql"], True),
		(["graphql", "--input", "-"], True),
		(["repos/o/r/pulls", "--jq", ".[] | select(.x == \"-f\")"], False),
	],
)
def test_is_unsafe_post(args: list[str], unsafe: bool) -> None:
	assert gar.is_unsafe_post(args) is unsafe


def test_is_unsafe_post_reads_graphql_documents_from_files(tmp_path: Path) -> None:
	read_doc = tmp_path / "read.graphql"
	read_doc.write_text("# a comment\nquery { viewer { login } }\n", encoding="utf-8")
	mut_doc = tmp_path / "mut.graphql"
	mut_doc.write_text("# mutation in a comment only\n  mutation { x }\n", encoding="utf-8")
	read_input = tmp_path / "read.json"
	read_input.write_text(json.dumps({"query": "query { a }"}), encoding="utf-8")
	mut_input = tmp_path / "mut.json"
	mut_input.write_text(json.dumps({"query": "mutation { a }"}), encoding="utf-8")
	bad_input = tmp_path / "bad.json"
	bad_input.write_text("not json", encoding="utf-8")
	assert gar.is_unsafe_post(["graphql", "-F", f"query=@{read_doc}"]) is False
	assert gar.is_unsafe_post(["graphql", "-F", f"query=@{mut_doc}"]) is True
	assert gar.is_unsafe_post(["graphql", "--input", str(read_input)]) is False
	assert gar.is_unsafe_post(["graphql", f"--input={mut_input}"]) is True
	assert gar.is_unsafe_post(["graphql", "--input", str(bad_input)]) is True


def test_bucket() -> None:
	assert gar.endpoint_bucket(["graphql", "-f", "query=x"]) == "graphql"
	assert gar.endpoint_bucket(["--method", "GET", "/search/issues"]) == "search"
	assert gar.endpoint_bucket(["-H", "Accept: x", "repos/o/r"]) == "core"


def test_split_include_output_uses_first_blank_line() -> None:
	raw = b"HTTP/2.0 200 OK\r\nX-RateLimit-Remaining: 9\r\n\r\nbody\r\n\r\nmore"
	status, headers, body = gar.split_include_output(raw)
	assert status == 200
	assert headers == {"x-ratelimit-remaining": "9"}
	assert body == b"body\r\n\r\nmore"


class _Runner:
	def __init__(self, responses: list[tuple[int, bytes, bytes]]) -> None:
		self.responses = responses
		self.calls: list[list[str]] = []

	def __call__(self, cmd, **_kwargs):
		self.calls.append(cmd)
		if cmd[:3] == ["gh", "api", "rate_limit"]:
			return subprocess.CompletedProcess(cmd, 0, b"", b"")
		index = min(len([c for c in self.calls if c[:3] != ["gh", "api", "rate_limit"]]) - 1, len(self.responses) - 1)
		rc, out, err = self.responses[index]
		return subprocess.CompletedProcess(cmd, rc, out, err)


OK = (0, b"HTTP/2.0 200 OK\r\n\r\n{\"ok\":1}", b"")
BAD = (1, b"HTTP/2.0 502 Bad Gateway\r\n\r\nBODY-MARKER", b"gh: HTTP 502\n")


def test_call_returns_only_success_body() -> None:
	runner = _Runner([BAD, OK])
	sleeps: list[float] = []
	result = gar.call(["repos/o/r"], runner=runner, sleep=sleeps.append, clock=lambda: 1000.0)
	assert result.rc == gar.RC_OK
	assert result.stdout == b'{"ok":1}'
	assert len(sleeps) == 1
	assert runner.calls[0][:3] == ["gh", "api", "-i"]


def test_call_no_sleep_after_last_attempt() -> None:
	runner = _Runner([BAD])
	sleeps: list[float] = []
	result = gar.call(["repos/o/r"], max_attempts=3, runner=runner, sleep=sleeps.append)
	assert result.rc == gar.RC_TRANSIENT
	assert len(runner.calls) == 3
	assert len(sleeps) == 2


def test_call_post_create_single_attempt_unless_idempotent() -> None:
	runner = _Runner([BAD, OK])
	assert gar.call(["-X", "POST", "repos/o/r/issues"], runner=runner, sleep=lambda _s: None).rc == gar.RC_TRANSIENT
	assert len(runner.calls) == 1
	runner = _Runner([BAD, OK])
	assert gar.call(["-X", "POST", "repos/o/r/issues/1/labels"], idempotent=True, runner=runner, sleep=lambda _s: None).rc == gar.RC_OK


def test_call_rate_limit_beyond_cap_returns_75_without_sleep(tmp_path: Path) -> None:
	limited = (1, b"HTTP/2.0 403 Forbidden\r\nx-ratelimit-remaining: 0\r\nx-ratelimit-reset: 9999\r\nx-ratelimit-resource: graphql\r\n\r\n{}", b"")
	sleeps: list[float] = []
	result = gar.call(["graphql", "-f", "query={a}"], runner=_Runner([limited]), sleep=sleeps.append, clock=lambda: 1000.0)
	assert result.rc == gar.RC_RATE_LIMITED
	assert sleeps == []
	assert "graphql 10000" in (tmp_path / "breaker").read_text()


def test_call_optional_skips_while_breaker_active(tmp_path: Path) -> None:
	(tmp_path / "breaker").write_text("core 5000\n", encoding="utf-8")
	runner = _Runner([OK])
	result = gar.call(["repos/o/r"], optional=True, runner=runner, clock=lambda: 1000.0)
	assert result.rc == gar.RC_RATE_LIMITED
	assert runner.calls == []


def test_call_permanent_returns_2() -> None:
	runner = _Runner([(1, b"HTTP/2.0 404 Not Found\r\n\r\n{}", b"gh: Not Found (HTTP 404)\n"), OK])
	assert gar.call(["repos/o/r"], runner=runner).rc == gar.RC_PERMANENT
	assert len(runner.calls) == 1


def test_cli_usage_error() -> None:
	result = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "gh_api_retry.py")], capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
	assert result.returncode == gar.RC_PERMANENT
