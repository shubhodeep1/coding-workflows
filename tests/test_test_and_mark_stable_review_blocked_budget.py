#!/usr/bin/env python3
"""Contract checks for the release gate's review-blocked E2E budget and run pinning."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import textwrap
from pathlib import Path
from runpy import run_path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
MARK_STABLE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "mark-stable.yml"
extract_refs = run_path(str(REPO_ROOT / "scripts" / "check_workflow_script_refs.py"))["extract_refs"]


def _read_workflow() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _slice_between(text: str, start_marker: str, end_marker: str) -> str:
	start = text.find(start_marker)
	assert start != -1, f"Missing marker: {start_marker}"
	end = text.find(end_marker, start)
	assert end != -1, f"Missing marker after {start_marker}: {end_marker}"
	return text[start:end]


def _e2e_job(workflow: str) -> str:
	return _slice_between(workflow, "  e2e-smoke-test:\n", "  validate-scripts:\n")


def _phase6(workflow: str) -> str:
	return _slice_between(
		workflow,
		'# ── Phase 6: Review-blocked simulation test',
		'# ── Deep verification: inspect every step in every workflow run',
	)


def _phase4b_retry(workflow: str) -> str:
	return _slice_between(
		workflow,
		'# ── Retry: adopt active work or redispatch ${REVIEW_WORKFLOW_FILE}',
		'# ── Attempt 2',
	)


def _phase4b_helpers(workflow: str) -> str:
	return _slice_between(
		workflow,
		"# gh api wrapper with a small retry budget",
		"run_pytest() {",
	)


def _integer_contract_value(job: str, name: str) -> int:
	match = re.search(rf"^\s+{re.escape(name)}:\s+(\d+)\s*$", job, re.MULTILINE)
	assert match is not None, f"Missing integer contract value: {name}"
	return int(match.group(1))


def _workflow_dispatch_integer_default(workflow: str, input_name: str) -> int:
	match = re.search(
		rf"^      {re.escape(input_name)}:\s*$.*?^        default:\s+(\d+)\s*$",
		workflow,
		re.MULTILINE | re.DOTALL,
	)
	assert match is not None, f"Missing workflow_dispatch integer default: {input_name}"
	return int(match.group(1))


def test_reference_extraction_ignores_only_full_line_comments() -> None:
	workflow_text = """
      # scripts/yaml_comment_only.sh
      run: |
        # ${SUPPORT_SCRIPTS_DIR}/shell_comment_only.py
        # COMMENTED_SCRIPTS="commented_assignment.sh"
        # for f in ${COMMENTED_SCRIPTS}; do
        #   cp "scripts/${f}" /tmp/
        # done
        scripts/active_reference.sh # scripts/inline_comment_reference.sh
        printf '%s\\n' '# scripts/quoted_hash_reference.sh'
"""
	refs = extract_refs(workflow_text)
	assert "yaml_comment_only.sh" not in refs
	assert "shell_comment_only.py" not in refs
	assert "commented_assignment.sh" not in refs
	assert "active_reference.sh" in refs
	assert "inline_comment_reference.sh" in refs
	assert "quoted_hash_reference.sh" in refs


def test_release_workflows_use_canonical_reference_checker() -> None:
	checker_call = "PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_workflow_script_refs.py"
	raw_scanner = "grep -rhoE 'scripts/[a-zA-Z0-9_.-]+\\.(sh|py|json)'"
	for workflow_path in (WORKFLOW, MARK_STABLE_WORKFLOW):
		workflow = workflow_path.read_text(encoding="utf-8")
		check_step = _slice_between(
			workflow,
			"      - name: Script-workflow cross-reference\n",
			"\n      - name:",
		)
		assert checker_call in check_step
		assert raw_scanner not in check_step
		assert "OPTIONAL_SCRIPTS=" not in check_step


def test_default_serial_budget_leaves_required_headroom() -> None:
	workflow = _read_workflow()
	job = _e2e_job(workflow)
	timeout_match = re.search(r"^\s+timeout-minutes:\s+(\d+)\s*$", job, re.MULTILINE)
	assert timeout_match is not None
	job_timeout = int(timeout_match.group(1))
	assert job_timeout == _integer_contract_value(job, "E2E_JOB_TIMEOUT_MINUTES") == 300

	phase_timeout = _workflow_dispatch_integer_default(workflow, "phase_timeout")
	plan_timeout = _workflow_dispatch_integer_default(workflow, "plan_timeout")
	review_timeout = _workflow_dispatch_integer_default(workflow, "review_timeout")
	review_step_timeout = _workflow_dispatch_integer_default(workflow, "review_step_timeout")
	review_handoff_budget = _integer_contract_value(job, "REVIEW_PHASE_HANDOFF_BUDGET_MINUTES")
	finalization_reserve = _integer_contract_value(job, "E2E_FINALIZATION_RESERVE_MINUTES")
	review_effective_budget = max(review_timeout, min(review_step_timeout, 90) + review_handoff_budget)
	serial_budget = (
		(2 * phase_timeout)
		+ plan_timeout
		+ review_effective_budget
		+ _integer_contract_value(job, "EDITOR_RETRY_BUDGET_MINUTES")
		+ phase_timeout
		+ _integer_contract_value(job, "PHASE7_WAIT_BUDGET_MINUTES")
		+ finalization_reserve
	)
	assert review_handoff_budget == 15
	assert review_effective_budget == 90
	assert serial_budget == 295
	assert serial_budget <= job_timeout
	assert job_timeout - (serial_budget - finalization_reserve) >= finalization_reserve


def test_budget_guard_runs_before_artifact_creation() -> None:
	job = _e2e_job(_read_workflow())
	guard = 'if [ "${E2E_SERIAL_BUDGET_MINUTES}" -gt "${E2E_JOB_TIMEOUT_MINUTES}" ]; then'
	create_issue = "- name: Create E2E test issue"
	assert guard in job
	assert 'echo "::error::Configured E2E phase budgets require' in job
	assert "exit 1" in job[job.index(guard) : job.index(create_issue)]
	assert job.index(guard) < job.index(create_issue)
	assert "(2 * PHASE_TIMEOUT)" in job
	assert "REVIEW_EFFECTIVE_BUDGET_MINUTES" in job
	assert "REVIEW_EFFECTIVE_BUDGET_MINUTES=$((REVIEW_EFFECTIVE_BUDGET_MINUTES + REVIEW_PHASE_HANDOFF_BUDGET_MINUTES))" in job
	assert "REVIEW_PHASE_WALL_BUDGET_MINUTES=$((REVIEW_STEP_TIMEOUT + REVIEW_PHASE_HANDOFF_BUDGET_MINUTES))" in job
	assert 'REVIEW_PHASE_DEADLINE=$((REVIEW_PHASE_STARTED_AT + (REVIEW_PHASE_WALL_BUDGET_MINUTES * 60)))' in job
	assert 'if [ "$NOW" -ge "$REVIEW_PHASE_DEADLINE" ]; then' in job


def test_budget_guard_clamps_review_step_timeout_before_serial_math() -> None:
	workflow = _read_workflow()
	job = _e2e_job(workflow)
	prerequisites = _slice_between(job, "- name: Validate prerequisites", "# Fast-fail the hottest")
	step_budget = 'REVIEW_EFFECTIVE_BUDGET_MINUTES="${REVIEW_STEP_TIMEOUT}"'
	cap_guard = 'if [ "${REVIEW_EFFECTIVE_BUDGET_MINUTES}" -gt 90 ]; then'
	handoff_budget = "REVIEW_EFFECTIVE_BUDGET_MINUTES=$((REVIEW_EFFECTIVE_BUDGET_MINUTES + REVIEW_PHASE_HANDOFF_BUDGET_MINUTES))"
	review_max = 'if [ "${REVIEW_TIMEOUT}" -gt "${REVIEW_EFFECTIVE_BUDGET_MINUTES}" ]; then'
	assert prerequisites.index(step_budget) < prerequisites.index(cap_guard) < prerequisites.index(handoff_budget)
	assert prerequisites.index(handoff_budget) < prerequisites.index(review_max)
	assert 'REVIEW_EFFECTIVE_BUDGET_MINUTES=90' in prerequisites
	assert 'REVIEW_STEP_TIMEOUT_MAX=90' in job

	phase_timeout = _workflow_dispatch_integer_default(workflow, "phase_timeout")
	plan_timeout = _workflow_dispatch_integer_default(workflow, "plan_timeout")
	review_timeout = _workflow_dispatch_integer_default(workflow, "review_timeout")
	review_step_override = _workflow_dispatch_integer_default(workflow, "review_step_timeout") + 30
	review_handoff_budget = _integer_contract_value(job, "REVIEW_PHASE_HANDOFF_BUDGET_MINUTES")
	shared_budget = (
		(2 * phase_timeout)
		+ plan_timeout
		+ _integer_contract_value(job, "EDITOR_RETRY_BUDGET_MINUTES")
		+ phase_timeout
		+ _integer_contract_value(job, "PHASE7_WAIT_BUDGET_MINUTES")
		+ _integer_contract_value(job, "E2E_FINALIZATION_RESERVE_MINUTES")
	)
	job_timeout = _integer_contract_value(job, "E2E_JOB_TIMEOUT_MINUTES")
	assert shared_budget + max(review_timeout, review_step_override) > job_timeout
	assert shared_budget + max(review_timeout, min(review_step_override, 90) + review_handoff_budget) > job_timeout


def test_named_retry_and_phase7_budgets_replace_literal_deadlines() -> None:
	job = _e2e_job(_read_workflow())
	assert "DEADLINE=$(( $(date +%s) + (EDITOR_RETRY_BUDGET_MINUTES * 60) ))" in job
	assert job.count("(PHASE7_WAIT_BUDGET_MINUTES * 60)") == 2


def test_phase4b_adopts_the_oldest_eligible_active_review_run() -> None:
	retry = _phase4b_retry(_read_workflow())
	prior_validation = 'if ! [[ "${PRIOR_REVIEW_RUN}" =~ ^[0-9]+$ ]]'
	discovery = 'if ! RETRY_RUNS_JSON=$(gh_api_with_retry "${RETRY_RUNS_QUERY}"); then'
	dispatch = 'if ! gh workflow run "${REVIEW_WORKFLOW_FILE}"'
	assert retry.index(prior_validation) < retry.index(discovery) < retry.index(dispatch)
	assert "`Inject editor bait commit on PR branch` step" in retry
	assert "lines ~1101-1109" not in retry
	assert 'runs?branch=${BRANCH}&per_page=100' in retry
	assert '(.workflow_runs | type == "array")' in retry
	assert '(.id | type == "number") and .id > 0 and .id == (.id | floor)' in retry
	assert 'select(.id != $prior_run_id)' in retry
	assert 'select(.status != "completed")' in retry
	assert 'select(.head_sha == $bait_sha or .head_sha == $retry_sha)' in retry
	assert 'sort_by(.created_at, .id)' in retry
	assert 'if [ -n "${RETRY_ACTIVE_RUN}" ]; then' in retry
	assert 'Adopting active review run #${RETRY_RUN_ID} instead of dispatching duplicate work' in retry


def test_phase4b_dispatches_only_without_active_work_and_pins_one_run() -> None:
	retry = _phase4b_retry(_read_workflow())
	active_branch = _slice_between(
		retry,
		'if [ -n "${RETRY_ACTIVE_RUN}" ]; then',
		'          else\n',
	)
	dispatch_branch = _slice_between(
		retry,
		'          else\n',
		'          fi\n\n          # EDITOR_RETRY_BUDGET_MINUTES',
	)
	assert 'gh workflow run "${REVIEW_WORKFLOW_FILE}"' not in active_branch
	assert 'gh workflow run "${REVIEW_WORKFLOW_FILE}"' in dispatch_branch
	assert "RETRY_BASELINE_ID=" in retry
	assert 'select(.id > $baseline_id and .id != $prior_run_id)' in retry
	registration_selection = retry[retry.index('select(.id > $baseline_id and .id != $prior_run_id)') :]
	assert ".head_sha == $bait_sha" not in registration_selection
	assert ".head_sha == $retry_sha" not in registration_selection
	assert 'RETRY_REGISTRATION_DEADLINE=$(( $(date +%s) + 90 ))' in retry
	assert '"repos/${TEST_REPO}/actions/runs/${RETRY_RUN_ID}"' in retry
	assert 'select(.head_sha == "${RETRY_DISPATCH_SHA}")' not in retry
	assert 'actions/workflows/${REVIEW_WORKFLOW_FILE}/runs?event=workflow_dispatch' not in retry
	assert 'echo "status=retry_timeout" >> "$GITHUB_OUTPUT"' in retry
	assert 'echo "status=pr_closed_during_retry" >> "$GITHUB_OUTPUT"' in retry
	assert 'echo "status=pr_state_check_failed" >> "$GITHUB_OUTPUT"' in retry


_PHASE4B_START_EPOCH = 1_790_000_000
_PHASE4B_BUDGET_MINUTES = 25
_PHASE4B_DEADLINE = _PHASE4B_START_EPOCH + _PHASE4B_BUDGET_MINUTES * 60
_PHASE4B_PRIOR_RUN = 111
_PHASE4B_ADOPTED_RUN = 222
_PHASE4B_DISPATCHED_RUN = 333
_PHASE4B_BAIT_SHA = "b" * 40
_PHASE4B_REACHED = "PHASE4B_REACHED_ATTEMPT_2"

# A stand-in for `gh` that answers the Phase 4b reads from a scenario file.
# The reads named in `limited_endpoints` (`pulls`, `runs_list`, `run`) are
# rate-limited while the fake clock is before `limited_until`, the way a
# real limit lasts until its reset; after that PR-state reads take
# `pr_states` one entry per request, repeating the last entry. A `primary`
# limit spends the core quota and resets at `limited_until`, and a
# `secondary` limit leaves quota (GitHub's message for each, verbatim in
# shape). `run_statuses` is consumed one entry per pinned-run read. With
# `dispatch`, no run is active before the retry, so the step dispatches one
# (`gh workflow run`) and the run list shows a new run only after that.
# Every call is appended to calls.log.
_STUB_GH = r'''#!/usr/bin/env python3
import json
import os
import subprocess
import sys

scenario_path = os.environ["STUB_GH_SCENARIO"]
state_path = scenario_path + ".state"
with open(scenario_path, encoding="utf-8") as fh:
	scenario = json.load(fh)
try:
	with open(state_path, encoding="utf-8") as fh:
		state = json.load(fh)
except FileNotFoundError:
	state = {}
with open(os.environ["FAKE_CLOCK_FILE"], encoding="utf-8") as fh:
	now = int(fh.read().strip())

args = sys.argv[1:]
with open(os.path.join(os.path.dirname(scenario_path), "calls.log"), "a", encoding="utf-8") as fh:
	fh.write(" ".join(args) + "\n")
if args[:2] == ["workflow", "run"] and scenario["dispatch"]:
	state["dispatched"] = True
	with open(state_path, "w", encoding="utf-8") as fh:
		json.dump(state, fh)
	sys.exit(0)
if not args or args[0] != "api":
	sys.stderr.write("stub gh: unsupported command\n")
	sys.exit(2)
endpoint = args[1]
jq_expr = args[args.index("--jq") + 1] if "--jq" in args else None
limited_now = now < scenario["limited_until"]
primary = scenario["limit_kind"] == "primary"
poll_run = scenario["dispatched_run"] if scenario["dispatch"] else scenario["adopted_run"]


def take(key):
	values = scenario[key]
	index = state.get(key, 0)
	state[key] = index + 1
	with open(state_path, "w", encoding="utf-8") as fh:
		json.dump(state, fh)
	return values[min(index, len(values) - 1)]


def rate_limited(kind):
	if not limited_now or kind not in scenario["limited_endpoints"]:
		return
	if primary:
		sys.stderr.write("gh: API rate limit exceeded for user ID 11442166. If you reach out to GitHub Support for help, please include the request ID CC40:3C07B (HTTP 403)\n")
	else:
		sys.stderr.write("gh: You have exceeded a secondary rate limit. Please wait a few minutes before you try again. (HTTP 403)\n")
	sys.exit(1)


def emit(body):
	text = json.dumps(body)
	if jq_expr is not None:
		text = subprocess.run(["jq", "-r", jq_expr], input=text, capture_output=True, text=True, check=True).stdout.rstrip("\n")
	sys.stdout.write(text)
	sys.exit(0)


if endpoint == "rate_limit":
	if primary:
		core = {"remaining": 0 if limited_now else 5000, "reset": scenario["limited_until"] if limited_now else now + 3600}
	else:
		core = {"remaining": 4999, "reset": now + 3600}
	emit({"resources": {"core": core}})
if endpoint.endswith("/pulls/" + os.environ["PR_NUMBER"]):
	rate_limited("pulls")
	pr_state = take("pr_states")
	if pr_state == "error":
		sys.stderr.write("gh: Server Error (HTTP 502)\n")
		sys.exit(1)
	emit({"state": pr_state, "head": {"sha": os.environ["BAIT_SHA"]}})
if "/git/refs/heads/" in endpoint:
	emit({"object": {"sha": os.environ["BAIT_SHA"]}})
if "/actions/workflows/" in endpoint:
	prior = {"id": scenario["prior_run"], "head_sha": os.environ["BAIT_SHA"], "status": "completed", "created_at": "2026-10-01T01:30:00Z", "conclusion": "success"}
	if not scenario["dispatch"]:
		emit({"workflow_runs": [
			{"id": scenario["adopted_run"], "head_sha": os.environ["BAIT_SHA"], "status": "in_progress", "created_at": "2026-10-01T01:40:00Z", "conclusion": None},
			prior,
		]})
	if not state.get("dispatched"):
		emit({"workflow_runs": [prior]})
	rate_limited("runs_list")
	emit({"workflow_runs": [
		{"id": scenario["dispatched_run"], "head_sha": os.environ["BAIT_SHA"], "status": "queued", "created_at": "2026-10-01T01:50:00Z", "conclusion": None},
		prior,
	]})
if endpoint.endswith("/actions/runs/%d" % poll_run):
	rate_limited("run")
	run_status = take("run_statuses")
	emit({"id": poll_run, "status": run_status, "conclusion": "success" if run_status == "completed" else None})
sys.stderr.write("stub gh: unexpected endpoint " + endpoint + "\n")
sys.exit(2)
'''

# Fake clock: `date +%s` reads it and `sleep` advances it, so the real
# 25-minute deadline and rate-limit waits run instantly and deterministically.
_FAKE_CLOCK_PRELUDE = '''set -euo pipefail
date() {
	if [ "${1:-}" = "+%s" ]; then
		cat "${FAKE_CLOCK_FILE}"
	else
		command date "$@"
	fi
}
sleep() {
	echo "SLEEP ${1:-0}" >> "${SLEEP_LOG}"
	echo $(( $(cat "${FAKE_CLOCK_FILE}") + ${1:-0} )) > "${FAKE_CLOCK_FILE}"
}
'''


def _run_phase4b_retry(
	*,
	pr_states: list[str],
	run_statuses: list[str],
	limited_for: int = 0,
	limit_kind: str = "primary",
	limited_endpoints: tuple[str, ...] = ("pulls",),
	dispatch: bool = False,
) -> dict:
	"""Run the real Phase 4b helpers and retry block against a stub `gh`.

	The reads in `limited_endpoints` are rate-limited for the first
	`limited_for` seconds of fake time. Returns the exit code, transcript, `$GITHUB_OUTPUT` text, gh
	calls, sleeps, and the fake clock's final epoch.
	"""
	workflow = _read_workflow()
	script = (
		_FAKE_CLOCK_PRELUDE
		+ textwrap.dedent(_phase4b_helpers(workflow))
		+ "\n"
		+ textwrap.dedent(_phase4b_retry(workflow))
		+ f'\necho "{_PHASE4B_REACHED}"\n'
	)
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		stub = bin_dir / "gh"
		stub.write_text(_STUB_GH, encoding="utf-8")
		stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
		scenario = tmp_path / "scenario.json"
		scenario.write_text(json.dumps({
			"pr_states": pr_states,
			"run_statuses": run_statuses,
			"limited_until": _PHASE4B_START_EPOCH + limited_for,
			"limit_kind": limit_kind,
			"limited_endpoints": list(limited_endpoints),
			"dispatch": dispatch,
			"dispatched_run": _PHASE4B_DISPATCHED_RUN,
			"adopted_run": _PHASE4B_ADOPTED_RUN,
			"prior_run": _PHASE4B_PRIOR_RUN,
		}), encoding="utf-8")
		clock = tmp_path / "clock"
		clock.write_text(str(_PHASE4B_START_EPOCH), encoding="utf-8")
		output = tmp_path / "github_output"
		output.write_text("", encoding="utf-8")
		sleep_log = tmp_path / "sleeps.log"
		sleep_log.write_text("", encoding="utf-8")
		script_path = tmp_path / "phase4b.sh"
		script_path.write_text(script, encoding="utf-8")
		env = dict(os.environ)
		env.update({
			"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			"STUB_GH_SCENARIO": str(scenario),
			"FAKE_CLOCK_FILE": str(clock),
			"SLEEP_LOG": str(sleep_log),
			"GITHUB_OUTPUT": str(output),
			"TEST_REPO": "example/repo",
			"PR_NUMBER": "5823",
			"BRANCH": "ai/issue-5800",
			"BAIT_SHA": _PHASE4B_BAIT_SHA,
			"PRIOR_REVIEW_RUN": str(_PHASE4B_PRIOR_RUN),
			"REVIEW_WORKFLOW_FILE": "internal-review.yml",
			"EDITOR_RETRY_BUDGET_MINUTES": str(_PHASE4B_BUDGET_MINUTES),
		})
		proc = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, env=env, timeout=120)
		calls_log = tmp_path / "calls.log"
		return {
			"rc": proc.returncode,
			"transcript": proc.stdout + proc.stderr,
			"output": output.read_text(encoding="utf-8"),
			"calls": calls_log.read_text(encoding="utf-8").splitlines() if calls_log.exists() else [],
			"sleeps": [int(line.split()[1]) for line in sleep_log.read_text(encoding="utf-8").splitlines()],
			"clock": int(clock.read_text(encoding="utf-8").strip()),
		}


def _pr_state_calls(result: dict) -> list[str]:
	return [call for call in result["calls"] if call.startswith("api repos/example/repo/pulls/5823")]


def test_phase4b_consecutive_rate_limits_then_a_good_read_keep_verifying_the_adopted_run() -> None:
	"""Issue #5858: rate-limited PR-state reads must not end the step while the adopted run works.

	Five consecutive reads hit a secondary limit (GitHub's minimum wait is
	60s), then a read succeeds. The old loop counted four of them as an
	unresolvable PR and failed `pr_state_check_failed` (run 36797692597).
	"""
	result = _run_phase4b_retry(
		pr_states=["open"],
		run_statuses=["in_progress"] * 7 + ["completed"],
		limited_for=315,
		limit_kind="secondary",
	)
	assert result["rc"] == 0, result["transcript"]
	assert _PHASE4B_REACHED in result["transcript"]
	assert result["output"] == "", result["output"]
	assert "pr_state_check_failed" not in result["transcript"]
	assert f"Adopting active review run #{_PHASE4B_ADOPTED_RUN}" in result["transcript"]
	assert result["transcript"].count("not counted as an unresolvable state") == 5
	assert f"retry run #{_PHASE4B_ADOPTED_RUN}: status=completed conclusion=success" in result["transcript"]
	# One request per rate-limited read (5, at +15s ... +255s) plus one per
	# later poll (8, one per run read): the 2s/4s retries are skipped for a
	# rate limit, and each wait is followed by a PR-state read, not a run
	# read (PR #5874 review round 3).
	assert len(_pr_state_calls(result)) == 13
	assert result["sleeps"][:6] == [15] + [60] * 5, result["sleeps"]
	assert result["sleeps"].count(60) == 5
	assert 2 not in result["sleeps"] and 4 not in result["sleeps"]


def test_phase4b_primary_rate_limit_waits_for_the_reset_then_continues() -> None:
	# The incident's limit: core quota spent, resetting 200s into the wait.
	result = _run_phase4b_retry(
		pr_states=["open"],
		run_statuses=["in_progress"] * 3 + ["completed"],
		limited_for=200,
	)
	assert result["rc"] == 0, result["transcript"]
	assert _PHASE4B_REACHED in result["transcript"]
	assert result["output"] == "", result["output"]
	assert result["transcript"].count("not counted as an unresolvable state") == 1
	assert "api rate_limit" in "\n".join(result["calls"])
	# First read at +15s; the wait runs to the reset (+200s) plus 1s.
	assert 186 in result["sleeps"], result["sleeps"]


def test_phase4b_rate_limit_until_the_deadline_fails_closed_with_retry_timeout() -> None:
	"""The read a wait owes at the deadline is made once, and the loop ends.

	PR #5874 review round 3: the PR-state read at the deadline is limited
	again with nothing left to wait. A flag left set by the earlier wait
	would keep the loop running forever (the subprocess timeout catches it).
	"""
	result = _run_phase4b_retry(
		pr_states=["open"],
		run_statuses=["in_progress"],
		limited_for=3600,
	)
	assert result["rc"] == 1, result["transcript"]
	assert result["output"] == "status=retry_timeout\n", result["output"]
	assert _PHASE4B_REACHED not in result["transcript"]
	assert "pr_state_check_failed" not in result["transcript"]
	assert "GitHub reads were rate-limited" in result["transcript"]
	# The wait is capped at the deadline; only the loop's own 15s sleep can
	# step past it before the loop condition ends the wait.
	assert result["clock"] < _PHASE4B_DEADLINE + 15, result["clock"] - _PHASE4B_DEADLINE
	assert max(result["sleeps"]) <= _PHASE4B_DEADLINE - _PHASE4B_START_EPOCH
	# One wait to the deadline, then the owed PR-state read (limited, zero
	# wait) and one run read before the loop ends.
	assert result["sleeps"] == [15, _PHASE4B_DEADLINE - _PHASE4B_START_EPOCH - 15, 0], result["sleeps"]
	assert len(_calls_to(result, f"repos/example/repo/actions/runs/{_PHASE4B_ADOPTED_RUN}")) == 1


def test_phase4b_closed_pr_is_still_detected_after_rate_limits() -> None:
	result = _run_phase4b_retry(
		pr_states=["closed"],
		run_statuses=["in_progress"],
		limited_for=150,
		limit_kind="secondary",
	)
	assert result["rc"] == 1, result["transcript"]
	assert result["output"] == "status=pr_closed_during_retry\n", result["output"]
	# Limited reads at +15s, +75s and +135s, each followed at once by the
	# next PR-state read; the read at +195s sees the closed PR.
	assert result["sleeps"] == [15, 60, 60, 60], result["sleeps"]


def test_phase4b_pr_closed_during_a_rate_limit_wait_beats_a_completed_run() -> None:
	"""PR #5874 review round 3: a wait is followed by a PR-state read.

	The PR-state read is rate-limited, the PR closes during the wait, and the
	adopted run has completed by then. The old loop fell through to the run
	read after the wait, broke on `completed`, and went on to attempt 2
	without re-checking `pr_closed_during_retry`.
	"""
	result = _run_phase4b_retry(
		pr_states=["closed"],
		run_statuses=["completed"],
		limited_for=60,
		limit_kind="secondary",
	)
	assert result["rc"] == 1, result["transcript"]
	assert result["output"] == "status=pr_closed_during_retry\n", result["output"]
	assert _PHASE4B_REACHED not in result["transcript"]
	assert not _calls_to(result, f"repos/example/repo/actions/runs/{_PHASE4B_ADOPTED_RUN}"), result["calls"]
	assert result["sleeps"] == [15, 60], result["sleeps"]


def test_phase4b_sustained_pr_state_limit_still_reads_the_registration_list_at_the_window_end() -> None:
	"""A PR-state wait never costs the run-list read at the 90s window end.

	PR-state reads stay rate-limited past the registration window while the
	run list answers. The waits run to the window's end; the PR-state read
	there is limited with nothing left to wait, so the loop falls through to
	the run-list read that the earlier wait owed, instead of failing
	`retry_dispatch_failed` without reading the list.

	PR #5874 review round 4: the run it registers has completed, but that
	poll never confirmed the PR state, so the loop keeps polling until a
	PR-state read succeeds (+345s) before it goes on to attempt 2.
	"""
	result = _run_phase4b_retry(
		pr_states=["open"],
		run_statuses=["completed"],
		limited_for=300,
		limit_kind="secondary",
		dispatch=True,
	)
	assert result["rc"] == 0, result["transcript"]
	assert _PHASE4B_REACHED in result["transcript"]
	assert result["output"] == "", result["output"]
	assert f"Registered retry review run #{_PHASE4B_DISPATCHED_RUN}" in result["transcript"]
	assert result["transcript"].count("not counted as an unresolvable state") == 7
	assert result["transcript"].count("re-reading the PR state before attempt 2") == 1
	# Waits to the window's end (+90s), the zero wait there, the run-list
	# and run reads, then 60s waits (now capped at the 25-minute deadline)
	# until the PR-state read at +345s succeeds.
	assert result["sleeps"] == [15, 60, 15, 0, 15, 60, 60, 60, 60], result["sleeps"]
	assert result["clock"] == _PHASE4B_START_EPOCH + 345
	assert len(_calls_to(result, f"repos/example/repo/actions/runs/{_PHASE4B_DISPATCHED_RUN}")) == 2


def test_phase4b_completed_run_never_ends_the_wait_on_a_rate_limited_pr_state() -> None:
	"""PR #5874 review round 4: a zero wait at the deadline proves nothing about the PR.

	The PR-state read is rate-limited until past the 25-minute deadline (a
	primary limit, so one wait runs to the deadline), and the adopted run has
	completed. The PR-state read at the deadline is limited with nothing left
	to wait and falls through to the run read, which sees `completed`. The
	old loop broke there and went on to attempt 2 without confirming the PR
	was still open; it must fail closed instead.
	"""
	result = _run_phase4b_retry(
		pr_states=["open"],
		run_statuses=["completed"],
		limited_for=3600,
	)
	assert result["rc"] == 1, result["transcript"]
	assert result["output"] == "status=retry_timeout\n", result["output"]
	assert _PHASE4B_REACHED not in result["transcript"]
	assert "re-reading the PR state before attempt 2" in result["transcript"]
	assert f"Retry review run #{_PHASE4B_ADOPTED_RUN} completed, but PR #5823's state could not be read" in result["transcript"]
	assert "did not complete within" not in result["transcript"]
	assert result["sleeps"] == [15, _PHASE4B_DEADLINE - _PHASE4B_START_EPOCH - 15, 0], result["sleeps"]
	assert len(_calls_to(result, f"repos/example/repo/actions/runs/{_PHASE4B_ADOPTED_RUN}")) == 1


def test_phase4b_completed_registered_run_fails_closed_when_the_pr_state_stays_limited() -> None:
	"""The dispatch path keeps the same guard through to the 25-minute deadline."""
	result = _run_phase4b_retry(
		pr_states=["open"],
		run_statuses=["completed"],
		limited_for=3600,
		limit_kind="secondary",
		dispatch=True,
	)
	assert result["rc"] == 1, result["transcript"]
	assert result["output"] == "status=retry_timeout\n", result["output"]
	assert _PHASE4B_REACHED not in result["transcript"]
	assert f"Registered retry review run #{_PHASE4B_DISPATCHED_RUN}" in result["transcript"]
	assert f"Retry review run #{_PHASE4B_DISPATCHED_RUN} completed, but PR #5823's state could not be read" in result["transcript"]
	assert result["clock"] == _PHASE4B_DEADLINE


def test_phase4b_plain_pr_state_failures_still_trip_the_breaker() -> None:
	result = _run_phase4b_retry(pr_states=["error"], run_statuses=["in_progress"])
	assert result["rc"] == 1, result["transcript"]
	assert result["output"] == "status=pr_state_check_failed\n", result["output"]
	assert "not counted as an unresolvable state" not in result["transcript"]
	# Ordinary failures keep the 3-attempt budget: 4 reads x 3 attempts.
	assert len(_pr_state_calls(result)) == 12


def _calls_to(result: dict, endpoint: str) -> list[str]:
	return [call for call in result["calls"] if call.split()[1:2] == [endpoint]]


def test_phase4b_rate_limited_adopted_run_reads_wait_instead_of_rereading_every_15s() -> None:
	"""A rate limit on the pinned run's status read waits like a PR-state one.

	PR #5874 review: a run-status read that kept re-reading every 15s under a
	secondary limit would poll a limited endpoint through the whole deadline.
	"""
	result = _run_phase4b_retry(
		pr_states=["open"],
		run_statuses=["in_progress", "completed"],
		limited_for=375,
		limit_kind="secondary",
		limited_endpoints=("run",),
	)
	assert result["rc"] == 0, result["transcript"]
	assert _PHASE4B_REACHED in result["transcript"]
	assert result["output"] == "", result["output"]
	assert result["transcript"].count(f"Retry review run #{_PHASE4B_ADOPTED_RUN} read was rate-limited") == 6
	assert "not counted as an unresolvable state" not in result["transcript"]
	assert "60s fallback: core.remaining=4999, so a secondary limit" in result["transcript"]
	# Each read follows the previous 60s wait at once, with no 15s poll
	# sleep in between (PR #5874 review round 2): limited reads at +15s,
	# +75s, ... +315s, a good read at +375s, then the normal 15s poll.
	assert result["sleeps"] == [15] + [60] * 6 + [15], result["sleeps"]
	assert len(_calls_to(result, f"repos/example/repo/actions/runs/{_PHASE4B_ADOPTED_RUN}")) == 8


def test_phase4b_rate_limited_registration_list_waits_for_the_reset_then_registers() -> None:
	result = _run_phase4b_retry(
		pr_states=["open"],
		run_statuses=["completed"],
		limited_for=30,
		limited_endpoints=("runs_list",),
		dispatch=True,
	)
	assert result["rc"] == 0, result["transcript"]
	assert _PHASE4B_REACHED in result["transcript"]
	assert "Retry dispatch sent" in result["transcript"]
	assert result["transcript"].count("Post-dispatch review run list read was rate-limited") == 1
	assert f"Registered retry review run #{_PHASE4B_DISPATCHED_RUN}" in result["transcript"]
	# First list read at +15s; the core quota resets at +30s, so wait 16s.
	assert 16 in result["sleeps"], result["sleeps"]
	assert "core quota spent, resets at epoch" in result["transcript"]


def test_phase4b_sustained_rate_limit_on_registration_list_fails_closed_in_the_window() -> None:
	# The wait never extends the 90s registration window (AD-6).
	result = _run_phase4b_retry(
		pr_states=["open"],
		run_statuses=["in_progress"],
		limited_for=3600,
		limit_kind="secondary",
		limited_endpoints=("runs_list",),
		dispatch=True,
	)
	assert result["rc"] == 1, result["transcript"]
	assert result["output"] == "status=retry_dispatch_failed\n", result["output"]
	assert "did not register a matching review run within 90 seconds" in result["transcript"]
	# Waits stop at the window's end (+90s), where one more list read is
	# made; only the ordinary 15s poll sleep follows before the step fails.
	assert result["sleeps"] == [15, 60, 15, 0, 15], result["sleeps"]
	assert result["clock"] <= _PHASE4B_START_EPOCH + 90 + 15, result["clock"] - _PHASE4B_START_EPOCH
	# The baseline read before the dispatch, then reads at +15s, +75s, +90s.
	assert len([call for call in result["calls"] if "/actions/workflows/" in call]) == 4


def test_phase4b_registration_list_limit_clearing_at_the_window_end_still_registers() -> None:
	"""PR #5874 review round 2: a wait that ends at the deadline gets its read.

	The secondary limit clears exactly when the 90s registration window
	ends. The old loop slept another 15s after the capped wait and failed
	`retry_dispatch_failed` without re-reading the run list.
	"""
	result = _run_phase4b_retry(
		pr_states=["open"],
		run_statuses=["completed"],
		limited_for=90,
		limit_kind="secondary",
		limited_endpoints=("runs_list",),
		dispatch=True,
	)
	assert result["rc"] == 0, result["transcript"]
	assert _PHASE4B_REACHED in result["transcript"]
	assert result["output"] == "", result["output"]
	assert f"Registered retry review run #{_PHASE4B_DISPATCHED_RUN}" in result["transcript"]
	assert result["sleeps"] == [15, 60, 15], result["sleeps"]
	assert result["clock"] == _PHASE4B_START_EPOCH + 90


def test_phase4b_run_status_limit_clearing_at_the_deadline_still_reads_the_run() -> None:
	"""PR #5874 review round 2: the pinned run gets one read at the deadline.

	The core quota resets exactly at the 25-minute deadline, so the wait is
	capped there. The old loop condition ended the wait without reading the
	run and failed `retry_timeout` although the run had completed.
	"""
	result = _run_phase4b_retry(
		pr_states=["open"],
		run_statuses=["completed"],
		limited_for=_PHASE4B_DEADLINE - _PHASE4B_START_EPOCH,
		limited_endpoints=("run",),
	)
	assert result["rc"] == 0, result["transcript"]
	assert _PHASE4B_REACHED in result["transcript"]
	assert result["output"] == "", result["output"]
	assert f"retry run #{_PHASE4B_ADOPTED_RUN}: status=completed conclusion=success" in result["transcript"]
	assert result["clock"] == _PHASE4B_DEADLINE
	assert result["sleeps"] == [15, _PHASE4B_DEADLINE - _PHASE4B_START_EPOCH - 15], result["sleeps"]


def test_gh_api_with_retry_rate_limit_warning_keeps_earlier_attempts_stderr() -> None:
	"""A 5xx before the rate limit stays in the warning (PR #5874 review)."""
	stub = textwrap.dedent('''\
		#!/usr/bin/env bash
		count=$(( $(cat "${STUB_COUNT_FILE}") + 1 ))
		echo "${count}" > "${STUB_COUNT_FILE}"
		if [ "${count}" -eq 1 ]; then
			echo "gh: Server Error (HTTP 502)" >&2
		else
			echo "gh: API rate limit exceeded for user ID 1 (HTTP 403)" >&2
		fi
		exit 1
	''')
	script = (
		_FAKE_CLOCK_PRELUDE
		+ textwrap.dedent(_phase4b_helpers(_read_workflow()))
		+ '\nrc=0\ngh_api_with_retry "repos/example/repo/pulls/1" || rc=$?\necho "RC=${rc}"\n'
	)
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		gh = bin_dir / "gh"
		gh.write_text(stub, encoding="utf-8")
		gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
		count = tmp_path / "count"
		count.write_text("0", encoding="utf-8")
		clock = tmp_path / "clock"
		clock.write_text(str(_PHASE4B_START_EPOCH), encoding="utf-8")
		script_path = tmp_path / "helpers.sh"
		script_path.write_text(script, encoding="utf-8")
		env = dict(os.environ)
		env.update({
			"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			"STUB_COUNT_FILE": str(count),
			"FAKE_CLOCK_FILE": str(clock),
			"SLEEP_LOG": str(tmp_path / "sleeps.log"),
		})
		proc = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, env=env, timeout=60)
		assert proc.returncode == 0, proc.stdout + proc.stderr
		assert "RC=75" in proc.stdout
		assert count.read_text(encoding="utf-8").strip() == "2"
		warning = next(line for line in proc.stderr.splitlines() if "rate-limited on attempt 2" in line)
		assert "Server Error (HTTP 502)" in warning
		assert "API rate limit exceeded" in warning


def test_phase4b_rate_limit_branch_contract() -> None:
	workflow = _read_workflow()
	helpers = _phase4b_helpers(workflow)
	retry = _phase4b_retry(workflow)
	assert "GH_API_RATE_LIMITED_RC=75" in helpers
	assert 'grep -qi "rate limit"' in helpers
	assert 'return "${GH_API_RATE_LIMITED_RC}"' in helpers
	assert 'echo "rate_limited"' in helpers
	assert "gh api rate_limit --jq" in helpers
	wait_helper = helpers[helpers.index("phase4b_wait_out_rate_limit() {"):]
	assert 'local wait_deadline="${DEADLINE}"' in wait_helper
	assert 'wait_deadline="${RETRY_REGISTRATION_DEADLINE}"' in wait_helper
	assert 'phase4b_rate_limit_wait_seconds "${wait_deadline}"' in wait_helper
	assert 'sleep "${wait_seconds}"' in wait_helper
	rate_limited_branch = _slice_between(
		retry,
		'if [ "${PR_STATE}" = "rate_limited" ]; then',
		'elif [ "${PR_STATE}" = "unknown" ]; then',
	)
	assert "PR_STATE_FAILURES" not in rate_limited_branch
	assert "phase4b_wait_out_rate_limit" in rate_limited_branch
	# PR #5874 review round 3: the flag is cleared before the wait, a wait
	# that slept goes back to the PR-state read (`continue`), and a zero
	# wait restores the read an earlier wait owed before falling through.
	assert re.search(
		r'PR_STATE_RATE_LIMIT_READ_OWED="\$\{RETRY_READ_AFTER_RATE_LIMIT_WAIT\}"\s+'
		r"RETRY_READ_AFTER_RATE_LIMIT_WAIT=0\s+phase4b_wait_out_rate_limit ",
		rate_limited_branch,
	)
	slept = rate_limited_branch[rate_limited_branch.index('if [ "${RETRY_READ_AFTER_RATE_LIMIT_WAIT}" -eq 1 ]; then'):]
	assert re.match(r'if \[ "\$\{RETRY_READ_AFTER_RATE_LIMIT_WAIT\}" -eq 1 \]; then\s+(?:#[^\n]*\s+)*continue\s+fi\b', slept)
	assert 'RETRY_READ_AFTER_RATE_LIMIT_WAIT="${PR_STATE_RATE_LIMIT_READ_OWED}"' in slept
	# The run-list and run-status reads wait out a rate limit too, before
	# their generic-failure `continue`.
	for read_call in ('gh_api_with_retry "${RETRY_RUNS_QUERY}") || RETRY_READ_RC=$?', 'gh_api_with_retry "repos/${TEST_REPO}/actions/runs/${RETRY_RUN_ID}") || RETRY_READ_RC=$?'):
		after = retry[retry.index(read_call):]
		assert after.index('-eq "${GH_API_RATE_LIMITED_RC}"') < after.index("continue")
		assert after.index("phase4b_wait_out_rate_limit") < after.index("continue")
		# The read a wait owes is consumed by the next read, never left set.
		before = retry[:retry.index(read_call)]
		assert re.search(r"RETRY_READ_AFTER_RATE_LIMIT_WAIT=0\s+RETRY_READ_RC=0\s+\w+=\$\($", before)
	# Only a wait that slept owes a read, so a zero wait at a deadline ends the loop.
	assert re.search(r'if \[ "\$\{wait_seconds\}" -gt 0 \]; then\s+RETRY_READ_AFTER_RATE_LIMIT_WAIT=1\s+fi', wait_helper)
	assert 'while [ "$(date +%s)" -lt "${DEADLINE}" ] || [ "${RETRY_READ_AFTER_RATE_LIMIT_WAIT}" -eq 1 ]; do' in retry
	assert '-ge "${RETRY_REGISTRATION_DEADLINE}" ] && [ "${RETRY_READ_AFTER_RATE_LIMIT_WAIT}" -ne 1 ]' in retry
	assert retry.index('if [ "${PR_STATE}" = "closed" ]; then') < retry.index(
		'if [ "${PR_STATE}" = "rate_limited" ]; then'
	)
	# PR #5874 review round 4: a completed run ends the loop only after a
	# PR-state read in that poll that was not rate-limited, and the post-loop
	# check fails closed unless the loop accepted the completion.
	completed = _slice_between(retry, 'if [ "${RETRY_STATUS}" = "completed" ]; then', "done\n")
	assert re.search(
		r'if \[ "\$\{PR_STATE\}" = "rate_limited" \]; then\s+echo "[^"\n]*"\s+continue\s+fi\s+'
		r"RETRY_RUN_COMPLETION_ACCEPTED=1\s+break\s+fi\s*$",
		completed,
	), completed
	assert retry.count("RETRY_RUN_COMPLETION_ACCEPTED=1") == 1
	assert retry.index("RETRY_RUN_COMPLETION_ACCEPTED=0") < retry.index("while [")
	assert '|| [ "${RETRY_RUN_COMPLETION_ACCEPTED}" -ne 1 ]; then' in retry


def test_phase6_registers_once_and_polls_only_the_pinned_run() -> None:
	phase6 = _phase6(_read_workflow())
	branch_query = "runs?event=workflow_dispatch&branch=${POLLER_DISPATCH_REF}&per_page=10"
	dispatch = 'gh workflow run "${workflow_file}" --repo "${TEST_REPO}" --ref "${POLLER_DISPATCH_REF}"'
	assert branch_query in phase6
	assert dispatch in phase6
	assert phase6.index(branch_query) < phase6.index(dispatch)
	assert phase6.count('echo "poller_run_id=${POLLER_RUN_ID}" >> "$GITHUB_OUTPUT"') == 1
	assert '"repos/${TEST_REPO}/actions/runs/${POLLER_RUN_ID}"' in phase6
	assert 'actions/workflows/${workflow_file}/dispatches' not in phase6
	assert "workflow_run_id" not in phase6
	assert "actions/runs?per_page=5&event=workflow_dispatch" not in phase6
	assert "POLLER_LATEST_ID" not in phase6


def test_phase6_rejects_a_newer_foreign_ref_dispatch() -> None:
	phase6 = _phase6(_read_workflow())
	assert '--arg dispatch_ref "${POLLER_DISPATCH_REF}"' in phase6
	assert '.head_branch == $dispatch_ref' in phase6
	assert '.id > $baseline_id' in phase6
	assert 'fromdateiso8601? // 0) >= ($dispatch_started_epoch - 5)' in phase6
	assert 'if [ "${candidate_count}" -gt 1 ]; then' in phase6
	assert 'Ambiguous ${workflow_file} registration' in phase6


def test_phase6_transient_api_errors_remain_bounded() -> None:
	phase6 = _phase6(_read_workflow())
	assert phase6.count("retrying within the inactivity window") == 3
	assert 'echo "status=timeout" >> "$GITHUB_OUTPUT"' in phase6
	labels_read = 'if ! LABELS=$(gh_api_safe_quiet_print'
	assert labels_read in phase6
	labels_block = phase6[phase6.index(labels_read) : phase6.index("# If rate-limited")]
	assert '|| echo ""' not in labels_block
	assert 'sleep "$POLL_INTERVAL"' in labels_block
	assert "continue" in labels_block
	assert 'issues/${TRACKING_NUMBER}' in labels_block
	assert "exit 0" in labels_block
	assert "exit 1" not in labels_block
	transient_read = 'if ! POLLER_RUN=$(gh_api_safe_quiet_print'
	label_progress = 'if [ "$LABELS" != "$PREV_LABELS" ]; then'
	success_guard = 'if [ "$REVIEW_BLOCKED_PRESENT" -eq 0 ] || [ "$TERMINAL_LABEL_PRESENT" -eq 1 ]; then'
	assert phase6.index(label_progress) < phase6.index(transient_read)
	assert phase6.index(success_guard) < phase6.index(transient_read)
	read_complete = 'POLLER_STATUS=$(printf'
	read_block = phase6[phase6.index(transient_read) : phase6.index(read_complete)]
	assert 'echo "status=poller_failed"' not in read_block
	assert read_block.count('issues/${TRACKING_NUMBER}') == 2
	assert read_block.count("exit 0") == 2
	assert "exit 1" not in read_block


def test_reviewer_majority_is_progress_only() -> None:
	job = _e2e_job(_read_workflow())
	progress_block = _slice_between(
		job,
		'if [ "$SUCCEEDED" -ge 3 ] && [ $((SUCCEEDED * 2)) -gt "$TOTAL_DONE" ]; then',
		'elif [ "$TOTAL_DONE" -gt 0 ]; then',
	)
	assert "waiting for authoritative review completion and editor verification" in progress_block
	assert "status=success" not in progress_block
	assert "review_run_id=" not in progress_block
	assert "exit 0" not in progress_block
	assert 'if [ "$RUN_STATUS" = "completed" ]; then' in job
	assert 'if [ "$RUN_CONCLUSION" = "success" ]; then' in job
	assert 'echo "status=success" >> "$GITHUB_OUTPUT"' in job
	assert 'if [ "${PR_HEAD}" = "${BAIT_SHA}" ]; then' in job


def test_success_transitions_and_unconditional_cleanup_are_preserved() -> None:
	workflow = _read_workflow()
	phase6 = _phase6(workflow)
	assert 'if [ "$REVIEW_BLOCKED_PRESENT" -eq 0 ] || [ "$TERMINAL_LABEL_PRESENT" -eq 1 ]; then' in phase6
	assert "ai:merged|ai:ready-to-merge" in phase6
	cleanup = _slice_between(workflow, "- name: Cleanup test artifacts", "# Temporary: reveal which git invocation")
	assert "if: always()" in cleanup
	assert 'issues/${TRACKING_NUMBER}' in cleanup


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
