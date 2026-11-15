#!/usr/bin/env python3
"""Release-gate dispatch steps cancel a child run whose watch timed out (#9532).

In release run 43590943288 three dispatched child runs stayed queued for hours
with no runner. The watcher gave up, but nothing cancelled the children, so
the orphaned orchestrate child later created a tracking issue and Wave 1
issues that the gate's cleanup could not see. Each of the three dispatch steps
now reads the timed-out child's jobs once, prints how many are queued with no
runner, cancels the child (unless DISPATCH_WATCH_CANCEL_ON_TIMEOUT=false) and
still fails with the watcher's exit code.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
WATCHER = REPO_ROOT / "scripts" / "dispatch_and_watch_workflow_run.sh"

BLOCK_START = "# Dispatch-watch timeout: cancel the timed-out child run (#9532)."
BLOCK_END = "# End dispatch-watch timeout handling."
PIPE_LINE = '| tee "${DW_TIMEOUT_LOG}" || DW_TIMEOUT_RC=$?'

DISPATCH_STEPS = {
	"Dispatch & watch — validation-refresh": "validation-refresh",
	"Dispatch internal-orchestrate.yml with multi-issue project": "orchestrate",
	"Dispatch internal-validate.yml standalone": "validate",
}

JOBS_REPLY = (
	'{"jobs":[{"status":"queued","runner_name":null},'
	'{"status":"queued","runner_name":""},'
	'{"status":"completed","runner_name":"r1"}]}'
)


def _all_steps(text: str) -> dict[str, str]:
	"""Map step name to its run: body (same split as the step-scripts contract test)."""
	chunks = re.split(r"\n(?=      - name: )", text)[1:]
	steps: dict[str, str] = {}
	for chunk in chunks:
		lines = chunk.split("\n")
		name = lines[0][len("      - name: "):].strip()
		if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'":
			name = name[1:-1]
		run_lines: list[str] = []
		if "        run: |" in lines:
			for line in lines[lines.index("        run: |") + 1:]:
				if line and not line.startswith(" " * 10):
					break
				run_lines.append(line[10:])
		steps[name] = "\n".join(run_lines).rstrip("\n") + "\n" if run_lines else ""
	return steps


def _step_runs() -> dict[str, str]:
	steps = _all_steps(WORKFLOW.read_text(encoding="utf-8"))
	runs = {}
	for name in DISPATCH_STEPS:
		assert name in steps, name
		runs[name] = steps[name]
	return runs


def _block(run: str) -> str:
	start = run.index(BLOCK_START)
	end = run.index(BLOCK_END, start)
	return run[start:end + len(BLOCK_END)]


def test_three_dispatch_steps_carry_identical_timeout_block() -> None:
	blocks = {name: _block(run) for name, run in _step_runs().items()}
	assert len(set(blocks.values())) == 1, "the three timeout blocks must be byte-identical"
	block = next(iter(blocks.values()))
	assert 'exit "${DW_TIMEOUT_RC}"' in block
	assert "/cancel" in block and "-X POST" in block
	assert "DISPATCH_WATCH_CANCEL_ON_TIMEOUT:-true" in block
	assert "43590943288" in block
	assert "${{" not in block


def test_watcher_call_is_piped_through_tee_before_the_block() -> None:
	for name, run in _step_runs().items():
		assert run.count("bash ./scripts/dispatch_and_watch_workflow_run.sh") == 1, name
		assert run.count(PIPE_LINE) == 1, name
		assert 'DW_TIMEOUT_LOG="$(mktemp)"' in run, name
		call = run.index("bash ./scripts/dispatch_and_watch_workflow_run.sh")
		assert run.index('DW_TIMEOUT_LOG="$(mktemp)"') < call < run.index(PIPE_LINE) < run.index(BLOCK_START), name


def test_watcher_output_lines_the_block_parses_are_unchanged() -> None:
	text = WATCHER.read_text(encoding="utf-8")
	assert 'echo "Watching run #${NEW_ID}"' in text
	assert 'echo "::error::${DISPLAY_NAME} run #${NEW_ID} timed out"' in text


def _run_step(
	run: str,
	watcher_body: str,
	*,
	cancel_switch: str | None = None,
	jobs_fail: bool = False,
	cancel_fail: bool = False,
) -> tuple[int, str, list[str]]:
	bash = shutil.which("bash")
	assert bash is not None
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		(tmp / "scripts").mkdir()
		watcher = tmp / "scripts" / "dispatch_and_watch_workflow_run.sh"
		watcher.write_text("#!/usr/bin/env bash\n" + watcher_body, encoding="utf-8")
		bindir = tmp / "bin"
		bindir.mkdir()
		gh_log = tmp / "gh.log"
		gh_log.write_text("", encoding="utf-8")
		gh = bindir / "gh"
		gh.write_text(
			"#!/usr/bin/env bash\n"
			'echo "$*" >> "${STUB_GH_LOG}"\n'
			'case "$*" in\n'
			'  *"/jobs?per_page=100"*) [ "${STUB_JOBS_FAIL:-0}" = "1" ] && exit 1; printf \'%s\\n\' "${STUB_JOBS_REPLY}" ;;\n'
			'  *"/cancel"*) [ "${STUB_CANCEL_FAIL:-0}" = "1" ] && exit 1; echo "{}" ;;\n'
			'  *) echo "" ;;\n'
			"esac\n",
			encoding="utf-8",
		)
		gh.chmod(0o755)
		sleep = bindir / "sleep"
		sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
		sleep.chmod(0o755)
		output = tmp / "github_output"
		output.write_text("", encoding="utf-8")
		env = {
			"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
			"HOME": str(tmp),
			"TEST_REPO": "owner/repo",
			"GITHUB_RUN_ID": "555",
			"GITHUB_OUTPUT": str(output),
			"STUB_GH_LOG": str(gh_log),
			"STUB_JOBS_REPLY": JOBS_REPLY,
			"STUB_JOBS_FAIL": "1" if jobs_fail else "0",
			"STUB_CANCEL_FAIL": "1" if cancel_fail else "0",
		}
		if cancel_switch is not None:
			env["DISPATCH_WATCH_CANCEL_ON_TIMEOUT"] = cancel_switch
		proc = subprocess.run(
			[bash, "-c", run],
			cwd=tmp,
			env=env,
			capture_output=True,
			text=True,
			check=False,
		)
		calls = [line for line in gh_log.read_text(encoding="utf-8").splitlines() if line]
	return proc.returncode, proc.stdout + proc.stderr, calls


def _timeout_watcher(display: str) -> str:
	return f'echo "Watching run #4242"\necho "::error::{display} run #4242 timed out"\nexit 1\n'


def test_timed_out_watch_reports_jobs_cancels_child_and_fails() -> None:
	for name, run in _step_runs().items():
		rc, out, calls = _run_step(run, _timeout_watcher(DISPATCH_STEPS[name]))
		assert rc == 1, (name, out)
		assert "::warning::Timed-out child run #4242: jobs=3 queued_without_runner=2" in out, (name, out)
		cancels = [c for c in calls if "/cancel" in c]
		assert cancels == ["api -X POST repos/owner/repo/actions/runs/4242/cancel"], (name, calls)
		assert "Cancel requested for timed-out child run #4242" in out, (name, out)


def test_switch_off_skips_the_cancel() -> None:
	for switch in ("false", "FALSE"):
		for name, run in _step_runs().items():
			rc, out, calls = _run_step(run, _timeout_watcher(DISPATCH_STEPS[name]), cancel_switch=switch)
			assert rc == 1, (name, out)
			assert not [c for c in calls if "/cancel" in c], (name, calls)
			assert "Cancel of timed-out child run #4242 skipped (DISPATCH_WATCH_CANCEL_ON_TIMEOUT=false)" in out


def test_failed_cancel_only_warns() -> None:
	for name, run in _step_runs().items():
		rc, out, _calls = _run_step(run, _timeout_watcher(DISPATCH_STEPS[name]), cancel_fail=True)
		assert rc == 1, (name, out)
		assert "::warning::Could not cancel timed-out child run #4242" in out, (name, out)


def test_failed_jobs_read_reports_unknown_and_still_cancels() -> None:
	for name, run in _step_runs().items():
		rc, out, calls = _run_step(run, _timeout_watcher(DISPATCH_STEPS[name]), jobs_fail=True)
		assert rc == 1, (name, out)
		assert "jobs=unknown queued_without_runner=unknown" in out, (name, out)
		assert [c for c in calls if "/cancel" in c], (name, calls)


def test_non_timeout_failure_cancels_nothing() -> None:
	watcher = 'echo "Watching run #4242"\necho "::error::validate run #4242 concluded failure"\nexit 1\n'
	for name, run in _step_runs().items():
		rc, out, calls = _run_step(run, watcher)
		assert rc == 1, (name, out)
		assert calls == [], (name, calls)


def test_successful_watch_makes_no_extra_calls() -> None:
	run = _step_runs()["Dispatch internal-validate.yml standalone"]
	rc, out, calls = _run_step(run, 'echo "Watching run #4242"\nexit 0\n')
	assert rc == 0, out
	assert calls == [], calls


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
