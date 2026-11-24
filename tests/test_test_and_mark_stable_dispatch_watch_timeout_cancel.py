#!/usr/bin/env python3
"""Release-gate dispatch steps cancel a child run whose watch timed out (#9532).

In release run 43590943288 three dispatched child runs stayed queued for hours
with no runner. The watcher gave up, but nothing cancelled the children, so
the orphaned orchestrate child later created a tracking issue and Wave 1
issues that the gate's cleanup could not see. Each dispatch step that waits
for its child to complete (the three that timed out, plus update_workflows and
internal-memory-maintenance) now reads the timed-out child's jobs once, prints
how many are queued with no runner, cancels the child (unless
DISPATCH_WATCH_CANCEL_ON_TIMEOUT=false) and still fails with the watcher's exit
code.
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
	"Dispatch & watch — update_workflows (allow_workflow_edits=false)": "update_workflows",
	"Dispatch & watch — internal-memory-maintenance": "internal-memory-maintenance",
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


def test_dispatch_steps_carry_identical_timeout_block() -> None:
	blocks = {name: _block(run) for name, run in _step_runs().items()}
	assert len(set(blocks.values())) == 1, "the timeout blocks must be byte-identical"
	block = next(iter(blocks.values()))
	assert 'exit "${DW_TIMEOUT_RC}"' in block
	assert "/cancel" in block and "-X POST" in block
	assert "DISPATCH_WATCH_CANCEL_ON_TIMEOUT:-true" in block
	assert "43590943288" in block
	assert "${{" not in block


JOB_ENV_SWITCH = "      DISPATCH_WATCH_CANCEL_ON_TIMEOUT: ${{ vars.DISPATCH_WATCH_CANCEL_ON_TIMEOUT || 'true' }}\n"


def test_owning_jobs_map_the_cancel_switch_from_a_repo_variable() -> None:
	"""Operators flip the switch with a repo variable, not a workflow edit."""
	text = WORKFLOW.read_text(encoding="utf-8")
	jobs = re.split(r"\n(?=  [A-Za-z0-9_-]+:\n)", text)
	for name in DISPATCH_STEPS:
		owners = [job for job in jobs if f"      - name: {name}\n" in job or f'      - name: "{name}"\n' in job]
		assert len(owners) == 1, name
		job = owners[0]
		steps_at = job.index("    steps:\n")
		assert JOB_ENV_SWITCH in job[:steps_at], name


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
	jobs_reply: str = JOBS_REPLY,
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
			"STUB_JOBS_REPLY": jobs_reply,
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


def test_unrecognised_switch_value_skips_the_cancel_and_warns() -> None:
	"""Repo boolean convention: a value other than true/false turns the cancel off."""
	for switch in ("off", "0", "flase"):
		for name, run in _step_runs().items():
			rc, out, calls = _run_step(run, _timeout_watcher(DISPATCH_STEPS[name]), cancel_switch=switch)
			assert rc == 1, (name, out)
			assert not [c for c in calls if "/cancel" in c], (name, switch, calls)
			assert "::warning::DISPATCH_WATCH_CANCEL_ON_TIMEOUT is neither true nor false" in out, (name, out)
			assert switch not in out.split("::warning::DISPATCH_WATCH_CANCEL_ON_TIMEOUT", 1)[1].split("\n", 1)[0]
			# The skip line must not claim a value of false the operator never set.
			assert "Cancel of timed-out child run #4242 skipped (DISPATCH_WATCH_CANCEL_ON_TIMEOUT unrecognised)" in out, (name, out)
			assert "DISPATCH_WATCH_CANCEL_ON_TIMEOUT=false" not in out, (name, switch, out)


def test_true_or_empty_switch_still_cancels() -> None:
	run = _step_runs()["Dispatch internal-validate.yml standalone"]
	for switch in ("true", "TRUE", ""):
		rc, out, calls = _run_step(run, _timeout_watcher("validate"), cancel_switch=switch)
		assert rc == 1, out
		assert [c for c in calls if "/cancel" in c], (switch, calls)
		assert "neither true nor false" not in out, out


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


def test_jobs_reply_without_jobs_list_reports_unknown() -> None:
	run = _step_runs()["Dispatch internal-validate.yml standalone"]
	rc, out, calls = _run_step(run, _timeout_watcher("validate"), jobs_reply='{"message":"Not Found"}')
	assert rc == 1, out
	assert "jobs=unknown queued_without_runner=unknown" in out, out
	assert [c for c in calls if "/cancel" in c], calls


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


REAL_WATCHER_GH_STUB = """#!/usr/bin/env bash
echo "$*" >> "${STUB_GH_LOG}"
[ "${1:-}" = "workflow" ] && exit 0
shift
jq_expr=""
path=""
while [ "$#" -gt 0 ]; do
	case "$1" in
		--jq) jq_expr="$2"; shift 2 ;;
		-X) shift 2 ;;
		*) path="$1"; shift ;;
	esac
done
case "${path}" in
	*"/runs?event=workflow_dispatch&per_page=1") body='{"workflow_runs":[]}' ;;
	*"/runs?event=workflow_dispatch&"*) body='{"workflow_runs":[{"id":4242,"created_at":"2026-11-15T01:26:16Z"}]}' ;;
	*"/actions/runs/4242/jobs?per_page=100") body="${STUB_JOBS_REPLY}" ;;
	*"/actions/runs/4242/cancel") body='{}' ;;
	*"/actions/runs/4242") body='{"status":"queued","conclusion":null}' ;;
	*) exit 1 ;;
esac
if [ -n "${jq_expr}" ]; then
	printf '%s' "${body}" | jq -r "${jq_expr}"
else
	printf '%s\\n' "${body}"
fi
"""


def _run_real_watcher(cancel_switch: str | None = None) -> tuple[int, str, list[str]]:
	"""Run the unmodified watcher against a stub gh whose child run stays queued,
	followed by the step's timeout block, so a drift between the watcher's
	timeout line and the block's sed extraction fails here."""
	bash = shutil.which("bash")
	assert bash is not None
	block = _block(_step_runs()["Dispatch internal-validate.yml standalone"])
	script = (
		"set -euo pipefail\n"
		'DW_TIMEOUT_LOG="$(mktemp)"\n'
		"DW_TIMEOUT_RC=0\n"
		"bash ./scripts/dispatch_and_watch_workflow_run.sh \\\n"
		'  --repo "${TEST_REPO}" \\\n'
		"  --workflow internal-validate.yml \\\n"
		"  --display-name validate \\\n"
		"  --registration-timeout-secs 30 \\\n"
		"  --registration-poll-interval-secs 1 \\\n"
		"  --completion-timeout-secs 2 \\\n"
		"  --completion-poll-interval-secs 1 \\\n"
		f"  {PIPE_LINE}\n"
		f"{block}\n"
	)
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		(tmp / "scripts").mkdir()
		for helper in ("dispatch_and_watch_workflow_run.sh", "comprehensive_test_and_release_gh_api.sh"):
			shutil.copyfile(REPO_ROOT / "scripts" / helper, tmp / "scripts" / helper)
		bindir = tmp / "bin"
		bindir.mkdir()
		gh_log = tmp / "gh.log"
		gh_log.write_text("", encoding="utf-8")
		gh = bindir / "gh"
		gh.write_text(REAL_WATCHER_GH_STUB, encoding="utf-8")
		gh.chmod(0o755)
		output = tmp / "github_output"
		output.write_text("", encoding="utf-8")
		env = {
			"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
			"HOME": str(tmp),
			"TEST_REPO": "owner/repo",
			"GITHUB_OUTPUT": str(output),
			"STUB_GH_LOG": str(gh_log),
			"STUB_JOBS_REPLY": JOBS_REPLY,
		}
		if cancel_switch is not None:
			env["DISPATCH_WATCH_CANCEL_ON_TIMEOUT"] = cancel_switch
		proc = subprocess.run(
			[bash, "-c", script],
			cwd=tmp,
			env=env,
			capture_output=True,
			text=True,
			check=False,
			timeout=120,
		)
		calls = [line for line in gh_log.read_text(encoding="utf-8").splitlines() if line]
	return proc.returncode, proc.stdout + proc.stderr, calls


def test_real_watcher_against_queued_stub_times_out_and_cancels_once() -> None:
	"""The name predates #9706 and is kept: the run is now cancelled twice,
	once by the watcher and once by the workflow's timeout block."""
	rc, out, calls = _run_real_watcher()
	assert rc == 1, out
	assert "status=queued" in out, out
	assert "::error::validate run #4242 timed out" in out, out
	assert "::warning::Timed-out child run #4242: jobs=3 queued_without_runner=2" in out, out
	cancels = [c for c in calls if "/cancel" in c]
	# Since #9706 the watcher cancels the timed-out run itself, and the
	# workflow block then posts its own cancel: the watcher's comes first.
	assert cancels == ["api -X POST repos/owner/repo/actions/runs/4242/cancel"] * 2, calls


def test_real_watcher_with_switch_off_issues_no_cancel() -> None:
	rc, out, calls = _run_real_watcher(cancel_switch="false")
	assert rc == 1, out
	assert "::error::validate run #4242 timed out" in out, out
	assert not [c for c in calls if "/cancel" in c], calls


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())


def test_gate_enables_the_runner_wait_stop_and_keeps_the_timeout_blocks() -> None:
	"""Issue #10037 (G3): the gate opts into the watcher's runner_wait stop at
	workflow level (no inline run: growth); every call site keeps its timeout
	block, and the watcher keeps the stop off by default for other callers."""
	text = WORKFLOW.read_text(encoding="utf-8")
	env_block = text[text.index("\nenv:\n"):text.index("\npermissions:\n")]
	assert '  DISPATCH_WATCH_STOP_RELEASE_GATE: "true"\n' in env_block
	for name, run in _step_runs().items():
		assert BLOCK_START in run and BLOCK_END in run, name
	watcher = WATCHER.read_text(encoding="utf-8")
	assert '"${DISPATCH_WATCH_STOP_RELEASE_GATE:-false}"' in watcher
	assert 'echo "::error::${DISPLAY_NAME} run #${NEW_ID} timed out"\nexit 1\n' in watcher
