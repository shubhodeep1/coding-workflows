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
# The only workflow-command lines the helpers themselves print in these
# tests: the final ``failed after`` error, the per-attempt retry warning,
# the escaped single-line ``stderr:`` warning and gh_retry's stdout-drop
# note. Any other ``::`` line (a ``::notice::``, or a forged
# ``::warning::add-mask::x``) fails the test.
ALLOWED_COMMAND_LINE_RES = (
	re.compile(r"^::error::gh (?:command|api) failed "),
	re.compile(r"^::warning::gh command failed \(attempt [0-9]+/[0-9]+\), retrying in "),
	re.compile(r"^::warning::  stderr: [^\n]*$"),
	re.compile(r"^::warning::  gh_retry: dropped [0-9]+ bytes of stdout from failed attempt [0-9]+/[0-9]+$"),
)

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
			assert any(rx.match(line) for rx in ALLOWED_COMMAND_LINE_RES), line
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


# gh_api_retry (scripts/gh_helpers.sh) ends its permanent and give-up paths
# with the same escaped, bounded line. ``gh`` is stubbed as a shell function
# here, so no executable file is needed. It prints an ``-i`` style response
# (status line, headers, body) and the hostile stderr.
GH_API_RETRY_CASES = {
	"transient_gave_up": ("502", 1, "GH_API_RETRY outcome=gave_up kind=transient"),
	"permanent": ("422", 2, "GH_API_RETRY outcome=permanent kind=permanent"),
}


@pytest.mark.parametrize("case", sorted(GH_API_RETRY_CASES))
def test_gh_api_retry_final_stderr_is_single_escaped_bounded_line(tmp_path: Path, case: str) -> None:
	status, expected_rc, log_prefix = GH_API_RETRY_CASES[case]
	stderr_file = tmp_path / "hostile_stderr"
	stderr_file.write_text(HOSTILE_STDERR.replace("HTTP 502", f"HTTP {status}"), encoding="utf-8")
	env = _setup(tmp_path, b"")
	env["FAKE_STATUS"] = status
	env["FAKE_STDERR_FILE"] = str(stderr_file)
	body = (
		"gh() { printf 'HTTP/2.0 %s X\\r\\nContent-Type: application/json\\r\\n\\r\\n{\"message\":\"m\"}' "
		"\"${FAKE_STATUS}\"; cat \"${FAKE_STDERR_FILE}\" >&2; return 1; }\n"
		"gh_api_retry --idempotent repos/o/r/issues/1\n"
		"echo \"rc=$?\"\n"
	)
	result = _run(tmp_path, env, body)

	assert f"rc={expected_rc}" in result.stdout, result.stderr
	_assert_no_foreign_workflow_commands(result.stderr)
	_final_warning_lines(result.stderr, log_prefix)
	warnings = [line for line in result.stderr.split("\n") if line.startswith(FINAL_WARNING_PREFIX)]
	assert len(warnings) == 1, result.stderr
	final = warnings[0]
	assert final.endswith(TRUNCATED_SUFFIX), final[-80:]
	assert "TAIL-MARKER" not in final
	content = _decode(final[len(FINAL_WARNING_PREFIX):])[: -len(TRUNCATED_SUFFIX)]
	assert len(content.encode("utf-8")) <= EXCERPT_CAP
	assert "::set-output name=x::y" in content
	assert sorted(p.name for p in (tmp_path / "tmpdir").iterdir()) == []


# The poller stores a refused final merge's error in `.final_merge_error`
# (scripts/orchestrate_poll_process.sh, `merge_err_for_state`) after decoding
# gh_retry's escaped "::warning::  stderr:" line. This pins that decode against
# the real _gh_actions_escape / _gh_final_stderr_excerpt output, so a change to
# the prefix or the escape alphabet fails here instead of leaving %0A / %25
# text in the state file.
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
MERGE_ERR_FOR_STATE_AWK_RE = re.compile(
	r"""merge_err_for_state="\$\(printf '%s\\n' "\$\{merge_err\}" \\\n\s*\| awk '(?P<prog>[^']+)'"""
)


def test_poller_final_merge_error_decode_inverts_gh_retry_escape(tmp_path: Path) -> None:
	match = MERGE_ERR_FOR_STATE_AWK_RE.search(POLLER.read_text(encoding="utf-8"))
	assert match is not None
	capture = tmp_path / "capture"
	capture.write_text("a\nb% literal %0A\rc", encoding="utf-8")
	env = _setup(tmp_path, b"")
	produced = _run(
		tmp_path,
		env,
		"echo '::error::gh command failed after 1 attempts: gh pr merge 1'; "
		f"echo \"::warning::  stderr: $(_gh_actions_escape \"$(_gh_final_stderr_excerpt '{capture}')\")\"",
	)
	assert produced.returncode == 0, produced.stderr
	decoded = subprocess.run(
		["awk", match.group("prog")],
		input=produced.stdout,
		capture_output=True,
		text=True,
		check=True,
	).stdout
	assert decoded == (
		"::error::gh command failed after 1 attempts: gh pr merge 1\n"
		# %0D decodes to a newline too, as in the refusal path.
		"a\nb% literal %0A\nc\n"
	)
