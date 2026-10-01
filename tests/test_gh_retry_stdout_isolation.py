#!/usr/bin/env python3
"""Tests for the stdout contract of ``gh_retry`` in scripts/gh_helpers.sh.

Background (issue #5495, clarify run 36670937896, 2026-09-30): ``gh api``
prints the error response body to **stdout** when a call fails.
``gh_retry`` redirected only stderr per attempt, so a call that was
rate-limited twice and then succeeded wrote two error objects followed by
the real issue into the caller's file. ``jq -r '.number'`` printed
``null``, ``null``, ``5016`` and the bare ``null`` line in ``$GITHUB_ENV``
failed the step with ``Invalid format 'null'``.

``gh_retry`` now buffers each attempt's stdout and emits only the buffer of
the attempt that succeeds. These tests drive it through a fake ``gh`` whose
per-attempt stdout, stderr, and exit code come from files, with ``sleep``
stubbed so the retry waits cost nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"
REVIEW_AUTOFIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"

# The two steps of review_autofix.yml that define their own retry loop
# instead of sourcing scripts/gh_helpers.sh (the first only as a fallback
# when the source fails). Both capture gh_retry output with $(...).
INLINE_GH_RETRY_STEPS = (
	"Dispatch standalone validate for orchestrator short-circuit issues",
	"Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge",
)

ISSUE_BODY = json.dumps({"number": 5016, "title": "real issue"}) + "\n"
RATE_LIMIT_BODY = (
	'{"message":"API rate limit exceeded for user ID 11442166. BODY-MARKER-RL",'
	'"documentation_url":"https://docs.github.com/rest/overview/rate-limits-for-the-rest-api"}'
)
RATE_LIMIT_STDERR = "gh: API rate limit exceeded for user ID 11442166. (HTTP 403)\n"
NOT_FOUND_BODY = '{"message":"Not Found BODY-MARKER-404","status":"404"}'
NOT_FOUND_STDERR = "gh: Not Found (HTTP 404)\n"
BAD_GATEWAY_BODY = '{"message":"Server Error BODY-MARKER-502"}'
BAD_GATEWAY_STDERR = "gh: HTTP 502: Bad Gateway\n"

# The fake gh answers `gh api -i /rate_limit` (the rate-limit wait probe)
# without consuming an attempt; every other call is attempt N, served from
# $FAKE_GH_PLAN/N.{stdout,stderr,rc}, falling back to the highest-numbered
# attempt when the plan runs out.
FAKE_GH = r"""#!/usr/bin/env bash
if [ "$1" = "api" ] && [ "$2" = "-i" ] && [ "$3" = "/rate_limit" ]; then
	printf 'HTTP/2 200\r\nx-ratelimit-reset: 0\r\n\r\n{}'
	exit 0
fi
count_file="${FAKE_GH_PLAN}/calls"
n=$(( $(cat "${count_file}" 2>/dev/null || echo 0) + 1 ))
echo "${n}" > "${count_file}"
i="${n}"
while [ "${i}" -gt 1 ] && [ ! -f "${FAKE_GH_PLAN}/${i}.rc" ]; do
	i=$(( i - 1 ))
done
[ -f "${FAKE_GH_PLAN}/${i}.stdout" ] && cat "${FAKE_GH_PLAN}/${i}.stdout"
[ -f "${FAKE_GH_PLAN}/${i}.stderr" ] && cat "${FAKE_GH_PLAN}/${i}.stderr" >&2
exit "$(cat "${FAKE_GH_PLAN}/${i}.rc")"
"""


def _setup(tmp_path: Path, attempts: list[tuple[str, str, int]]) -> dict[str, str]:
	"""Install the fake gh and its per-attempt plan; return the env to run with."""
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	fake = bin_dir / "gh"
	fake.write_text(FAKE_GH, encoding="utf-8")
	fake.chmod(0o755)
	plan = tmp_path / "plan"
	plan.mkdir()
	for index, (stdout, stderr, rc) in enumerate(attempts, start=1):
		(plan / f"{index}.stdout").write_text(stdout, encoding="utf-8")
		(plan / f"{index}.stderr").write_text(stderr, encoding="utf-8")
		(plan / f"{index}.rc").write_text(f"{rc}\n", encoding="utf-8")
	tmpdir = tmp_path / "tmpdir"
	tmpdir.mkdir()
	env = {
		key: value
		for key, value in os.environ.items()
		if key not in {"TG_BOT_SECRET", "TG_ADMIN_CHAT_ID", "TG_CHAT_ID", "GH_RETRY_MAX_ATTEMPTS"}
	}
	env.update(
		{
			"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			"FAKE_GH_PLAN": str(plan),
			"TMPDIR": str(tmpdir),
			"GH_RATE_LIMIT_BREAKER_FILE": str(tmp_path / "breaker"),
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	return env


def _run(tmp_path: Path, env: dict[str, str], body: str) -> subprocess.CompletedProcess[str]:
	"""Source gh_helpers.sh with sleep stubbed, then run ``body`` in bash."""
	script = (
		"set -uo pipefail\n"
		f"source '{GH_HELPERS}'\n"
		"sleep() { :; }\n"
		f"{body}\n"
	)
	return subprocess.run(
		["bash", "-c", script], env=env, capture_output=True, text=True, cwd=tmp_path, timeout=60,
	)


def _calls(tmp_path: Path) -> int:
	calls = tmp_path / "plan" / "calls"
	return int(calls.read_text(encoding="utf-8").strip()) if calls.exists() else 0


def _leftover_temp_files(tmp_path: Path) -> list[str]:
	return sorted(p.name for p in (tmp_path / "tmpdir").glob("gh_retry_*"))


RATE_LIMITED_TWICE_THEN_OK = [
	(RATE_LIMIT_BODY, RATE_LIMIT_STDERR, 1),
	(RATE_LIMIT_BODY, RATE_LIMIT_STDERR, 1),
	(ISSUE_BODY, "", 0),
]


def test_redirect_to_file_holds_only_the_successful_body(tmp_path: Path) -> None:
	env = _setup(tmp_path, RATE_LIMITED_TWICE_THEN_OK)
	out_file = tmp_path / "issue_meta.json"
	result = _run(
		tmp_path,
		env,
		f"gh_retry gh api repos/o/r/issues/5016 > '{out_file}'; echo \"rc=$?\"",
	)
	assert result.stdout.strip() == "rc=0", result.stderr
	assert out_file.read_text(encoding="utf-8") == ISSUE_BODY
	assert _calls(tmp_path) == 3
	# The clarify.yml:392 consumer: exactly one number, no `null` lines.
	jq = subprocess.run(
		["jq", "-r", ".number", str(out_file)], capture_output=True, text=True, check=True,
	)
	assert jq.stdout == "5016\n"
	assert _leftover_temp_files(tmp_path) == []


def test_command_substitution_captures_only_the_successful_body(tmp_path: Path) -> None:
	env = _setup(tmp_path, RATE_LIMITED_TWICE_THEN_OK)
	captured = tmp_path / "captured.json"
	result = _run(
		tmp_path,
		env,
		"meta=\"$(gh_retry gh api repos/o/r/issues/5016)\"; rc=$?\n"
		f"printf '%s' \"${{meta}}\" > '{captured}'; echo \"rc=${{rc}}\"",
	)
	assert result.stdout.strip() == "rc=0", result.stderr
	assert captured.read_text(encoding="utf-8") == ISSUE_BODY.rstrip("\n")


def test_failed_attempt_output_is_reported_by_size_not_content(tmp_path: Path) -> None:
	env = _setup(tmp_path, RATE_LIMITED_TWICE_THEN_OK)
	result = _run(tmp_path, env, "gh_retry gh api repos/o/r/issues/5016 > /dev/null")
	assert result.returncode == 0, result.stderr
	size = len(RATE_LIMIT_BODY.encode("utf-8"))
	assert f"gh_retry: dropped {size} bytes of stdout from failed attempt 1/5" in result.stderr
	assert f"gh_retry: dropped {size} bytes of stdout from failed attempt 2/5" in result.stderr
	assert "BODY-MARKER" not in result.stderr
	# The existing rate-limit handling still runs unchanged.
	assert result.stderr.count("GitHub API rate limit hit") == 2
	assert (tmp_path / "breaker").exists()


def test_permanent_failure_leaves_stdout_empty_and_returns_1(tmp_path: Path) -> None:
	env = _setup(tmp_path, [(NOT_FOUND_BODY, NOT_FOUND_STDERR, 1)])
	result = _run(tmp_path, env, "gh_retry gh api repos/o/r/issues/1")
	assert result.returncode == 1
	assert result.stdout == ""
	assert _calls(tmp_path) == 1
	assert "non-retryable error" in result.stderr
	assert "BODY-MARKER" not in result.stderr
	assert _leftover_temp_files(tmp_path) == []


def test_exhausted_retries_leave_stdout_empty_and_return_1(tmp_path: Path) -> None:
	env = _setup(tmp_path, [(BAD_GATEWAY_BODY, BAD_GATEWAY_STDERR, 1)])
	env["GH_RETRY_MAX_ATTEMPTS"] = "2"
	result = _run(tmp_path, env, "gh_retry gh api repos/o/r/issues/1")
	assert result.returncode == 1
	assert result.stdout == ""
	assert _calls(tmp_path) == 2
	assert "gh command failed after 2 attempts" in result.stderr
	assert _leftover_temp_files(tmp_path) == []


def test_first_try_success_passes_stdout_through_unchanged(tmp_path: Path) -> None:
	payload = "line one\nlast line without newline"
	env = _setup(tmp_path, [(payload, "", 0)])
	result = _run(tmp_path, env, "gh_retry gh api repos/o/r")
	assert result.returncode == 0
	assert result.stdout == payload
	assert result.stderr == ""
	assert _calls(tmp_path) == 1
	assert _leftover_temp_files(tmp_path) == []


def test_stdout_buffer_mktemp_failure_does_not_run_the_command(tmp_path: Path) -> None:
	env = _setup(tmp_path, [(ISSUE_BODY, "", 0)])
	result = _run(
		tmp_path,
		env,
		'mktemp() { case "$1" in *gh_retry_stdout*) return 1 ;; esac; command mktemp "$@"; }\n'
		"gh_retry gh api repos/o/r/issues/5016",
	)
	assert result.returncode == 1
	assert result.stdout == ""
	assert "gh_retry: failed to create stdout temp file" in result.stderr
	assert _calls(tmp_path) == 0
	assert _leftover_temp_files(tmp_path) == []


def test_closed_reader_does_not_rerun_a_successful_command(tmp_path: Path) -> None:
	# 1 MiB of output into `head -c 1`: the reader goes away long before the
	# output is written. The command already succeeded, so it must not be
	# run again; the undelivered output is reported as a non-zero status.
	big = "x" * (1024 * 1024)
	env = _setup(tmp_path, [(big, "", 0)])
	result = _run(
		tmp_path,
		env,
		"gh_retry gh api repos/o/r | head -c 1 > /dev/null\n"
		'echo "rc=${PIPESTATUS[0]}"',
	)
	assert _calls(tmp_path) == 1, result.stderr
	rc_line = result.stdout.strip()
	assert rc_line.startswith("rc=") and rc_line != "rc=0", (rc_line, result.stderr)
	assert _leftover_temp_files(tmp_path) == []


def _inline_gh_retry(step_name: str) -> str:
	"""Return the inline ``gh_retry() { ... }`` defined in a review_autofix.yml step."""
	lines = REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8").splitlines()
	step_idx = next(
		(i for i, line in enumerate(lines) if line.strip() == f"- name: {step_name}"), None,
	)
	assert step_idx is not None, f"step not found in review_autofix.yml: {step_name}"
	start = next(
		(i for i in range(step_idx + 1, len(lines)) if lines[i].strip() == "gh_retry() {"), None,
	)
	assert start is not None, f"no inline gh_retry() in step: {step_name}"
	indent = lines[start][: len(lines[start]) - len(lines[start].lstrip(" "))]
	end = next(i for i in range(start + 1, len(lines)) if lines[i] == f"{indent}}}")
	return textwrap.dedent("\n".join(lines[start : end + 1])) + "\n"


def _run_inline(
	tmp_path: Path, env: dict[str, str], step_name: str, body: str,
) -> subprocess.CompletedProcess[str]:
	"""Define the step's inline gh_retry with sleep stubbed, then run ``body``."""
	script = (
		"set -euo pipefail\n"
		f"{_inline_gh_retry(step_name)}"
		"sleep() { :; }\n"
		f"{body}\n"
	)
	return subprocess.run(
		["bash", "-c", script], env=env, capture_output=True, text=True, cwd=tmp_path, timeout=60,
	)


def _all_temp_files(tmp_path: Path) -> list[str]:
	return sorted(p.name for p in (tmp_path / "tmpdir").iterdir())


@pytest.mark.parametrize("step_name", INLINE_GH_RETRY_STEPS)
def test_inline_wrapper_capture_holds_only_the_successful_body(
	tmp_path: Path, step_name: str,
) -> None:
	env = _setup(tmp_path, RATE_LIMITED_TWICE_THEN_OK)
	captured = tmp_path / "captured.json"
	result = _run_inline(
		tmp_path,
		env,
		step_name,
		'meta="$(gh_retry gh api repos/o/r/pulls/1 --jq .head.sha)"\n'
		f"printf '%s' \"${{meta}}\" > '{captured}'",
	)
	assert result.returncode == 0, result.stderr
	assert captured.read_text(encoding="utf-8") == ISSUE_BODY.rstrip("\n")
	assert _calls(tmp_path) == 3
	size = len(RATE_LIMIT_BODY.encode("utf-8"))
	assert f"gh_retry: dropped {size} bytes of stdout from failed attempt 1/4" in result.stderr
	assert f"gh_retry: dropped {size} bytes of stdout from failed attempt 2/4" in result.stderr
	assert "BODY-MARKER" not in result.stderr
	assert _all_temp_files(tmp_path) == []


@pytest.mark.parametrize("step_name", INLINE_GH_RETRY_STEPS)
def test_inline_wrapper_exhausted_retries_leave_stdout_empty(
	tmp_path: Path, step_name: str,
) -> None:
	env = _setup(tmp_path, [(BAD_GATEWAY_BODY, BAD_GATEWAY_STDERR, 1)])
	result = _run_inline(
		tmp_path,
		env,
		step_name,
		'if gh_retry gh api repos/o/r/pulls/1; then echo "rc=0"; else echo "rc=$?"; fi',
	)
	assert result.stdout == "rc=1\n", result.stderr
	assert _calls(tmp_path) == 4
	assert "BODY-MARKER" not in result.stdout + result.stderr
	assert _all_temp_files(tmp_path) == []


@pytest.mark.parametrize("step_name", INLINE_GH_RETRY_STEPS)
def test_inline_wrapper_first_try_success_passes_stdout_through_unchanged(
	tmp_path: Path, step_name: str,
) -> None:
	payload = "line one\nlast line without newline"
	env = _setup(tmp_path, [(payload, "", 0)])
	result = _run_inline(tmp_path, env, step_name, "gh_retry gh api repos/o/r")
	assert result.returncode == 0, result.stderr
	assert result.stdout == payload
	assert result.stderr == ""
	assert _calls(tmp_path) == 1
	assert _all_temp_files(tmp_path) == []
