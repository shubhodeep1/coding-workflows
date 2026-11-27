#!/usr/bin/env python3
"""Issue #10225: Phase 7's cancel-run listing waits out a rate limit.

Gate run 46009933351 failed in "Phase 7: Close PR and verify
cancel_on_pr_close fires": from 17:22:59 UTC the runs listing answered
"API rate limit exceeded", phase7_list_cancel_runs() tried three times 5 s
apart and the step reported lookup_failed. These cases replay that with a
fake `gh` executable and the real scripts/gh_helpers.sh: the listing now
waits for the reset and succeeds, and a reset further away than the job's
remaining time (or the helper's 600 s cap) gives up without sleeping.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "stable_e2e_step_verify_cancel_on_close.sh"
HELPERS_START = "if ! declare -F gh_api_retry >/dev/null 2>&1; then"
HELPERS_END = "# ── Phase 7 wait deadline helpers"
RUNS_BODY = {"workflow_runs": [{"id": 46040765863, "event": "pull_request", "status": "completed"}]}

FAKE_GH = r"""#!/usr/bin/env bash
n=$(cat "$GH_COUNT" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$GH_COUNT"
printf '%s\n' "$*" >> "$GH_ARGS"
if [ "$n" -le "$FAIL_N" ]; then
  printf 'HTTP/2.0 403 Forbidden\r\nx-ratelimit-limit: 5000\r\nx-ratelimit-remaining: 0\r\nx-ratelimit-reset: %s\r\nx-ratelimit-resource: core\r\n\r\n{"message":"API rate limit exceeded for user ID 11442166."}' "$RESET"
  echo "gh: API rate limit exceeded for user ID 11442166. (HTTP 403)" >&2
  exit 1
fi
printf 'HTTP/2.0 200 OK\r\nx-ratelimit-remaining: 4999\r\nx-ratelimit-reset: %s\r\nx-ratelimit-resource: core\r\n\r\n%s' "$RESET" "$RUNS_BODY"
"""


def _helpers_block() -> str:
	text = SCRIPT.read_text(encoding="utf-8")
	start = text.index(HELPERS_START)
	end = text.index(HELPERS_END, start)
	return text[start:end]


def _replay(fail_n: int, reset_in: int, job_started_ago: int | None = None) -> tuple[subprocess.CompletedProcess, list[int], int, str]:
	if shutil.which("jq") is None:
		import pytest

		pytest.skip("jq is required")
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		fake = bin_dir / "gh"
		fake.write_text(FAKE_GH, encoding="utf-8")
		fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
		sleeps = tmp_path / "sleeps"
		sleeps.write_text("", encoding="utf-8")
		out = tmp_path / "out"
		env = {
			key: value
			for key, value in os.environ.items()
			if not key.startswith(("TELEGRAM", "GH_RETRY", "GH_API_RETRY", "E2E_"))
		}
		env.update(
			PATH=f"{bin_dir}:{env.get('PATH', '/usr/bin:/bin')}",
			GH_COUNT=str(tmp_path / "count"),
			GH_ARGS=str(tmp_path / "args"),
			FAIL_N=str(fail_n),
			RESET=str(int(time.time()) + reset_in),
			RUNS_BODY=json.dumps(RUNS_BODY),
			SLEEPS=str(sleeps),
			OUT=str(out),
			TMPDIR=tmp,
			RUNNER_TEMP=tmp,
			GH_RATE_LIMIT_BREAKER_FILE=str(tmp_path / "breaker"),
			TEST_REPO="o/r",
			E2E_JOB_TIMEOUT_MINUTES="360",
			E2E_FINALIZATION_RESERVE_MINUTES="20",
		)
		if job_started_ago is not None:
			env["E2E_JOB_STARTED_EPOCH"] = str(int(time.time()) - job_started_ago)
		prelude = "\n".join(
			[
				"set -euo pipefail",
				'sleep() { echo "$1" >> "$SLEEPS"; }',
				"",
			]
		)
		body = (
			prelude
			+ _helpers_block()
			+ '\nrc=0\nphase7_list_cancel_runs 10 || rc=$?\nprintf \'%s\' "${PHASE7_CANCEL_RUNS_JSON:-}" > "$OUT"\nexit "$rc"\n'
		)
		proc = subprocess.run(
			["bash", "-c", body],
			cwd=REPO_ROOT,
			env=env,
			capture_output=True,
			text=True,
			check=False,
			timeout=120,
		)
		recorded = [int(v) for v in sleeps.read_text(encoding="utf-8").split()]
		count_file = tmp_path / "count"
		calls = int(count_file.read_text(encoding="utf-8").strip()) if count_file.exists() else 0
		listing = out.read_text(encoding="utf-8") if out.exists() else ""
		args_file = tmp_path / "args"
		if args_file.exists():
			for line in args_file.read_text(encoding="utf-8").splitlines():
				assert line.startswith("api -i repos/o/r/actions/workflows/internal-cancel-on-pr-close.yml/runs?per_page=10"), line
	return proc, recorded, calls, listing


def test_rate_limited_listing_waits_for_the_reset_and_succeeds() -> None:
	"""The 17:22:59 replay: two rate-limit answers, then the reset passes."""
	proc, recorded, calls, listing = _replay(fail_n=2, reset_in=30)
	assert proc.returncode == 0, (proc.stdout, proc.stderr)
	assert calls == 3
	assert json.loads(listing) == RUNS_BODY
	# Each wait runs to the reset (about 31 s), not a fixed 5 s.
	assert len(recorded) == 2, recorded
	assert all(25 <= v <= 35 for v in recorded), recorded
	assert "GH_API_RETRY outcome=retry kind=primary bucket=core status=403" in proc.stderr
	assert "Listing internal-cancel-on-pr-close runs failed" not in proc.stdout


def test_unlimited_listing_makes_one_call_and_never_sleeps() -> None:
	proc, recorded, calls, listing = _replay(fail_n=0, reset_in=30)
	assert proc.returncode == 0
	assert calls == 1 and recorded == []
	assert json.loads(listing) == RUNS_BODY


def test_reset_past_the_job_cap_gives_up_without_sleeping() -> None:
	# Job started 339 minutes ago: 1 minute is left before the 20-minute
	# finalization reserve, so a single wait may take at most 30 s.
	proc, recorded, calls, _listing = _replay(fail_n=5, reset_in=300, job_started_ago=339 * 60)
	assert proc.returncode == 1, (proc.stdout, proc.stderr)
	assert recorded == []
	assert calls == 1
	assert "GH_API_RETRY outcome=gave_up kind=primary" in proc.stderr
	assert "::warning::Listing internal-cancel-on-pr-close runs failed (gh_api_retry rc=75)." in proc.stdout


def test_reset_past_the_helper_cap_gives_up_without_sleeping() -> None:
	proc, recorded, calls, _listing = _replay(fail_n=5, reset_in=3000)
	assert proc.returncode == 1
	assert recorded == [] and calls == 1


def test_waits_stay_inside_the_job_cap() -> None:
	"""Three attempts mean at most two waits, each at most half the time left."""
	block = _helpers_block()
	assert "GH_RETRY_MAX_ATTEMPTS=3" in block
	assert "left=$(( (job_cap - now) / 2 ))" in block
	assert "(E2E_JOB_TIMEOUT_MINUTES - ${E2E_FINALIZATION_RESERVE_MINUTES:-20}) * 60" in block
	assert "  echo 600\n" in block
	assert "sleep 5" not in block


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	sys.exit(main())
