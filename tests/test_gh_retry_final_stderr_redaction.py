#!/usr/bin/env python3
"""Final-failure stderr of the gh retry helpers is escaped and bounded.

Background (SEC-001 in analysis/workflow-optimization-2026-09-26-2.md,
follow-up to issue #8095): after their ``failed after N attempts``
``::error::`` line, ``gh_retry``, ``gh_retry_to_file`` and
``gh_api_json_to_file`` in scripts/gh_helpers.sh used to ``cat`` the last
attempt's stderr straight to the job log. That dump had no size limit and
was not escaped, so an API error body line starting with ``::`` (for
example ``::set-output`` or ``::add-mask``) was read as a workflow command.

All three now print one ``::warning::  stderr: ...`` line built from
``_gh_final_stderr_excerpt`` (first 2048 bytes, NUL bytes stripped, a
`` [truncated]`` suffix when cut) wrapped in ``_gh_actions_escape``. These
tests drive the helpers through a fake ``gh`` with one attempt and a
stubbed ``sleep``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"

EXCERPT_CAP = 2048
TRUNCATED_SUFFIX = " [truncated]"
FINAL_WARNING_PREFIX = "::warning::  stderr: "
ALLOWED_COMMAND_PREFIXES = ("::error::", "::warning::", "::notice::")

# "HTTP 502" is transient: neither _is_gh_permanent_failure nor
# _is_gh_rate_limit matches it, so the call reaches the final-failure path.
HOSTILE_STDERR = (
	"gh: HTTP 502: Bad Gateway\n"
	"::set-output name=x::y\n"
	"::add-mask::zzz\n"
	"percent %0A literal and carriage\rreturn\n"
	+ "A" * 5000
	+ "\nTAIL-MARKER\n"
)
SHORT_STDERR = "gh: HTTP 502: Bad Gateway\n::set-output name=x::y\n"

# Fake gh: serves stdout/stderr/rc from $FAKE_GH_PLAN for every call.
FAKE_GH = r"""#!/usr/bin/env bash
cat "${FAKE_GH_PLAN}/stdout"
cat "${FAKE_GH_PLAN}/stderr" >&2
exit "$(cat "${FAKE_GH_PLAN}/rc")"
"""

CALLS = {
	"gh_retry": (
		"gh_retry gh api repos/o/r/issues/1",
		"::error::gh command failed after 1 attempts",
	),
	"gh_retry_to_file": (
		"gh_retry_to_file \"${OUT_FILE}\" gh api repos/o/r/issues/1",
		"::error::gh command failed after 1 attempts",
	),
	"gh_api_json_to_file": (
		"gh_api_json_to_file \"${OUT_FILE}\" gh api repos/o/r/issues/1",
		"::error::gh api failed to return valid JSON after 1 attempts",
	),
}


def _setup(tmp_path: Path, stderr: bytes, stdout: bytes = b'{"message":"Server Error"}', rc: int = 1) -> dict[str, str]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	fake = bin_dir / "gh"
	fake.write_text(FAKE_GH, encoding="utf-8")
	fake.chmod(0o755)
	plan = tmp_path / "plan"
	plan.mkdir()
	(plan / "stdout").write_bytes(stdout)
	(plan / "stderr").write_bytes(stderr)
	(plan / "rc").write_text(f"{rc}\n", encoding="utf-8")
	tmpdir = tmp_path / "tmpdir"
	tmpdir.mkdir()
	env = {
		key: value
		for key, value in os.environ.items()
		if key not in {"TG_BOT_SECRET", "TG_ADMIN_CHAT_ID", "TG_CHAT_ID", "GH_RETRY_IDEMPOTENT"}
	}
	env.update(
		{
			"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			"FAKE_GH_PLAN": str(plan),
			"TMPDIR": str(tmpdir),
			"GH_RATE_LIMIT_BREAKER_FILE": str(tmp_path / "breaker"),
			"GH_RETRY_MAX_ATTEMPTS": "1",
			"OUT_FILE": str(tmp_path / "out.json"),
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	return env


def _run(tmp_path: Path, env: dict[str, str], body: str) -> subprocess.CompletedProcess[str]:
	script = (
		"set -uo pipefail\n"
		f"source '{GH_HELPERS}'\n"
		"sleep() { :; }\n"
		f"{body}\n"
	)
	return subprocess.run(
		["bash", "-c", script],
		env=env,
		capture_output=True,
		text=True,
		errors="replace",
		cwd=tmp_path,
		timeout=60,
	)


def _decode(payload: str) -> str:
	"""Reverse _gh_actions_escape (%25, %0D, %0A) in a single pass."""
	table = {"%25": "%", "%0D": "\r", "%0A": "\n"}
	return re.sub(r"%(?:25|0D|0A)", lambda m: table[m.group(0)], payload)


def _final_warning_lines(stderr: str, error_line_prefix: str) -> list[str]:
	lines = stderr.split("\n")
	error_idx = [i for i, line in enumerate(lines) if line.startswith(error_line_prefix)]
	assert len(error_idx) == 1, stderr
	return [line for line in lines[error_idx[0] + 1:] if line]


def _assert_no_foreign_workflow_commands(stderr: str) -> None:
	for line in stderr.split("\n"):
		if line.startswith("::"):
			assert line.startswith(ALLOWED_COMMAND_PREFIXES), line
		assert not line.startswith("::set-output"), line
		assert not line.startswith("::add-mask"), line


@pytest.mark.parametrize("name", sorted(CALLS))
def test_hostile_final_stderr_is_single_escaped_bounded_line(tmp_path: Path, name: str) -> None:
	call, error_prefix = CALLS[name]
	env = _setup(tmp_path, HOSTILE_STDERR.encode("utf-8"))
	result = _run(tmp_path, env, call)

	assert result.returncode == 1, result.stderr
	_assert_no_foreign_workflow_commands(result.stderr)
	assert error_prefix in result.stderr

	after_error = _final_warning_lines(result.stderr, error_prefix)
	assert len(after_error) == 1, after_error
	final = after_error[0]
	assert final.startswith(FINAL_WARNING_PREFIX), final
	assert final.endswith(TRUNCATED_SUFFIX), final[-80:]
	assert "TAIL-MARKER" not in final

	decoded = _decode(final[len(FINAL_WARNING_PREFIX):])
	assert decoded.endswith(TRUNCATED_SUFFIX)
	content = decoded[: -len(TRUNCATED_SUFFIX)]
	assert len(content.encode("utf-8")) <= EXCERPT_CAP
	assert HOSTILE_STDERR.startswith(content), content[:200]
	assert "::set-output name=x::y" in content

	if name != "gh_retry":
		out_file = Path(env["OUT_FILE"])
		assert out_file.exists() and out_file.read_bytes() == b""
	assert sorted(p.name for p in (tmp_path / "tmpdir").iterdir()) == []


@pytest.mark.parametrize("name", sorted(CALLS))
def test_short_final_stderr_is_not_marked_truncated(tmp_path: Path, name: str) -> None:
	call, error_prefix = CALLS[name]
	env = _setup(tmp_path, SHORT_STDERR.encode("utf-8"))
	result = _run(tmp_path, env, call)

	assert result.returncode == 1, result.stderr
	_assert_no_foreign_workflow_commands(result.stderr)
	after_error = _final_warning_lines(result.stderr, error_prefix)
	assert after_error == [
		FINAL_WARNING_PREFIX + "gh: HTTP 502: Bad Gateway%0A::set-output name=x::y"
	]


def test_excerpt_helper_strips_nul_bytes_without_bash_warning(tmp_path: Path) -> None:
	capture = tmp_path / "capture"
	capture.write_bytes(b"gh: HTTP 502\x00 Bad Gateway\n::set-output name=x::y\n")
	env = _setup(tmp_path, b"")
	result = _run(
		tmp_path,
		env,
		f"x=\"$(_gh_final_stderr_excerpt '{capture}')\"; "
		"echo \"::warning::  stderr: $(_gh_actions_escape \"${x}\")\" >&2",
	)
	assert result.returncode == 0, result.stderr
	assert "ignored null byte" not in result.stderr
	assert result.stderr.strip("\n") == (
		FINAL_WARNING_PREFIX + "gh: HTTP 502 Bad Gateway%0A::set-output name=x::y"
	)


def test_excerpt_helper_caps_at_exact_boundary(tmp_path: Path) -> None:
	exact = tmp_path / "exact"
	exact.write_bytes(b"B" * EXCERPT_CAP)
	over = tmp_path / "over"
	over.write_bytes(b"B" * (EXCERPT_CAP + 1))
	env = _setup(tmp_path, b"")
	result = _run(
		tmp_path,
		env,
		f"_gh_final_stderr_excerpt '{exact}'; echo; _gh_final_stderr_excerpt '{over}'; echo; "
		f"_gh_final_stderr_excerpt '{tmp_path / 'missing'}'; echo \"rc=$?\"",
	)
	assert result.returncode == 0, result.stderr
	assert result.stderr == ""
	assert result.stdout.split("\n") == [
		"B" * EXCERPT_CAP,
		"B" * EXCERPT_CAP + TRUNCATED_SUFFIX,
		"rc=0",
		"",
	]
