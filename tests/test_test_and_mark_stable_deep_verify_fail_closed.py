#!/usr/bin/env python3
"""Issue #10225 review: deep verify fails closed on an unreadable jobs listing.

scripts/stable_e2e_step_deep_verify.sh reads each phase run's jobs through
gh_api_retry. A run whose listing could not be read used to pass as a
"spurious re-trigger" (zero jobs); it must now set deep_passed=false. A
rate-limit give-up (rc 75) or a permanent answer (rc 2) stops the retries at
once, a transient failure (rc 1) still retries, and a readable empty listing
still passes as a spurious re-trigger. These cases run the real script with
a stubbed gh_api_retry (the script only sources scripts/gh_helpers.sh when
gh_api_retry is not defined) and a no-op sleep.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = "scripts/stable_e2e_step_deep_verify.sh"
PHASES = 4  # Clarify, Plan, Implement, Review/Edit

_STUB = r"""
gh_api_retry() {
  echo call >> "$CALLS"
  local stub_rc="$STUB_RC"
  if [ -n "${STUB_RC_SEQ:-}" ]; then
    # Per-call rc: the Nth call takes the Nth word of STUB_RC_SEQ.
    local stub_n stub_seq
    stub_n=$(wc -l < "$CALLS")
    read -r -a stub_seq <<< "$STUB_RC_SEQ"
    stub_rc="${stub_seq[$(( (stub_n - 1) % ${#stub_seq[@]} ))]}"
  fi
  if [ "$stub_rc" -ne 0 ]; then
    return "$stub_rc"
  fi
  printf '%s' "$STUB_BODY"
}
sleep() { :; }
"""


def _run(rc: int, body: dict | str | None = None, rc_seq: str = "") -> tuple[subprocess.CompletedProcess, int, str]:
	if shutil.which("jq") is None:
		import pytest

		pytest.skip("jq is required")
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		calls = tmp_path / "calls"
		out = tmp_path / "github_output"
		out.write_text("", encoding="utf-8")
		env = {
			key: value
			for key, value in os.environ.items()
			if not key.startswith(("GH_RETRY", "GH_API_RETRY", "E2E_"))
		}
		env.update(
			CALLS=str(calls),
			STUB_RC=str(rc),
			STUB_RC_SEQ=rc_seq,
			STUB_BODY=body if isinstance(body, str) else json.dumps(body if body is not None else {"jobs": []}),
			GITHUB_OUTPUT=str(out),
			TEST_REPO="o/r",
			CLARIFY_RUN_ID="11",
			PLAN_RUN_ID="12",
			IMPL_RUN_ID="13",
			REVIEW_RUN_ID="14",
		)
		proc = subprocess.run(
			["bash", "-c", _STUB + f"source {SCRIPT}\n"],
			cwd=REPO_ROOT,
			env=env,
			capture_output=True,
			text=True,
			check=False,
			timeout=60,
		)
		n = len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
		return proc, n, out.read_text(encoding="utf-8")


def test_rate_limit_give_up_fails_closed_without_more_reads() -> None:
	proc, calls, output = _run(75)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "deep_passed=false" in output
	assert calls == PHASES  # no outer retry, no indexing-delay recheck
	assert "Jobs listing unavailable (gh_api_retry rc=75)" in proc.stdout
	assert "spurious re-trigger" not in proc.stdout


def test_permanent_failure_fails_closed_without_more_reads() -> None:
	proc, calls, output = _run(2)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "deep_passed=false" in output
	assert calls == PHASES
	assert "Jobs listing unavailable (gh_api_retry rc=2)" in proc.stdout
	assert "spurious re-trigger" not in proc.stdout


def test_transient_failure_retries_then_fails_closed() -> None:
	proc, calls, output = _run(1)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "deep_passed=false" in output
	assert calls == PHASES * (3 + 3)  # 3 outer rounds + 3 rechecks per phase
	assert "Jobs listing unavailable (gh_api_retry rc=1)" in proc.stdout
	assert "spurious re-trigger" not in proc.stdout


def test_readable_empty_listing_still_passes_as_spurious() -> None:
	proc, calls, output = _run(0, {"jobs": []})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "deep_passed=true" in output
	assert calls == PHASES * (1 + 3)
	assert "Run has no jobs after retries (spurious re-trigger) — OK" in proc.stdout
	assert "Jobs listing unavailable" not in proc.stdout


def test_empty_listing_then_failed_recheck_stays_spurious() -> None:
	# A listing read once (rc 0, zero jobs) counts as read: a recheck that then
	# hits a rate limit (rc 75) must not turn it into "listing unavailable".
	proc, calls, output = _run(0, {"jobs": []}, rc_seq="0 75")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "deep_passed=true" in output
	assert calls == PHASES * 2
	assert "Jobs listing unavailable" not in proc.stdout
	assert "Run has no jobs after retries (spurious re-trigger) — OK" in proc.stdout


def test_malformed_listing_then_failed_recheck_fails_closed() -> None:
	# An rc-0 reply that is not a jobs object is not a listing read: a recheck
	# that then hits a rate limit (rc 75) must fail closed, not pass as spurious.
	proc, calls, output = _run(0, "<html>not json</html>", rc_seq="0 75")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "deep_passed=false" in output
	assert calls == PHASES * 2
	assert "Jobs listing unavailable (gh_api_retry rc=75)" in proc.stdout
	assert "spurious re-trigger" not in proc.stdout


def test_successful_steps_pass() -> None:
	body = {"jobs": [{"conclusion": "success", "steps": [{"name": "Run", "conclusion": "success", "status": "completed"}]}]}
	proc, calls, output = _run(0, body)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "deep_passed=true" in output
	assert calls == PHASES


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	sys.exit(main())
