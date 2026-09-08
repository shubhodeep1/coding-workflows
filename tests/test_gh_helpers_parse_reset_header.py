#!/usr/bin/env python3
"""Tests for ``_parse_reset_header`` in scripts/gh_helpers.sh.

Background (tele-funtoken-msg-scoring AI Review run 34168241128,
2026-09-08): ``curl_gh_api`` computes its rate-limit wait from the header
dump of the 403 response. It only ever read ``X-RateLimit-Reset``, the
primary-window reset. GitHub answers *secondary* rate limits (abuse
detection) with ``Retry-After`` in seconds and expects the client to wait
that long; the primary reset on the same response can be up to an hour out,
so the helper slept the full 600 s cap in ``_sleep_until_reset`` instead of
the few seconds GitHub asked for. ``Retry-After`` is now authoritative when
present and numeric; everything else keeps the ``X-RateLimit-Reset`` path.
The only other ``Retry-After`` handling in the repo is the Python one in
``scripts/ai_labels.py``; this pins the shell path to the same contract.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"

PRIMARY_RESET_EPOCH = 1788827590


def _parse(tmp_path: Path, headers: str) -> tuple[str, str]:
	"""Source gh_helpers.sh and run _parse_reset_header on a header dump.

	Returns (stdout, stderr) with surrounding whitespace stripped.
	"""
	header_file = tmp_path / "headers.txt"
	header_file.write_text(headers, encoding="utf-8")
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	script = (
		"set -euo pipefail\n"
		f"source '{GH_HELPERS}'\n"
		f"_parse_reset_header '{header_file}'\n"
	)
	result = subprocess.run(
		["bash", "-c", script], env=env, capture_output=True, text=True, check=True,
	)
	return result.stdout.strip(), result.stderr.strip()


def test_primary_reset_is_returned_when_no_retry_after(tmp_path: Path) -> None:
	out, err = _parse(
		tmp_path,
		f"HTTP/2 403\r\nx-ratelimit-remaining: 0\r\nx-ratelimit-reset: {PRIMARY_RESET_EPOCH}\r\n\r\n",
	)
	assert out == str(PRIMARY_RESET_EPOCH)
	assert "Retry-After" not in err


def test_primary_reset_accepts_no_whitespace_after_colon(tmp_path: Path) -> None:
	out, _err = _parse(tmp_path, f"HTTP/2 403\r\nX-RateLimit-Reset:{PRIMARY_RESET_EPOCH}\r\n\r\n")
	assert out == str(PRIMARY_RESET_EPOCH)


def test_retry_after_seconds_win_over_primary_reset(tmp_path: Path) -> None:
	before = int(time.time())
	out, err = _parse(
		tmp_path,
		"HTTP/2 403\r\nretry-after: 45\r\n"
		f"x-ratelimit-reset: {PRIMARY_RESET_EPOCH}\r\n\r\n",
	)
	after = int(time.time())
	assert out.isdigit(), out
	assert before + 45 <= int(out) <= after + 45, (out, before, after)
	assert "Retry-After: 45s" in err


def test_retry_after_accepts_no_whitespace_after_colon(tmp_path: Path) -> None:
	before = int(time.time())
	out, _err = _parse(tmp_path, "HTTP/2 403\r\nRetry-After:45\r\n\r\n")
	assert out.isdigit(), out
	assert int(out) >= before + 45


def test_retry_after_trims_trailing_optional_whitespace(tmp_path: Path) -> None:
	before = int(time.time())
	out, _err = _parse(tmp_path, "HTTP/2 403\r\nRetry-After: 45 \t\r\n\r\n")
	assert out.isdigit(), out
	assert int(out) >= before + 45


def test_retry_after_header_name_is_case_insensitive(tmp_path: Path) -> None:
	before = int(time.time())
	out, _err = _parse(tmp_path, "HTTP/1.1 403 Forbidden\r\nRetry-After: 7\r\n\r\n")
	assert out.isdigit(), out
	assert int(out) >= before + 7


def test_non_numeric_retry_after_falls_back_to_primary_reset(tmp_path: Path) -> None:
	# RFC 9110 also allows an HTTP-date; GitHub sends seconds, but the
	# fallback must not turn a date into a bogus epoch or an arithmetic error.
	out, err = _parse(
		tmp_path,
		"HTTP/2 403\r\nretry-after: Wed, 21 Oct 2026 07:28:00 GMT\r\n"
		f"x-ratelimit-reset: {PRIMARY_RESET_EPOCH}\r\n\r\n",
	)
	assert out == str(PRIMARY_RESET_EPOCH)
	assert "Retry-After" not in err


def test_no_reset_headers_yields_empty_string(tmp_path: Path) -> None:
	# _sleep_until_reset treats an empty value as "fall back to 30 s".
	out, _err = _parse(tmp_path, "HTTP/2 403\r\ncontent-type: application/json\r\n\r\n")
	assert out == ""


def test_missing_header_file_yields_empty_string(tmp_path: Path) -> None:
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	script = (
		"set -euo pipefail\n"
		f"source '{GH_HELPERS}'\n"
		f"_parse_reset_header '{tmp_path / 'does-not-exist'}'\n"
	)
	result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == ""


def test_sleep_until_reset_log_uses_source_neutral_epoch_label() -> None:
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	script = (
		"set -euo pipefail\n"
		f"source '{GH_HELPERS}'\n"
		"sleep() { :; }\n"
		f"_sleep_until_reset {PRIMARY_RESET_EPOCH}\n"
	)
	result = subprocess.run(
		["bash", "-c", script], env=env, capture_output=True, text=True, check=True,
	)
	assert f"computed reset epoch: {PRIMARY_RESET_EPOCH}" in result.stderr
	assert "X-RateLimit-Reset:" not in result.stderr
