#!/usr/bin/env python3
"""Contract checks for the live-log cooldown in the release gate's Phase 4
review wait (``wait-review`` in ``test-and-mark-stable.yml``).

Run 35696603462 made 275 failed ``/actions/jobs/{id}/logs`` fetches in about
an hour because the wait loop retried an unavailable job log on every poll.
After 3 failures in a row the loop now skips every live-log fetch for 300
seconds of its ``ELAPSED`` counter; a success resets the count.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

STEP_MARKER = "        id: wait-review\n"
BLOCK_START = "# BEGIN live-log cooldown (wfopt-0922-3)"
BLOCK_END = "# END live-log cooldown (wfopt-0922-3)"
LOOP_START = "          while true; do\n"
FAILURE_LINE = (
	"⚠ Live-log fetch failed for job ${JOB_ID} — editor-noop shortcut and "
	"reviewer-count shortcut are inactive this iteration"
)
COOLDOWN_LINE = "⏸ Live-log fetch failed"
JOBS_LOG_FETCH_RE = re.compile(
	r'gh_api_safe_quiet_print "repos/\$\{TEST_REPO\}/actions/jobs/[^"]*/logs"'
)


def _step_text() -> str:
	wf = WORKFLOW.read_text(encoding="utf-8")
	start = wf.find(STEP_MARKER)
	assert start != -1, "wait-review step not found"
	end = wf.find("\n      - name:", start)
	assert end != -1, "end of wait-review step not found"
	return wf[start:end]


def _cooldown_block() -> str:
	step = _step_text()
	start = step.find(BLOCK_START)
	end = step.find(BLOCK_END, start)
	assert start != -1 and end != -1, "live-log cooldown markers missing"
	return step[start:end + len(BLOCK_END)]


def _loop_text() -> str:
	step = _step_text()
	start = step.find(LOOP_START)
	assert start != -1, "wait loop not found"
	return step[start:]


def test_cooldown_constants_and_message() -> None:
	block = _cooldown_block()
	assert "LIVE_LOG_FAILURE_THRESHOLD=3\n" in block
	assert "LIVE_LOG_COOLDOWN_SECS=300\n" in block
	assert "LIVE_LOG_FETCH_FAILURES=0\n" in block
	assert "LIVE_LOG_COOLDOWN_UNTIL=0\n" in block
	assert COOLDOWN_LINE in block
	assert "skipping live-log probes for ${LIVE_LOG_COOLDOWN_SECS}s (status polling continues)" in block
	assert "LIVE_LOG_COOLDOWN_UNTIL=$((ELAPSED + LIVE_LOG_COOLDOWN_SECS))" in block


def test_cooldown_state_initialised_before_loop() -> None:
	step = _step_text()
	assert step.index(BLOCK_START) < step.index(LOOP_START)
	assert step.index("ELAPSED=0\n") < step.index(BLOCK_START)


def test_every_jobs_log_fetch_in_wait_step_is_gated() -> None:
	# Scan the whole step, not only the loop: the bait-coverage helper
	# review_run_checked_out_bait() is defined before the loop.
	lines = _step_text().splitlines()
	fetch_indexes = [i for i, line in enumerate(lines) if JOBS_LOG_FETCH_RE.search(line)]
	assert len(fetch_indexes) >= 3, fetch_indexes
	for idx in fetch_indexes:
		window = "\n".join(lines[max(0, idx - 6):idx + 1])
		assert "live_log_fetch_allowed" in window, lines[idx]


def test_success_resets_and_failure_counts() -> None:
	loop = _loop_text()
	assert loop.count("live_log_fetch_record ok") == 2
	assert loop.count("live_log_fetch_record fail") == 2
	block = _cooldown_block()
	ok_branch = block[block.index('if [ "$1" = "ok" ]; then'):]
	first_lines = ok_branch.splitlines()[1:3]
	assert any(line.strip() == "LIVE_LOG_FETCH_FAILURES=0" for line in first_lines)


def test_bait_coverage_fetch_records_outcome() -> None:
	step = _step_text()
	start = step.index("review_run_checked_out_bait() {")
	end = step.index("require_bait_review_coverage() {", start)
	helper = step[start:end]
	assert "live_log_fetch_allowed" in helper
	assert helper.count("live_log_fetch_record ok") == 1
	assert helper.count("live_log_fetch_record fail") == 1


def test_existing_failure_line_preserved() -> None:
	assert FAILURE_LINE in _loop_text()


def _model(iterations: int, results: str, step: int = 30) -> tuple[int, int]:
	failures = 0
	until = 0
	calls = 0
	cooldowns = 0
	elapsed = 0
	for _ in range(iterations):
		if elapsed >= until:
			outcome = results[calls] if calls < len(results) else "f"
			calls += 1
			if outcome == "s":
				failures = 0
			else:
				failures += 1
				if failures >= 3:
					until = elapsed + 300
					failures = 0
					cooldowns += 1
		elapsed += step
	return calls, cooldowns


def _run_harness(iterations: int, results: str) -> tuple[int, int]:
	bash = shutil.which("bash")
	assert bash is not None
	block = textwrap.dedent(_cooldown_block())
	harness = (
		"set -euo pipefail\n"
		"ELAPSED=0\n"
		f"{block}\n"
		'gh_api_safe_quiet_print() {\n'
		'  local n\n'
		'  echo x >> "$CALLS_FILE"\n'
		'  n=$(wc -l < "$CALLS_FILE")\n'
		'  [ "${FAKE_RESULTS:$((n - 1)):1}" = "s" ]\n'
		'}\n'
		'i=0\n'
		'while [ "$i" -lt "$ITERATIONS" ]; do\n'
		'  if ! live_log_fetch_allowed; then\n'
		'    :\n'
		'  elif gh_api_safe_quiet_print "repos/x/actions/jobs/1/logs" > /dev/null; then\n'
		'    live_log_fetch_record ok\n'
		'  else\n'
		'    live_log_fetch_record fail\n'
		'  fi\n'
		'  ELAPSED=$((ELAPSED + 30))\n'
		'  i=$((i + 1))\n'
		'done\n'
	)
	with tempfile.TemporaryDirectory() as tmp:
		calls_file = Path(tmp) / "calls"
		calls_file.write_text("", encoding="utf-8")
		env = dict(os.environ)
		env.update(
			{
				"CALLS_FILE": str(calls_file),
				"FAKE_RESULTS": results,
				"ITERATIONS": str(iterations),
			}
		)
		result = subprocess.run(
			[bash, "-c", harness],
			capture_output=True,
			text=True,
			env=env,
			check=False,
		)
		assert result.returncode == 0, result.stderr
		calls = len(calls_file.read_text(encoding="utf-8").splitlines())
	return calls, result.stderr.count(COOLDOWN_LINE)


def test_behaviour_schedule_always_failing() -> None:
	if shutil.which("bash") is None:
		return
	# Fetches at ELAPSED 0,30,60 / 360,390,420 / 720,750,780 / 1080,1110,1140.
	assert _model(40, "") == (12, 4)
	assert _run_harness(40, "") == (12, 4)


def test_behaviour_success_resets_counter() -> None:
	if shutil.which("bash") is None:
		return
	results = "ffsfff"
	expected = _model(10, results)
	assert expected == (6, 1)
	assert _run_harness(10, results) == expected


def test_workflow_step_parses_as_bash() -> None:
	bash = shutil.which("bash")
	if bash is None:
		return
	result = subprocess.run(
		[bash, "-n"],
		input=textwrap.dedent(_cooldown_block()),
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stderr


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
