"""Regression tests for the checked clock read in
``check_integration_branch_staleness`` (scripts/orchestrate_poll_process.sh).

Finding SHELL-001 (ShellCheck SC2155) in
analysis/workflow-optimization-2026-10-01.md: the function used to start with
``local now_epoch="$(date +%s)"``. ``local`` hides the exit status of
``date``, so a failed clock read left ``now_epoch`` empty, and that empty
value then reached shell arithmetic and the ``jq --argjson`` state write.

The function now declares ``local now_epoch=""`` and reads the clock with a
checked status after the ``ORCH_INTEGRATION_STALE_ALERT_HOURS -eq 0`` early
return, so the disabled path never reads the clock at all.

CI runs these cases through tests/test_orchestrate_integration_ahead_by_gate.py,
which collects every ``test_*`` function from this module.
"""

from __future__ import annotations

import inspect
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
STALENESS_SIGNATURE = "check_integration_branch_staleness() {"
CHECKED_CLOCK_READ = 'if ! now_epoch="$(date +%s)" || ! [[ "${now_epoch}" =~ ^[0-9]+$ ]]; then'
CLOCK_WARNING = (
	"::warning::check_integration_branch_staleness: clock read failed; "
	"skipping the integration staleness check this cycle."
)
# Fake ``date`` as a shell function: a function shadows the real command, so
# the stub still applies when the temp directory is mounted noexec.
FAILING_DATE = "date() { return 1; }\n"
NON_NUMERIC_DATE = "date() { echo notanumber; return 0; }\n"


def _extract_staleness_function() -> str:
	"""Return the function text from its signature to the first line that is exactly ``}``."""
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	start = script.index(STALENESS_SIGNATURE)
	end = script.index("\n}\n", start) + 3
	return script[start:end]


def _run_staleness(date_script: str, alert_hours: str) -> dict:
	"""Run the extracted function under bash with ``date`` replaced by a shell function."""
	with tempfile.TemporaryDirectory(prefix="staleness_clock_") as td:
		tmp = Path(td)
		state_file = tmp / "state.json"
		state_file.write_text("{}", encoding="utf-8")
		calls_log = tmp / "stub_calls.log"
		harness = tmp / "harness.sh"
		harness.write_text(
			"tg_notify() { echo \"tg_notify\" >> " + shlex.quote(str(calls_log)) + "; }\n"
			"mark_integration_branch_squash_fresh() { echo \"mark_fresh $*\" >> " + shlex.quote(str(calls_log)) + "; }\n"
			+ date_script
			+ _extract_staleness_function()
			+ "check_integration_branch_staleness int-branch main 3\n"
			"exit $?\n",
			encoding="utf-8",
		)
		env = os.environ.copy()
		env["ORCH_INTEGRATION_STALE_ALERT_HOURS"] = alert_hours
		env["ORCH_INTEGRATION_STALE_REALERT_HOURS"] = "1"
		env["STATE_FILE"] = str(state_file)
		env["TRACKING_NUM"] = "1"
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		result = subprocess.run(
			["bash", str(harness)],
			env=env,
			capture_output=True,
			text=True,
			timeout=30,
		)
		return {
			"returncode": result.returncode,
			"stdout": result.stdout,
			"stderr": result.stderr,
			"state": state_file.read_bytes(),
			"tmp_exists": Path(str(state_file) + ".tmp").exists(),
			"stub_calls": calls_log.read_text(encoding="utf-8") if calls_log.exists() else "",
		}


def test_staleness_clock_read_is_status_checked():
	body = _extract_staleness_function()
	assert 'local now_epoch="$(date' not in body, body
	assert 'local now_epoch=""' in body, body
	assert CHECKED_CLOCK_READ in body, body
	assert CLOCK_WARNING in body, body
	read_idx = body.index(CHECKED_CLOCK_READ)
	disabled_idx = body.index('ORCH_INTEGRATION_STALE_ALERT_HOURS}" -eq 0')
	first_use_idx = body.index('mark_integration_branch_squash_fresh "${now_epoch}"')
	assert disabled_idx < read_idx < first_use_idx, (disabled_idx, read_idx, first_use_idx)


def test_staleness_clock_failure_returns_nonzero_and_leaves_state():
	outcome = _run_staleness(FAILING_DATE, "1")
	assert outcome["returncode"] != 0, outcome
	assert CLOCK_WARNING in outcome["stderr"], outcome
	assert outcome["state"] == b"{}", outcome
	assert outcome["tmp_exists"] is False, outcome
	assert outcome["stub_calls"] == "", outcome


def test_staleness_clock_non_numeric_output_is_rejected():
	outcome = _run_staleness(NON_NUMERIC_DATE, "1")
	assert outcome["returncode"] != 0, outcome
	assert CLOCK_WARNING in outcome["stderr"], outcome
	assert "notanumber" not in outcome["stderr"], outcome
	assert outcome["state"] == b"{}", outcome
	assert outcome["tmp_exists"] is False, outcome
	assert outcome["stub_calls"] == "", outcome


def test_staleness_disabled_path_skips_clock_read():
	outcome = _run_staleness(FAILING_DATE, "0")
	assert outcome["returncode"] == 0, outcome
	assert "clock read failed" not in outcome["stderr"], outcome
	assert outcome["state"] == b"{}", outcome
	assert outcome["tmp_exists"] is False, outcome
	assert outcome["stub_calls"] == "", outcome


def main(argv: list[str] | None = None) -> int:
	"""Direct ``python3 tests/<file>.py`` entrypoint."""
	selected_names = list(sys.argv[1:] if argv is None else argv)
	tests_by_name = {
		name: func
		for name, func in sorted(globals().items())
		if name.startswith("test_") and callable(func) and not inspect.signature(func).parameters
	}
	if selected_names:
		missing = [name for name in selected_names if name not in tests_by_name]
		for name in missing:
			print(f"  FAIL  {name}: unknown test name", flush=True)
		if missing:
			return 1
		named_tests = [(name, tests_by_name[name]) for name in selected_names]
	else:
		named_tests = list(tests_by_name.items())
	passed = 0
	failed = 0
	for name, func in named_tests:
		try:
			func()
			print(f"  PASS  {name}", flush=True)
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}", flush=True)
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total", flush=True)
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
