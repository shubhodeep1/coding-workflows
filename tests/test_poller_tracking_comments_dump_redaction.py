#!/usr/bin/env python3
"""The poller no longer dumps raw tracking-issue comments on a validation failure.

Background (SEC-002 in analysis/workflow-optimization-2026-10-06-2.md, issue
#8944, follow-up to #8095): when the paginated tracking-issue comments response
failed JSON validation, scripts/orchestrate_poll_process.sh printed the first
50 lines of it (user-controlled comment bodies and orchestrator state) into the
Actions log inside a ``::group::``. A line starting with ``::`` in a comment
body was read as a workflow command.

The block now prints one ``::warning::Comments response summary ...`` line
with ``bytes=``, a 12-hex ``sha256=`` prefix and a ``category=`` enum, the same
shape gh_api_json_to_file in scripts/gh_helpers.sh logs. These tests pin the
text of the block and run it in bash against hostile fixtures.

CI runs these cases through tests/test_gh_retry_final_stderr_redaction.py,
which collects them (ci.yml is outside the file grant of the issue).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"

FAILED_VALIDATION_LINE = (
	'echo "::warning::Comments JSON for issue #${TRACKING_NUM} failed validation; '
	'proceeding with empty list" >&2'
)
SLICE_START = '_comments_raw="$(mktemp "${TMPDIR:-/tmp}/comments_raw.XXXXXX")"'
SLICE_END = 'rm -f "${_comments_raw}"'
BLOCK_END = 'rm -f "${_comments_merged}"'

SUMMARY_RE = re.compile(
	r"^::warning::Comments response summary for issue #123: "
	r"bytes=(?P<bytes>\d+) sha256=(?P<sha>[0-9a-f]{12}|unavailable) "
	r"category=(?P<cat>empty|invalid_json|non_array)$"
)

SENTINEL = "SENTINEL_COMMENT_BODY"
HOSTILE_TRUNCATED = (
	'[{"id":1,"body":"' + SENTINEL + '\\n::add-mask::leak"},\n'
	"::add-mask::leak\n"
	'{"id":2,"body":"' + SENTINEL + ' unfinished'
).encode("utf-8")
HOSTILE_OBJECT = ('{"body":"' + SENTINEL + ' ::add-mask::leak"}').encode("utf-8")


def _source() -> str:
	return POLLER.read_text(encoding="utf-8")


def _failure_block(source: str) -> str:
	assert source.count(FAILED_VALIDATION_LINE) == 1
	start = source.index(FAILED_VALIDATION_LINE)
	end = source.index(BLOCK_END, start)
	return source[start:end]


def _fetch_slice(source: str) -> str:
	assert source.count(SLICE_START) == 1
	assert source.count(SLICE_END) == 1
	start = source.index(SLICE_START)
	end = source.index(SLICE_END, start) + len(SLICE_END)
	return source[start:end]


def test_raw_comments_dump_is_gone() -> None:
	source = _source()
	assert 'head -50 "${_comments_raw}"' not in source
	assert "Raw comments response (first 50 lines)" not in source


def test_failure_block_emits_bounded_summary() -> None:
	block = _failure_block(_source())
	for needle in (
		"bytes=",
		"sha256=",
		"category=",
		"Comments response summary for issue #${TRACKING_NUM}",
		"sha256sum",
		"shasum -a 256",
		"cut -c1-12",
		"^[0-9a-f]{12}$",
	):
		assert needle in block, needle
	assert "head" not in block
	assert 'cat "${_comments_raw}"' not in block
	assert "::group::" not in block
	# A rename of the summary variables must fail here.
	assert block.count("_cmt_dump_") >= 12


def test_proceeding_with_empty_list_warning_kept() -> None:
	assert FAILED_VALIDATION_LINE in _source()


def _run_fetch(tmp_path: Path, fixture: bytes, extra: str = "") -> tuple[subprocess.CompletedProcess[str], Path]:
	if shutil.which("jq") is None:
		# In CI a missing jq must not turn the redaction cases into a green skip.
		if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
			pytest.fail("jq required for the redaction tests in CI")
		pytest.skip("jq not on PATH")
	work = tmp_path / "tmp"
	work.mkdir()
	fixture_file = tmp_path / "fixture"
	fixture_file.write_bytes(fixture)
	script = (
		"set -euo pipefail\n"
		'gh_retry_to_file() { cp "${FIXTURE}" "$1"; }\n'
		f"{extra}\n"
		f"{_fetch_slice(_source())}\n"
		"printf 'COMMENTS=%s\\nCOMMENTS_FETCH_OK=%s\\n' \"${COMMENTS}\" \"${COMMENTS_FETCH_OK}\"\n"
	)
	env = dict(os.environ)
	env.update(
		{
			"TMPDIR": str(work),
			"TRACKING_NUM": "123",
			"GITHUB_REPOSITORY": "o/r",
			"FIXTURE": str(fixture_file),
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	result = subprocess.run(
		["bash", "-c", script],
		env=env,
		capture_output=True,
		text=True,
		errors="replace",
		timeout=60,
	)
	return result, work


# Real jq accepts an empty response as [] (jq -s 'add // []'), so the empty
# category is only reached when jq itself fails; the stub simulates that.
FAILING_JQ = "jq() { return 5; }"

FAILURE_CASES = {
	"truncated_json": (HOSTILE_TRUNCATED, "", "invalid_json"),
	"object_page": (HOSTILE_OBJECT, "", "non_array"),
	"empty_response_jq_failure": (b"", FAILING_JQ, "empty"),
}


@pytest.mark.parametrize("case", sorted(FAILURE_CASES))
def test_failure_logs_only_summary(tmp_path: Path, case: str) -> None:
	fixture, extra, category = FAILURE_CASES[case]
	result, work = _run_fetch(tmp_path, fixture, extra)

	assert result.returncode == 0, result.stderr
	assert SENTINEL not in result.stderr
	assert "add-mask" not in result.stderr
	assert "::group::" not in result.stderr
	lines = [line for line in result.stderr.split("\n") if line]
	assert lines[0] == (
		"::warning::Comments JSON for issue #123 failed validation; proceeding with empty list"
	)
	summaries = [m for m in (SUMMARY_RE.match(line) for line in lines) if m]
	assert len(summaries) == 1, result.stderr
	assert len(lines) == 2, result.stderr
	summary = summaries[0]
	assert summary.group("cat") == category
	assert int(summary.group("bytes")) == len(fixture)
	if shutil.which("sha256sum") or shutil.which("shasum"):
		assert summary.group("sha") == hashlib.sha256(fixture).hexdigest()[:12]
	assert result.stdout.split("\n")[:2] == ["COMMENTS=[]", "COMMENTS_FETCH_OK=false"]
	assert sorted(p.name for p in work.iterdir()) == []


@pytest.mark.parametrize("fixture", [b'[{"id":1}]\n[{"id":2}]\n', b""], ids=["two_pages", "empty"])
def test_valid_response_logs_nothing(tmp_path: Path, fixture: bytes) -> None:
	result, work = _run_fetch(tmp_path, fixture)

	assert result.returncode == 0, result.stderr
	assert result.stderr == ""
	assert "COMMENTS_FETCH_OK=true" in result.stdout
	assert sorted(p.name for p in work.iterdir()) == []
