#!/usr/bin/env python3
"""The dispatch watcher cancels a child run whose completion watch timed out (#9706).

Follow-up to #9532: in release run 43590943288 dispatched children stayed
queued for hours with no runner after their watch gave up. The workflow steps
in test-and-mark-stable.yml already cancel such a child; this covers the same
behaviour in scripts/dispatch_and_watch_workflow_run.sh itself. On the
completion-timeout path the watcher prints a diagnosis (queued the whole time
versus in_progress), cancels the run unless DISPATCH_WATCH_CANCEL_ON_TIMEOUT is
false, and still ends with the unchanged `::error::... timed out` line and
exit 1.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPERS = ("dispatch_and_watch_workflow_run.sh", "comprehensive_test_and_release_gh_api.sh")

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the watcher")

CANCEL_CALL = "api -X POST repos/owner/repo/actions/runs/4242/cancel"
TIMEOUT_LINE = "::error::validate run #4242 timed out"
# Same extraction the workflow steps' timeout block uses.
WORKFLOW_SED_RE = re.compile(r"^::error::.* run #([0-9][0-9]*) timed out$")
QUEUED_DIAG = (
	"::warning::validate run #4242 watch diagnosis: status=queued the whole time "
	"(no runner was assigned); status_reads="
)
IN_PROGRESS_DIAG = (
	"::warning::validate run #4242 watch diagnosis: status=in_progress "
	"(a runner was assigned; the run was slow); status_reads="
)
UNKNOWN_DIAG = (
	"::warning::validate run #4242 watch diagnosis: last status=unknown "
	"(could not tell queued from in_progress); status_reads=0"
)

GH_STUB = """#!/usr/bin/env bash
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
	*"/actions/runs/4242/jobs"*)
		[ -n "${STUB_JOBS_BODY:-}" ] || exit 1
		body="${STUB_JOBS_BODY}" ;;
	*"/actions/runs/9999/jobs"*)
		[ -n "${STUB_GATE_JOBS_BODY:-}" ] || exit 1
		body="${STUB_GATE_JOBS_BODY}" ;;
	*"/actions/runs/9999/cancel")
		[ "${STUB_GATE_CANCEL_FAIL:-0}" = "1" ] && exit 1
		body='{}' ;;
	*"/runs?event=workflow_dispatch&per_page=1") body='{"workflow_runs":[]}' ;;
	*"/runs?event=workflow_dispatch&"*) body='{"workflow_runs":[{"id":4242,"created_at":"2026-11-15T01:26:16Z"}]}' ;;
	*"/actions/runs/4242/cancel")
		if [ "${STUB_CANCEL_FAIL:-0}" = "1" ]; then
			echo "HTTP 409: Cannot cancel a workflow run that is completed." >&2
			exit 1
		fi
		body='{}' ;;
	*"/actions/runs/4242")
		if [ "${STUB_STATUS_FAIL:-0}" = "1" ]; then
			echo "HTTP 404: Not Found" >&2
			exit 1
		fi
		if [ -n "${STUB_RUN_CONCLUSION:-}" ]; then
			body="{\\"status\\":\\"${STUB_RUN_STATUS}\\",\\"conclusion\\":\\"${STUB_RUN_CONCLUSION}\\"}"
		else
			body="{\\"status\\":\\"${STUB_RUN_STATUS}\\",\\"conclusion\\":null}"
		fi
		;;
	*) exit 1 ;;
esac
if [ -n "${jq_expr}" ]; then
	printf '%s' "${body}" | jq -r "${jq_expr}"
else
	printf '%s\\n' "${body}"
fi
"""


def _run_watcher(
	*,
	run_status: str = "queued",
	run_conclusion: str = "",
	cancel_switch: str | None = None,
	status_fail: bool = False,
	cancel_fail: bool = False,
	extra_env: dict[str, str] | None = None,
	args: list[str] | None = None,
) -> tuple[int, str, list[str], str]:
	bash = shutil.which("bash")
	assert bash is not None
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		(tmp / "scripts").mkdir()
		for helper in HELPERS:
			shutil.copyfile(REPO_ROOT / "scripts" / helper, tmp / "scripts" / helper)
		bindir = tmp / "bin"
		bindir.mkdir()
		gh_log = tmp / "gh.log"
		gh_log.write_text("", encoding="utf-8")
		gh = bindir / "gh"
		gh.write_text(GH_STUB, encoding="utf-8")
		gh.chmod(0o755)
		output = tmp / "github_output"
		output.write_text("", encoding="utf-8")
		env = {
			"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
			"HOME": str(tmp),
			"GITHUB_OUTPUT": str(output),
			"STUB_GH_LOG": str(gh_log),
			"STUB_RUN_STATUS": run_status,
			"STUB_RUN_CONCLUSION": run_conclusion,
			"STUB_STATUS_FAIL": "1" if status_fail else "0",
			"STUB_CANCEL_FAIL": "1" if cancel_fail else "0",
		}
		if cancel_switch is not None:
			env["DISPATCH_WATCH_CANCEL_ON_TIMEOUT"] = cancel_switch
		env.update(extra_env or {})
		proc = subprocess.run(
			[
				bash,
				"scripts/dispatch_and_watch_workflow_run.sh",
				*(args if args is not None else [
					"--repo", "owner/repo",
					"--workflow", "internal-validate.yml",
					"--display-name", "validate",
					"--registration-timeout-secs", "30",
					"--registration-poll-interval-secs", "1",
					"--completion-timeout-secs", "2",
					"--completion-poll-interval-secs", "1",
				]),
			],
			cwd=tmp,
			env=env,
			capture_output=True,
			text=True,
			check=False,
			timeout=120,
		)
		calls = [line for line in gh_log.read_text(encoding="utf-8").splitlines() if line]
		github_output = output.read_text(encoding="utf-8")
	return proc.returncode, proc.stdout + proc.stderr, calls, github_output


def _cancels(calls: list[str]) -> list[str]:
	return [c for c in calls if "/cancel" in c]


def test_queued_whole_time_reports_diagnosis_cancels_and_fails() -> None:
	rc, out, calls, github_output = _run_watcher()
	assert rc == 1, out
	assert QUEUED_DIAG in out, out
	assert _cancels(calls) == [CANCEL_CALL], calls
	assert "Cancel requested for watched run #4242 (completion watch timeout)" in out, out
	lines = out.splitlines()
	assert lines[-1] == TIMEOUT_LINE, out
	diag_at = next(i for i, line in enumerate(lines) if line.startswith(QUEUED_DIAG))
	cancel_at = lines.index("Cancel requested for watched run #4242 (completion watch timeout)")
	assert diag_at < cancel_at < lines.index(TIMEOUT_LINE), out
	matches = [m.group(1) for m in (WORKFLOW_SED_RE.match(line) for line in lines) if m]
	assert matches and matches[-1] == "4242", out
	assert "dispatched_run_id=4242" in github_output.splitlines(), github_output
	assert not [line for line in github_output.splitlines() if line.startswith("run_id=")], github_output


def test_in_progress_reports_slow_run_and_cancels() -> None:
	rc, out, calls, _ = _run_watcher(run_status="in_progress")
	assert rc == 1, out
	assert IN_PROGRESS_DIAG in out, out
	assert QUEUED_DIAG not in out, out
	assert _cancels(calls) == [CANCEL_CALL], calls
	assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_switch_false_skips_the_cancel() -> None:
	for switch in ("false", "FALSE"):
		rc, out, calls, _ = _run_watcher(cancel_switch=switch)
		assert rc == 1, (switch, out)
		assert not _cancels(calls), (switch, calls)
		assert "Cancel of watched run #4242 skipped (DISPATCH_WATCH_CANCEL_ON_TIMEOUT=false)" in out, out
		assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_unrecognised_switch_skips_the_cancel_without_printing_the_value() -> None:
	for switch in ("off", "0"):
		rc, out, calls, _ = _run_watcher(cancel_switch=switch)
		assert rc == 1, (switch, out)
		assert not _cancels(calls), (switch, calls)
		warning = "::warning::DISPATCH_WATCH_CANCEL_ON_TIMEOUT is neither true nor false; treating it as false"
		assert warning in out.splitlines(), out
		assert "Cancel of watched run #4242 skipped (DISPATCH_WATCH_CANCEL_ON_TIMEOUT unrecognised)" in out, out
		assert "DISPATCH_WATCH_CANCEL_ON_TIMEOUT=false" not in out, out
		assert f"={switch}" not in out and f"'{switch}'" not in out, out


def test_true_or_empty_switch_cancels() -> None:
	for switch in ("", "TRUE", "true"):
		rc, out, calls, _ = _run_watcher(cancel_switch=switch)
		assert rc == 1, (switch, out)
		assert _cancels(calls) == [CANCEL_CALL], (switch, calls)
		assert "neither true nor false" not in out, out


def test_failed_cancel_only_warns_and_still_fails() -> None:
	rc, out, calls, _ = _run_watcher(cancel_fail=True)
	assert rc == 1, out
	assert _cancels(calls) == [CANCEL_CALL], calls
	assert "::warning::Could not cancel watched run #4242 after the completion watch timeout" in out, out
	assert "HTTP 409" not in out, out
	assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_failed_status_reads_report_unknown_and_still_cancel() -> None:
	rc, out, calls, _ = _run_watcher(status_fail=True)
	assert rc == 1, out
	assert UNKNOWN_DIAG in out, out
	assert _cancels(calls) == [CANCEL_CALL], calls
	assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_successful_completion_makes_no_cancel_and_no_diagnosis() -> None:
	rc, out, calls, github_output = _run_watcher(run_status="completed", run_conclusion="success")
	assert rc == 0, out
	assert not _cancels(calls), calls
	assert "watch diagnosis" not in out, out
	assert "run_id=4242" in github_output.splitlines(), github_output


def test_failed_completion_makes_no_cancel() -> None:
	rc, out, calls, _ = _run_watcher(run_status="completed", run_conclusion="failure")
	assert rc == 1, out
	assert "::error::validate run #4242 concluded failure" in out, out
	assert not _cancels(calls), calls
	assert "watch diagnosis" not in out, out
	assert "timed out" not in out, out


# --- Runner-wait classification and gate stop (issue #10037) ---

GATE_CANCEL_CALL = "api -X POST repos/owner/gate/actions/runs/9999/cancel"
GATE_ENV = {"GITHUB_RUN_ID": "9999", "GITHUB_REPOSITORY": "owner/gate", "DISPATCH_WATCH_STOP_WAIT_SECS": "0"}


def _iso(delta_seconds: int) -> str:
	return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jobs(*jobs: dict) -> str:
	return json.dumps({"total_count": len(jobs), "jobs": list(jobs)})


QUEUED_JOB = {"id": 1, "status": "queued", "runner_name": "", "steps": []}
LATE_JOB = {"id": 2, "status": "in_progress", "runner_name": "GitHub Actions 7", "steps": [{"name": "Set up job", "started_at": _iso(3600)}]}
EARLY_JOB = {"id": 3, "status": "in_progress", "runner_name": "GitHub Actions 8", "steps": [{"name": "Set up job", "started_at": "2020-01-01T00:00:00Z"}]}
RUNNER_NO_STEPS_JOB = {"id": 4, "status": "in_progress", "runner_name": "GitHub Actions 9", "steps": []}


def _result_line(out: str) -> str:
	return next(line for line in out.splitlines() if line.startswith("DISPATCH_WATCH_RESULT "))


def _gate_calls(calls: list[str]) -> list[str]:
	return [c for c in calls if "owner/gate" in c]


def test_never_assigned_job_is_runner_wait() -> None:
	rc, out, calls, github_output = _run_watcher(extra_env={"STUB_JOBS_BODY": _jobs(QUEUED_JOB)})
	assert rc == 1, out
	assert _result_line(out) == "DISPATCH_WATCH_RESULT outcome=runner_wait run=4242 reason=never_assigned jobs=1 queued_without_runner=1"
	assert "watch_result=runner_wait" in github_output.splitlines(), github_output
	assert _cancels(calls) == [CANCEL_CALL], calls
	# Stop is off by default: no gate calls, the timeout line stays last.
	assert not _gate_calls(calls), calls
	assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_empty_jobs_list_counts_only_when_queued_the_whole_time() -> None:
	rc, out, _, _ = _run_watcher(extra_env={"STUB_JOBS_BODY": _jobs()})
	assert "outcome=runner_wait run=4242 reason=never_assigned jobs=0" in _result_line(out), out
	rc, out, _, github_output = _run_watcher(run_status="in_progress", extra_env={"STUB_JOBS_BODY": _jobs()})
	assert "outcome=timeout run=4242 reason=no_jobs" in _result_line(out), out
	assert "watch_result=timeout" in github_output.splitlines()


def test_late_assigned_job_is_runner_wait() -> None:
	rc, out, _, _ = _run_watcher(run_status="in_progress", extra_env={"STUB_JOBS_BODY": _jobs(LATE_JOB, QUEUED_JOB)})
	assert rc == 1, out
	assert _result_line(out) == "DISPATCH_WATCH_RESULT outcome=runner_wait run=4242 reason=late_assigned jobs=2 queued_without_runner=1"


def test_job_that_ran_early_and_overran_is_a_timeout() -> None:
	for jobs in ((EARLY_JOB,), (LATE_JOB, EARLY_JOB), (RUNNER_NO_STEPS_JOB,)):
		rc, out, calls, github_output = _run_watcher(run_status="in_progress", extra_env={"STUB_JOBS_BODY": _jobs(*jobs), **GATE_ENV, "DISPATCH_WATCH_STOP_RELEASE_GATE": "true"})
		assert rc == 1, out
		assert "outcome=timeout run=4242 reason=ran_early" in _result_line(out), (jobs, out)
		assert "watch_result=timeout" in github_output.splitlines()
		assert not _gate_calls(calls), calls
		assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_unreadable_jobs_is_a_timeout() -> None:
	rc, out, calls, _ = _run_watcher(extra_env={**GATE_ENV, "DISPATCH_WATCH_STOP_RELEASE_GATE": "true"})
	assert rc == 1, out
	assert "outcome=timeout run=4242 reason=jobs_unreadable jobs=- queued_without_runner=-" in _result_line(out), out
	assert not _gate_calls(calls), calls


def test_job_waiting_on_an_environment_or_concurrency_group_is_a_timeout() -> None:
	# G3 names status=queued only; a waiting/pending job is not blocked on a runner.
	for status in ("waiting", "pending"):
		job = {"id": 5, "status": status, "runner_name": "", "steps": []}
		rc, out, calls, _ = _run_watcher(run_status="in_progress", extra_env={"STUB_JOBS_BODY": _jobs(job), **GATE_ENV, "DISPATCH_WATCH_STOP_RELEASE_GATE": "true"})
		assert rc == 1, out
		assert "outcome=timeout run=4242 reason=held jobs=1 queued_without_runner=0" in _result_line(out), (status, out)
		assert not _gate_calls(calls), calls


def test_late_started_job_that_failed_is_a_timeout() -> None:
	# A job that got a runner late and then failed is a real failure, even
	# while another job is still queued without a runner.
	late_failed = dict(LATE_JOB, status="completed", conclusion="failure")
	late_failed_step = dict(LATE_JOB, steps=[{"name": "Set up job", "started_at": _iso(3600), "conclusion": "failure"}])
	for job in (late_failed, late_failed_step):
		rc, out, calls, _ = _run_watcher(run_status="in_progress", extra_env={"STUB_JOBS_BODY": _jobs(job, QUEUED_JOB), **GATE_ENV, "DISPATCH_WATCH_STOP_RELEASE_GATE": "true"})
		assert rc == 1, out
		assert "outcome=timeout run=4242 reason=ran_early" in _result_line(out), (job, out)
		assert not _gate_calls(calls), calls


def test_runner_wait_with_stop_enabled_cancels_the_gate() -> None:
	env = {"STUB_JOBS_BODY": _jobs(QUEUED_JOB), "STUB_GATE_JOBS_BODY": _jobs({"id": 7, "conclusion": None}, {"id": 8, "conclusion": "success"}), **GATE_ENV, "DISPATCH_WATCH_STOP_RELEASE_GATE": "TRUE"}
	rc, out, calls, _ = _run_watcher(extra_env=env)
	assert rc == 1, out
	assert "RELEASE_GATE_RUNNER_WAIT outcome=cancel_requested gate_run=9999 watched_run=4242 reason=never_assigned display_name=validate" in out, out
	assert GATE_CANCEL_CALL in calls, calls
	# The child is cancelled before the gate.
	assert calls.index(CANCEL_CALL) < calls.index(GATE_CANCEL_CALL)
	# The stub runner never kills the step: the stop is reported as failed
	# and the timeout line stays last for the workflow's parser.
	assert "RELEASE_GATE_RUNNER_WAIT outcome=stop_failed detail=still_running gate_run=9999" in out, out
	assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_runner_wait_does_not_cancel_a_gate_with_a_real_failure() -> None:
	env = {"STUB_JOBS_BODY": _jobs(QUEUED_JOB), "STUB_GATE_JOBS_BODY": _jobs({"id": 7, "conclusion": "failure"}), **GATE_ENV, "DISPATCH_WATCH_STOP_RELEASE_GATE": "true"}
	rc, out, calls, _ = _run_watcher(extra_env=env)
	assert rc == 1, out
	assert "RELEASE_GATE_RUNNER_WAIT outcome=real_failure_present gate_run=9999 watched_run=4242" in out, out
	assert GATE_CANCEL_CALL not in calls, calls
	assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_runner_wait_does_not_cancel_a_gate_with_a_failed_step_in_a_running_job() -> None:
	# A gate job still running its always() steps after a failed step has
	# conclusion null; the failed step alone blocks the cancel.
	running_failed = {"id": 7, "status": "in_progress", "conclusion": None, "steps": [{"name": "Dispatch", "conclusion": "failure"}, {"name": "Cleanup", "conclusion": None}]}
	env = {"STUB_JOBS_BODY": _jobs(QUEUED_JOB), "STUB_GATE_JOBS_BODY": _jobs(running_failed), **GATE_ENV, "DISPATCH_WATCH_STOP_RELEASE_GATE": "true"}
	rc, out, calls, _ = _run_watcher(extra_env=env)
	assert rc == 1, out
	assert "RELEASE_GATE_RUNNER_WAIT outcome=real_failure_present gate_run=9999 watched_run=4242" in out, out
	assert GATE_CANCEL_CALL not in calls, calls
	assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_runner_wait_stop_failures_keep_the_timeout_line_last() -> None:
	cases = (
		({"STUB_GATE_JOBS_BODY": _jobs()}, "detail=cancel_failed", {"STUB_GATE_CANCEL_FAIL": "1"}),
		({}, "detail=gate_jobs_unreadable", {}),
		({"STUB_GATE_JOBS_BODY": _jobs(), "GITHUB_RUN_ID": "abc"}, "detail=no_gate_run", {}),
	)
	for env_extra, detail, more in cases:
		env = {"STUB_JOBS_BODY": _jobs(QUEUED_JOB), **GATE_ENV, "DISPATCH_WATCH_STOP_RELEASE_GATE": "true", **env_extra, **more}
		rc, out, _, _ = _run_watcher(extra_env=env)
		assert rc == 1, out
		assert f"RELEASE_GATE_RUNNER_WAIT outcome=stop_failed {detail}" in out, (detail, out)
		assert out.splitlines()[-1] == TIMEOUT_LINE, out


def test_stop_release_gate_cli_mode_cancels_child_and_gate() -> None:
	env = {"STUB_GATE_JOBS_BODY": _jobs({"id": 7, "conclusion": None}), **GATE_ENV}
	args = ["--stop-release-gate", "--repo", "owner/repo", "--watched-run-id", "4242", "--display-name", "review", "--runner-wait-reason", "late_assigned"]
	rc, out, calls, _ = _run_watcher(extra_env=env, args=args)
	assert rc == 1, out
	assert calls[0] == CANCEL_CALL, calls
	assert GATE_CANCEL_CALL in calls, calls
	assert "RELEASE_GATE_RUNNER_WAIT outcome=cancel_requested gate_run=9999 watched_run=4242 reason=late_assigned display_name=review" in out, out
	# Bad arguments never call the API.
	rc, out, calls, _ = _run_watcher(extra_env=env, args=["--stop-release-gate", "--repo", "owner/repo", "--watched-run-id", "x"])
	assert rc == 2 and not calls, (out, calls)


# --- Alt-model wait runner-wait check (issue #10037, G3) ---

ALT_GH_STUB = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_GH_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\\n")
state = json.load(open(os.environ["STUB_ALT_STATE"], encoding="utf-8"))
fields, path, i = {}, "", 1
while i < len(args):
    if args[i] == "-X":
        i += 2
        continue
    if args[i] == "-f":
        key, _, value = args[i + 1].partition("=")
        fields[key] = value
        i += 2
        continue
    path = args[i]
    i += 1
if path.endswith("/cancel"):
    sys.exit(0)
if state.get("fail"):
    sys.exit(1)
if path.endswith("/actions/runs"):
    runs = state.get(fields.get("status", ""), [])
    print(json.dumps({"total_count": state.get(fields.get("status", "") + "_total", len(runs)), "workflow_runs": runs}))
    sys.exit(0)
if "/jobs" in path:
    run_id = path.split("/runs/")[1].split("/")[0]
    if run_id in state.get("jobs", {}):
        print(json.dumps(state["jobs"][run_id]))
        sys.exit(0)
sys.exit(1)
"""
ALT_TITLE = "[E2E alt-model] add a helper"


def _alt_run(run_id: int, status: str, created_delta: int, title: str = ALT_TITLE) -> dict:
	return {"id": run_id, "status": status, "display_title": title, "created_at": _iso(created_delta)}


def _run_alt_check(state: dict, stop: bool = False) -> tuple[str, list[str], str]:
	bash = shutil.which("bash")
	assert bash is not None
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		bindir = tmp / "bin"
		bindir.mkdir()
		gh = bindir / "gh"
		gh.write_text(ALT_GH_STUB, encoding="utf-8")
		gh.chmod(0o755)
		(tmp / "state.json").write_text(json.dumps(state), encoding="utf-8")
		for name in ("gh.log", "output"):
			(tmp / name).write_text("", encoding="utf-8")
		env = {
			"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
			"HOME": str(tmp), "GITHUB_OUTPUT": str(tmp / "output"), "STUB_GH_LOG": str(tmp / "gh.log"),
			"STUB_ALT_STATE": str(tmp / "state.json"), "TEST_REPO": "owner/repo", **GATE_ENV,
			"ALT_ISSUE_TITLE": ALT_TITLE, "ALT_CREATED_AFTER": _iso(-4600),
			"ALT_WAIT_STARTED_AT": str(int(datetime.now(timezone.utc).timestamp()) - 4500), "ALT_WAIT_BUDGET_SECS": "4500",
		}
		if stop:
			env["DISPATCH_WATCH_STOP_RELEASE_GATE"] = "true"
		proc = subprocess.run([bash, str(REPO_ROOT / "scripts" / "dispatch_and_watch_workflow_run.sh"), "--alt-model-runner-wait"], cwd=tmp, env=env,
			capture_output=True, text=True, check=False, timeout=60)
		assert proc.returncode == 0, proc.stdout + proc.stderr
		calls = [line for line in (tmp / "gh.log").read_text(encoding="utf-8").splitlines() if line]
		return proc.stdout, calls, (tmp / "output").read_text(encoding="utf-8")


def test_alt_model_runs_queued_since_early_are_a_runner_wait_and_stop_the_gate() -> None:
	queued_jobs = json.loads(_jobs(QUEUED_JOB))
	state = {"queued": [_alt_run(51, "queued", -4000), _alt_run(52, "queued", -300), _alt_run(53, "queued", -4000, title="other")], "in_progress": [],
		"jobs": {"51": queued_jobs, "52": queued_jobs}}
	out, calls, github_output = _run_alt_check(state)
	assert "ALT_MODEL_RUNNER_WAIT outcome=runner_wait reason=never_assigned runs=51,52" in out, out
	assert "runner_wait=true" in github_output.splitlines()
	assert not [c for c in calls if "/cancel" in c], calls
	state["jobs"]["9999"] = json.loads(_jobs({"id": 7, "status": "in_progress", "conclusion": None, "steps": []}))
	out, calls, _ = _run_alt_check(state, stop=True)
	assert "api -X POST repos/owner/repo/actions/runs/51/cancel" in calls, calls
	assert "api -X POST repos/owner/repo/actions/runs/52/cancel" in calls, calls
	assert GATE_CANCEL_CALL in calls, calls
	assert "RELEASE_GATE_RUNNER_WAIT outcome=cancel_requested gate_run=9999 watched_run=51 reason=never_assigned display_name=alt-model" in out, out


def test_alt_model_late_runner_with_queued_sibling_is_late_assigned() -> None:
	jobs = {"61": json.loads(_jobs(dict(LATE_JOB, steps=[{"name": "Set up job", "started_at": _iso(-300)}]), QUEUED_JOB))}
	state = {"queued": [], "in_progress": [_alt_run(61, "in_progress", -4000)], "jobs": jobs}
	out, _, github_output = _run_alt_check(state)
	assert "ALT_MODEL_RUNNER_WAIT outcome=runner_wait reason=late_assigned runs=61" in out, out
	assert "runner_wait=true" in github_output.splitlines()


def test_alt_model_real_slowness_or_unreadable_state_stays_a_timeout() -> None:
	early = {"61": json.loads(_jobs(EARLY_JOB))}
	queued_jobs = {"51": json.loads(_jobs(QUEUED_JOB))}
	held_jobs = {"51": json.loads(_jobs({"id": 5, "status": "waiting", "runner_name": "", "steps": []}))}
	failed_late = {"61": json.loads(_jobs(dict(LATE_JOB, conclusion="failure", steps=[{"name": "Set up job", "started_at": _iso(-300)}])))}
	cases = (
		({"queued": [], "in_progress": [_alt_run(61, "in_progress", -4000)], "jobs": early}, "reason=ran_early runs=61"),
		({"queued": [], "in_progress": [_alt_run(61, "in_progress", -4000)], "jobs": failed_late}, "reason=ran_early runs=61"),
		({"queued": [], "in_progress": [_alt_run(61, "in_progress", -4000)]}, "reason=jobs_unreadable runs=61"),
		({"queued": [_alt_run(51, "queued", -600)], "in_progress": [], "jobs": queued_jobs}, "reason=queued_late runs=51"),
		# Held runs (concurrency group or environment) are not a runner wait.
		({"queued": [_alt_run(51, "queued", -4000)], "in_progress": [], "pending": [_alt_run(54, "pending", -4000)], "jobs": queued_jobs}, "reason=held runs=54"),
		({"queued": [_alt_run(51, "queued", -4000)], "in_progress": [], "waiting": [_alt_run(55, "waiting", -4000)], "jobs": queued_jobs}, "reason=held runs=55"),
		({"queued": [_alt_run(51, "queued", -4000)], "in_progress": [], "jobs": held_jobs}, "reason=held runs=51"),
		({"queued": [_alt_run(51, "queued", -4000)], "in_progress": []}, "reason=jobs_unreadable runs=51"),
		({"queued": [_alt_run(53, "queued", -4000, title="other")], "in_progress": []}, "reason=no_runs_in_flight runs=-"),
		({"fail": True}, "reason=listing_unreadable runs=-"),
		({"queued": [_alt_run(51, "queued", -4000)], "in_progress": [], "in_progress_total": 150}, "reason=listing_truncated runs=-"),
	)
	for state, expected in cases:
		out, calls, github_output = _run_alt_check(state, stop=True)
		assert f"ALT_MODEL_RUNNER_WAIT outcome=timeout {expected}" in out, (expected, out)
		assert "runner_wait=true" not in github_output
		assert not [c for c in calls if "/cancel" in c], (expected, calls)


def test_gate_workflow_wires_the_alt_model_check_and_child_cancels() -> None:
	text = (REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml").read_text(encoding="utf-8")
	wait = text.index("      - name: Wait for clarify→plan→implement (alt-model)\n")
	check = text.index("bash ./scripts/dispatch_and_watch_workflow_run.sh --alt-model-runner-wait", wait)
	assert check < text.index("Alt-model run timed out before reaching review stage", wait)
	assert text.count("      - name: Cancel dispatched child runs when the gate is cancelled\n        if: cancelled()\n") == 2
	assert "steps.dispatch_memory_maintenance.outputs.dispatched_run_id" in text


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
