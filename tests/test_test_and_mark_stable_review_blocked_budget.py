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
_PHASE4B_BAIT_SHA = "b" * 40
_PHASE4B_REACHED = "PHASE4B_REACHED_ATTEMPT_2"

# A stand-in for `gh` that answers the Phase 4b reads from a scenario file.
# PR-state reads are rate-limited while the fake clock is before
# `limited_until`, the way a real limit lasts until its reset; after that
# they take `pr_states` one entry per request, repeating the last entry.
# A `primary` limit spends the core quota and resets at `limited_until`, and
# a `secondary` limit leaves quota (GitHub's message for each, verbatim in
# shape). `run_statuses` is consumed one entry per adopted-run read. Every
# call is appended to calls.log.
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
if not args or args[0] != "api":
	sys.stderr.write("stub gh: unsupported command\n")
	sys.exit(2)
endpoint = args[1]
jq_expr = args[args.index("--jq") + 1] if "--jq" in args else None
limited = now < scenario["limited_until"]
primary = scenario["limit_kind"] == "primary"


def take(key):
	values = scenario[key]
	index = state.get(key, 0)
	state[key] = index + 1
	with open(state_path, "w", encoding="utf-8") as fh:
		json.dump(state, fh)
	return values[min(index, len(values) - 1)]


def emit(body):
	text = json.dumps(body)
	if jq_expr is not None:
		text = subprocess.run(["jq", "-r", jq_expr], input=text, capture_output=True, text=True, check=True).stdout.rstrip("\n")
	sys.stdout.write(text)
	sys.exit(0)


if endpoint == "rate_limit":
	if primary:
		core = {"remaining": 0 if limited else 5000, "reset": scenario["limited_until"] if limited else now + 3600}
	else:
		core = {"remaining": 4999, "reset": now + 3600}
	emit({"resources": {"core": core}})
if endpoint.endswith("/pulls/" + os.environ["PR_NUMBER"]):
	if limited:
		if primary:
			sys.stderr.write("gh: API rate limit exceeded for user ID 11442166. If you reach out to GitHub Support for help, please include the request ID CC40:3C07B (HTTP 403)\n")
		else:
			sys.stderr.write("gh: You have exceeded a secondary rate limit. Please wait a few minutes before you try again. (HTTP 403)\n")
		sys.exit(1)
	pr_state = take("pr_states")
	if pr_state == "error":
		sys.stderr.write("gh: Server Error (HTTP 502)\n")
		sys.exit(1)
	emit({"state": pr_state, "head": {"sha": os.environ["BAIT_SHA"]}})
if "/git/refs/heads/" in endpoint:
	emit({"object": {"sha": os.environ["BAIT_SHA"]}})
if "/actions/workflows/" in endpoint:
	emit({"workflow_runs": [
		{"id": scenario["adopted_run"], "head_sha": os.environ["BAIT_SHA"], "status": "in_progress", "created_at": "2026-10-01T01:40:00Z", "conclusion": None},
		{"id": scenario["prior_run"], "head_sha": os.environ["BAIT_SHA"], "status": "completed", "created_at": "2026-10-01T01:30:00Z", "conclusion": "success"},
	]})
if endpoint.endswith("/actions/runs/%d" % scenario["adopted_run"]):
	run_status = take("run_statuses")
	emit({"id": scenario["adopted_run"], "status": run_status, "conclusion": "success" if run_status == "completed" else None})
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
) -> dict:
	"""Run the real Phase 4b helpers and retry block against a stub `gh`.

	PR-state reads are rate-limited for the first `limited_for` seconds of
	fake time. Returns the exit code, transcript, `$GITHUB_OUTPUT` text, gh
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
		limited_for=375,
		limit_kind="secondary",
	)
	assert result["rc"] == 0, result["transcript"]
	assert _PHASE4B_REACHED in result["transcript"]
	assert result["output"] == "", result["output"]
	assert "pr_state_check_failed" not in result["transcript"]
	assert f"Adopting active review run #{_PHASE4B_ADOPTED_RUN}" in result["transcript"]
	assert result["transcript"].count("not counted as an unresolvable state") == 5
	assert f"retry run #{_PHASE4B_ADOPTED_RUN}: status=completed conclusion=success" in result["transcript"]
	# One request per rate-limited read (5) plus one per later read (3):
	# the 2s/4s retries are skipped for a rate limit.
	assert len(_pr_state_calls(result)) == 8
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
	result = _run_phase4b_retry(
		pr_states=["open"],
		run_statuses=["in_progress"],
		limited_for=3600,
	)
	assert result["rc"] == 1, result["transcript"]
	assert result["output"] == "status=retry_timeout\n", result["output"]
	assert _PHASE4B_REACHED not in result["transcript"]
	assert "pr_state_check_failed" not in result["transcript"]
	assert "PR-state reads were rate-limited" in result["transcript"]
	# The wait is capped at the deadline; only the loop's own 15s sleep can
	# step past it before the loop condition ends the wait.
	assert result["clock"] < _PHASE4B_DEADLINE + 15, result["clock"] - _PHASE4B_DEADLINE
	assert max(result["sleeps"]) <= _PHASE4B_DEADLINE - _PHASE4B_START_EPOCH


def test_phase4b_closed_pr_is_still_detected_after_rate_limits() -> None:
	result = _run_phase4b_retry(
		pr_states=["closed"],
		run_statuses=["in_progress"],
		limited_for=150,
		limit_kind="secondary",
	)
	assert result["rc"] == 1, result["transcript"]
	assert result["output"] == "status=pr_closed_during_retry\n", result["output"]
	assert result["sleeps"].count(60) == 2


def test_phase4b_plain_pr_state_failures_still_trip_the_breaker() -> None:
	result = _run_phase4b_retry(pr_states=["error"], run_statuses=["in_progress"])
	assert result["rc"] == 1, result["transcript"]
	assert result["output"] == "status=pr_state_check_failed\n", result["output"]
	assert "not counted as an unresolvable state" not in result["transcript"]
	# Ordinary failures keep the 3-attempt budget: 4 reads x 3 attempts.
	assert len(_pr_state_calls(result)) == 12


def test_phase4b_rate_limit_branch_contract() -> None:
	workflow = _read_workflow()
	helpers = _phase4b_helpers(workflow)
	retry = _phase4b_retry(workflow)
	assert "GH_API_RATE_LIMITED_RC=75" in helpers
	assert 'grep -qi "rate limit"' in helpers
	assert 'return "${GH_API_RATE_LIMITED_RC}"' in helpers
	assert 'echo "rate_limited"' in helpers
	assert "gh api rate_limit --jq" in helpers
	rate_limited_branch = _slice_between(
		retry,
		'if [ "${PR_STATE}" = "rate_limited" ]; then',
		'elif [ "${PR_STATE}" = "unknown" ]; then',
	)
	assert "PR_STATE_FAILURES" not in rate_limited_branch
	assert 'RATE_LIMIT_WAIT_DEADLINE="${DEADLINE}"' in rate_limited_branch
	assert 'RATE_LIMIT_WAIT_DEADLINE="${RETRY_REGISTRATION_DEADLINE}"' in rate_limited_branch
	assert 'sleep "${RATE_LIMIT_WAIT}"' in rate_limited_branch
	assert retry.index('if [ "${PR_STATE}" = "closed" ]; then') < retry.index(
		'if [ "${PR_STATE}" = "rate_limited" ]; then'
	)


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
