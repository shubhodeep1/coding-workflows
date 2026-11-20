#!/usr/bin/env python3
"""Phase 7's wait budget counts from when the cancel run leaves the queue (#9794).

Release gate run 44490333814 failed Phase 7 with ``timeout`` while
cancel_on_pr_close run 44527274737 was still ``queued``: no runner had picked
it up when the 10-minute PHASE7_WAIT_BUDGET_MINUTES ran out. The budget now
starts once the run is seen out of queued/pending/waiting/requested. A run
that never leaves the queue ends the wait after PHASE7_QUEUE_WAIT_CAP_MINUTES
with ``status=queued_timeout``. Every deadline stops
E2E_FINALIZATION_RESERVE_MINUTES before the job timeout, but never earlier
than the old start + budget.

These tests run both real wait loops of the step with a fake clock and a run
whose status changes over time.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import expanded_test_and_mark_stable_text  # noqa: E402

HELPERS_START = "# ── Phase 7 wait deadline helpers"
HELPERS_END = "# ── end Phase 7 wait deadline helpers ──"
OPEN_WAIT_START = "DEADLINE=$(( $(date +%s) + (PHASE7_WAIT_BUDGET_MINUTES * 60) ))"
OPEN_CONCLUSION_CHECK = 'if [ "${NEW_CONCLUSION}" != "success" ]; then'
CLOSED_WAIT_START = "PHASE7_WAIT_START=$(date +%s)"
CLOSED_CONCLUSION_CHECK = 'if [ "${EXISTING_CONCLUSION}" != "success" ]; then'
START = 1000

# The run's status by fake-clock time: each "<epoch> <status> <conclusion>"
# line applies from that epoch on; status "none" lists no run at all.
PLAN_STATUS = r'''
phase7_plan_status() {
	local now t s c
	now=$(cat "$CLOCK")
	PLAN_S=""
	PLAN_C="null"
	while read -r t s c; do
		[ -n "${t}" ] || continue
		if [ "${now}" -ge "${t}" ]; then
			PLAN_S="${s}"
			PLAN_C="${c}"
		fi
	done < "$PLAN_FILE"
}
phase7_plan_json() {
	jq -nc --arg s "${PLAN_S}" --arg c "${PLAN_C}" \
		'{id: 200, event: "pull_request", status: $s, conclusion: (if $c == "null" then null else $c end)}'
}
'''


def _slice_between(text: str, start_marker: str, end_marker: str) -> str:
	start = text.find(start_marker)
	assert start != -1, f"Missing marker: {start_marker}"
	end = text.find(end_marker, start)
	assert end != -1, f"Missing marker after {start_marker}: {end_marker}"
	return text[start:end]


def _phase7_script() -> str:
	return _slice_between(
		expanded_test_and_mark_stable_text(),
		'- name: "Phase 7: Close PR and verify cancel_on_pr_close fires"',
		'- name: "Phase 7b: Soft-error analyser (cancel-on-pr-close)"',
	)


def _dedented(block: str) -> str:
	return textwrap.dedent("          " + block)


def _run(body: str, plan: list[tuple[int, str, str]], **env_overrides: str) -> tuple[subprocess.CompletedProcess, str, int]:
	script = _phase7_script()
	helpers = _dedented(_slice_between(script, HELPERS_START, HELPERS_END))
	with tempfile.TemporaryDirectory() as tmp:
		clock = os.path.join(tmp, "clock")
		output = os.path.join(tmp, "github_output")
		plan_file = os.path.join(tmp, "plan")
		with open(clock, "w", encoding="utf-8") as fh:
			fh.write(f"{START}\n")
		open(output, "w", encoding="utf-8").close()
		with open(plan_file, "w", encoding="utf-8") as fh:
			fh.write("".join(f"{t} {s} {c}\n" for t, s, c in plan))
		prelude = "\n".join(
			[
				"set -euo pipefail",
				'date() { if [ "${1:-}" = "+%s" ]; then cat "$CLOCK"; else command date "$@"; fi; }',
				'sleep() { echo $(( $(cat "$CLOCK") + $1 )) > "$CLOCK"; }',
				PLAN_STATUS,
				'phase7_list_cancel_runs() { phase7_plan_status; if [ "${PLAN_S}" = "none" ]; then PHASE7_CANCEL_RUNS_JSON=\'{"workflow_runs":[]}\'; else PHASE7_CANCEL_RUNS_JSON="{\\"workflow_runs\\":[$(phase7_plan_json)]}"; fi; return 0; }',
				'gh() { phase7_plan_status; if [ "${PLAN_S}" = "fail" ]; then return 1; fi; phase7_plan_json; }',
				"PR_NUMBER=42",
				"PR_CLOSED_AT=2026-10-20T00:00:00Z",
				"PRE_RUN_ID=100",
				"TEST_REPO=o/r",
				'PHASE7_RUN_STARTED_AT=""',
				helpers,
				"",
			]
		)
		env = dict(os.environ, CLOCK=clock, PLAN_FILE=plan_file, GITHUB_OUTPUT=output, PHASE7_WAIT_BUDGET_MINUTES="1")
		for name in ("PHASE7_QUEUE_WAIT_CAP_MINUTES", "E2E_JOB_STARTED_EPOCH", "E2E_JOB_TIMEOUT_MINUTES", "E2E_FINALIZATION_RESERVE_MINUTES"):
			env.pop(name, None)
		env.update(env_overrides)
		proc = subprocess.run(
			["bash", "-c", prelude + body + "\necho REACHED_CONCLUSION_CHECK\n"],
			env=env,
			capture_output=True,
			text=True,
			check=False,
			timeout=120,
		)
		with open(output, encoding="utf-8") as fh:
			github_output = fh.read()
		with open(clock, encoding="utf-8") as fh:
			end_clock = int(fh.read().strip())
	return proc, github_output, end_clock


def _open_pr_wait(plan: list[tuple[int, str, str]], **env: str) -> tuple[subprocess.CompletedProcess, str, int]:
	script = _phase7_script()
	branch = script[script.index("# PR is still open — exercise the close path end to end."):]
	return _run(_dedented(_slice_between(branch, OPEN_WAIT_START, OPEN_CONCLUSION_CHECK)), plan, **env)


def _closed_pr_wait(plan: list[tuple[int, str, str]], **env: str) -> tuple[subprocess.CompletedProcess, str, int]:
	block = _dedented(_slice_between(_phase7_script(), CLOSED_WAIT_START, CLOSED_CONCLUSION_CHECK))
	first = plan[0]
	prefix = f'EXISTING_RUN_ID=200\nEXISTING_STATUS="{first[1]}"\nEXISTING_CONCLUSION=""\n'
	return _run(prefix + block, plan, **env)


def test_queued_time_does_not_consume_the_budget() -> None:
	# Queued for 5 minutes (five times the 1-minute budget), then runs for
	# 40 s and succeeds: the wait must reach the conclusion check.
	plan = [(START, "queued", "null"), (START + 300, "in_progress", "null"), (START + 340, "completed", "success")]
	for wait in (_open_pr_wait, _closed_pr_wait):
		proc, out, _ = wait(plan)
		assert proc.returncode == 0, (wait.__name__, proc.stdout, proc.stderr)
		assert "REACHED_CONCLUSION_CHECK" in proc.stdout, wait.__name__
		assert "status=" not in out, (wait.__name__, out)


def test_in_progress_time_consumes_the_budget() -> None:
	# Leaves the queue at +300 s and then never completes: the 1-minute
	# budget runs from that point, so the wait ends about 60 s later with
	# the plain timeout status.
	plan = [(START, "queued", "null"), (START + 300, "in_progress", "null")]
	for wait in (_open_pr_wait, _closed_pr_wait):
		proc, out, end = wait(plan)
		assert proc.returncode == 1, (wait.__name__, proc.stdout, proc.stderr)
		lines = out.splitlines()
		assert "status=timeout" in lines, (wait.__name__, out)
		assert "cancel_run_id=200" in lines, (wait.__name__, out)
		assert "did not complete within 1m (status=in_progress)" in proc.stdout, wait.__name__
		assert START + 300 + 60 - 1 <= end <= START + 300 + 60 + 35, (wait.__name__, end)
		assert "REACHED_CONCLUSION_CHECK" not in proc.stdout


def test_queue_cap_ends_a_wait_on_a_run_that_never_starts() -> None:
	plan = [(START, "queued", "null")]
	for wait in (_open_pr_wait, _closed_pr_wait):
		proc, out, end = wait(plan, PHASE7_QUEUE_WAIT_CAP_MINUTES="5")
		assert proc.returncode == 1, (wait.__name__, proc.stdout, proc.stderr)
		lines = out.splitlines()
		assert "status=queued_timeout" in lines, (wait.__name__, out)
		assert "status=timeout" not in lines, (wait.__name__, out)
		assert "cancel_run_id=200" in lines, (wait.__name__, out)
		assert "never left the queue within 5m (status=queued)" in proc.stdout, wait.__name__
		assert START + 300 - 1 <= end <= START + 300 + 35, (wait.__name__, end)


def test_failed_status_read_keeps_the_queue_cap() -> None:
	# The closed-PR loop reads the run directly; from +700 s (past the
	# 1-minute budget) every read fails. The last known status (queued) must
	# keep the queue-cap deadline and the queued_timeout verdict.
	plan = [(START, "queued", "null"), (START + 700, "fail", "null")]
	proc, out, end = _closed_pr_wait(plan, PHASE7_QUEUE_WAIT_CAP_MINUTES="20")
	assert proc.returncode == 1, (proc.stdout, proc.stderr)
	lines = out.splitlines()
	assert "status=queued_timeout" in lines, out
	assert "status=timeout" not in lines, out
	assert START + 1200 - 1 <= end <= START + 1200 + 35, end


def test_queue_cap_default_is_sixty_minutes() -> None:
	proc, out, end = _open_pr_wait([(START, "pending", "null")])
	assert "status=queued_timeout" in out.splitlines(), out
	assert "never left the queue within 60m (status=pending)" in proc.stdout
	assert START + 3600 - 1 <= end <= START + 3600 + 35, end


def test_job_time_left_caps_the_queued_wait() -> None:
	# The job started at START with 10 minutes and a 6-minute finalization
	# reserve, so Phase 7 must stop by START + 240 s despite the 60m cap.
	env = {"E2E_JOB_STARTED_EPOCH": str(START), "E2E_JOB_TIMEOUT_MINUTES": "10", "E2E_FINALIZATION_RESERVE_MINUTES": "6"}
	for wait in (_open_pr_wait, _closed_pr_wait):
		proc, out, end = wait([(START, "queued", "null")], **env)
		assert "status=queued_timeout" in out.splitlines(), (wait.__name__, out)
		assert START + 240 - 1 <= end <= START + 240 + 35, (wait.__name__, end)


def test_job_cap_never_shortens_the_old_budget() -> None:
	# A job cap already in the past still leaves the old start + budget wait.
	env = {"E2E_JOB_STARTED_EPOCH": str(START), "E2E_JOB_TIMEOUT_MINUTES": "1", "E2E_FINALIZATION_RESERVE_MINUTES": "1"}
	proc, out, end = _open_pr_wait([(START, "queued", "null")], **env)
	assert "status=queued_timeout" in out.splitlines(), out
	assert START + 60 - 1 <= end <= START + 60 + 35, end


def test_no_run_keeps_the_old_budget() -> None:
	# No run registered: the wait still ends after the plain budget.
	proc, out, end = _open_pr_wait([(START, "none", "null")])
	assert proc.returncode == 1, (proc.stdout, proc.stderr)
	assert "status=no_run" in out.splitlines(), out
	assert end <= START + 60 + 35, end


def test_new_queue_cap_is_a_validated_job_budget() -> None:
	text = expanded_test_and_mark_stable_text()
	job = _slice_between(text, "  e2e-smoke-test:\n", "  validate-scripts:\n")
	assert "\n      PHASE7_QUEUE_WAIT_CAP_MINUTES: 60\n" in job
	prerequisites = _slice_between(job, "- name: Validate prerequisites", 'echo "Target repo:')
	loop = prerequisites[prerequisites.index("for timeout_value in"):prerequisites.index("done")]
	assert '"${PHASE7_QUEUE_WAIT_CAP_MINUTES}"' in loop
