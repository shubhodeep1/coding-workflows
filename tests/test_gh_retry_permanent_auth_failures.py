#!/usr/bin/env python3
"""Tests that ``gh_retry`` treats HTTP 400 / 401 failures as permanent.

``_is_gh_permanent_failure`` in scripts/gh_helpers.sh decides which failures
the legacy retry loops (``gh_retry``, ``gh_retry_to_file``) stop on at once.
HTTP 400, HTTP 401, ``Bad credentials`` and ``Requires authentication`` are
deterministic, so retrying them only burns the attempt budget (Refs #7355,
finding API-003 of analysis/workflow-optimization-2026-09-18.md). Generic
HTTP 403 stays retryable, because secondary rate limits also answer 403.

The harness mirrors tests/test_gh_retry_stdout_isolation.py: a fake ``gh``
serves per-attempt stdout, stderr and exit code from files, and ``sleep`` is
stubbed so retry waits cost nothing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"

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

BAD_CREDENTIALS_BODY = '{"message":"Bad credentials"}'
BAD_CREDENTIALS_STDERR = "gh: Bad credentials (HTTP 401)\n"


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


def _classify(tmp_path: Path, sample: str) -> int:
	env = _setup(tmp_path, [])
	env["SAMPLE"] = sample
	return _run(tmp_path, env, '_is_gh_permanent_failure "${SAMPLE}"').returncode


@pytest.mark.parametrize(
	"sample",
	[
		"HTTP 401: Bad credentials (https://api.github.com/user)",
		"gh: Bad credentials (HTTP 401)",
		"HTTP 400: Bad Request",
		"gh: Requires authentication (HTTP 401)",
		"gh: Not Found (HTTP 404)",
		"HTTP 422: Validation Failed",
	],
)
def test_permanent_failure_matches_auth_and_bad_request(tmp_path: Path, sample: str) -> None:
	assert _classify(tmp_path, sample) == 0


@pytest.mark.parametrize(
	"sample",
	[
		"HTTP 403: API rate limit exceeded for user ID 1.",
		"You have exceeded a secondary rate limit",
		"HTTP 502: Bad Gateway",
		"API rate limit exceeded for 203.0.113.7. (But here's the good news: "
		"Authenticated requests get a higher rate limit.)",
		"error connecting to api.github.com",
	],
)
def test_permanent_failure_does_not_match_rate_limits_or_server_errors(
	tmp_path: Path, sample: str
) -> None:
	assert _classify(tmp_path, sample) != 0


def test_gh_retry_makes_one_attempt_on_401(tmp_path: Path) -> None:
	env = _setup(tmp_path, [(BAD_CREDENTIALS_BODY, BAD_CREDENTIALS_STDERR, 1)])
	result = _run(tmp_path, env, "gh_retry gh api /repos/o/r")
	assert result.returncode != 0
	assert _calls(tmp_path) == 1
	assert "non-retryable" in result.stderr
	assert result.stdout == ""


@pytest.mark.parametrize(
	"stderr",
	["gh: HTTP 400: Bad Request\n", "gh: Requires authentication (HTTP 401)\n"],
)
def test_gh_retry_makes_one_attempt_on_400_and_requires_authentication(
	tmp_path: Path, stderr: str
) -> None:
	env = _setup(tmp_path, [('{"message":"error"}', stderr, 1)])
	result = _run(tmp_path, env, "gh_retry gh api /repos/o/r")
	assert result.returncode != 0
	assert _calls(tmp_path) == 1


def test_gh_retry_to_file_makes_one_attempt_on_401(tmp_path: Path) -> None:
	env = _setup(tmp_path, [(BAD_CREDENTIALS_BODY, BAD_CREDENTIALS_STDERR, 1)])
	result = _run(tmp_path, env, "gh_retry_to_file out.json gh api /repos/o/r")
	assert result.returncode != 0
	assert _calls(tmp_path) == 1
	out = tmp_path / "out.json"
	assert not out.exists() or out.read_text(encoding="utf-8") == ""


def test_gh_retry_still_retries_502(tmp_path: Path) -> None:
	env = _setup(tmp_path, [('{"message":"Server Error"}', "gh: HTTP 502: Bad Gateway\n", 1)])
	env["GH_RETRY_MAX_ATTEMPTS"] = "3"
	result = _run(tmp_path, env, "gh_retry gh api /repos/o/r")
	assert result.returncode != 0
	assert _calls(tmp_path) == 3
