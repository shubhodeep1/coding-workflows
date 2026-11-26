"""Contract checks for the Phase 4 review ``timeout_reason`` output (#9619).

Refs #9418: a release-gate Phase 4 inactivity timeout whose watched review
run stayed queued/pending with no started job for the whole wait is
environmental, not a review stall. ``scripts/stable_e2e_step_wait_review.sh``
keeps writing ``status=timeout`` on that path and adds
``timeout_reason=review_run_never_started`` (or ``inactivity`` otherwise),
plus one ``E2E_REVIEW_TIMEOUT_REASON`` log line. No new GitHub API call.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "stable_e2e_step_wait_review.sh"

BLOCK_START = "# BEGIN phase4 review timeout reason (#9619)"
BLOCK_END = "# END phase4 review timeout reason (#9619)"
STATUS_TIMEOUT_LINE = 'echo "status=timeout" >> "$GITHUB_OUTPUT"'
STALL_ERROR = 'echo "::error::Review phase stalled'
DEADLINE_ERROR = "total wall-clock budget$(e2e_watched_run_urls)"
REASON_OUTPUT_LINE = 'echo "timeout_reason=${E2E_REVIEW_TIMEOUT_REASON_VALUE}" >> "$GITHUB_OUTPUT"'
# Count of API call sites before #9619; the change must not add one.
API_CALL_SITES = 14


def _script() -> str:
	return SCRIPT.read_text(encoding="utf-8")


def _block() -> str:
	text = _script()
	start = text.find(BLOCK_START)
	end = text.find(BLOCK_END, start)
	assert start != -1 and end != -1, "timeout-reason markers missing"
	return text[start:end + len(BLOCK_END)]


def _stall_branch() -> str:
	text = _script()
	start = text.index(STALL_ERROR)
	end = text.index("exit 1", start)
	return text[start:end + len("exit 1")]


def _deadline_branch() -> str:
	text = _script()
	start = text.index(DEADLINE_ERROR)
	end = text.index("exit 1", start)
	return text[start:end + len("exit 1")]


def _require_tools() -> str:
	bash = shutil.which("bash")
	if bash is None or shutil.which("jq") is None:
		raise unittest.SkipTest("bash or jq is not installed")
	return bash


def test_status_timeout_kept_and_reason_written_on_stall_path() -> None:
	text = _script()
	assert text.count(STATUS_TIMEOUT_LINE) == 2
	branch = _stall_branch()
	assert STATUS_TIMEOUT_LINE in branch
	assert REASON_OUTPUT_LINE in branch
	assert branch.index(STATUS_TIMEOUT_LINE) < branch.index(REASON_OUTPUT_LINE)
	assert branch.index(REASON_OUTPUT_LINE) < branch.index("exit 1")
	assert 'echo "E2E_REVIEW_TIMEOUT_REASON run=' in branch
	assert "E2E_REVIEW_TIMEOUT_REASON_VALUE=$(e2e_review_timeout_reason)" in branch


def test_reason_values_present() -> None:
	block = _block()
	assert 'echo "review_run_never_started"' in block
	assert 'echo "inactivity"' in block


def test_deadline_path_has_no_timeout_reason() -> None:
	assert "timeout_reason" not in _deadline_branch()


def test_no_new_api_calls() -> None:
	text = _script()
	assert text.count('gh_api_safe_quiet_print "') == API_CALL_SITES
	assert "gh_api" not in _block()
	assert "gh " not in _block()


def test_note_called_each_poll_before_commit_probe() -> None:
	text = _script()
	loop = text[text.index("while true; do"):]
	assert loop.count("  e2e_review_note_run_started\n") == 1
	assert loop.index("e2e_review_note_run_started") < loop.index("# 2) Commit count on PR branch")
	assert loop.index("# 1) Current step name") < loop.index("e2e_review_note_run_started")


def _run_helpers(setup: str) -> str:
	bash = _require_tools()
	harness = (
		"set -euo pipefail\n"
		f"{_block()}\n"
		# Default poll clock: run first seen at the start of the idle window.
		"NOW=1000 LAST_ACTIVITY=1000\n"
		f"{setup}\n"
		"e2e_review_note_run_started\n"
		'echo "REASON=$(e2e_review_timeout_reason)"\n'
		'echo "JOBS=$(e2e_review_timeout_job_statuses)"\n'
	)
	result = subprocess.run([bash, "-c", harness], capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	return result.stdout


QUEUED_JOBS = '{"total_count":2,"jobs":[{"id":5,"name":"gate","status":"queued"},{"id":6,"name":"a::b","status":"waiting"}]}'


def test_helper_cases() -> None:
	cases = [
		# Queued run, all jobs queued/waiting, fetched this poll.
		(f"RUN_ID=101 RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{QUEUED_JOBS}'",
			"review_run_never_started", "5=queued,6=waiting"),
		# Pending run with zero jobs (workflow-level concurrency hold).
		("RUN_ID=101 RUN_STATUS=pending E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{\"total_count\":0,\"jobs\":[]}'",
			"review_run_never_started", "none"),
		# Flag already set by an earlier poll of the same run.
		(f"E2E_REVIEW_RUN_EVER_STARTED=1 E2E_REVIEW_STARTED_FOR_RUN_ID=101 RUN_ID=101 RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{QUEUED_JOBS}'",
			"inactivity", "5=queued,6=waiting"),
		# Run in progress.
		("RUN_ID=101 RUN_STATUS=in_progress JOBS_JSON='{\"jobs\":[{\"id\":5,\"status\":\"in_progress\"}]}'",
			"inactivity", "5=in_progress"),
		# One job started while the run still reports queued.
		("RUN_ID=101 RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{\"jobs\":[{\"id\":5,\"status\":\"in_progress\"}]}'",
			"inactivity", "5=in_progress"),
		# Jobs payload unavailable (not fetched this poll).
		(f"RUN_ID=101 RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=0 JOBS_STEP_JSON='{QUEUED_JOBS}'",
			"inactivity", "unavailable"),
		# Partial jobs page (total_count above the listed jobs): not classified.
		("RUN_ID=101 RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{\"total_count\":12,\"jobs\":[{\"id\":5,\"status\":\"queued\"}]}'",
			"inactivity", "5=queued"),
		# Jobs payload without total_count: completeness unknown, not classified.
		("RUN_ID=101 RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{\"jobs\":[{\"id\":5,\"status\":\"queued\"}]}'",
			"inactivity", "5=queued"),
		# Jobs payload invalid.
		("RUN_ID=101 RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='not json'",
			"inactivity", "unavailable"),
		# No watched run.
		(f"RUN_ID=unknown RUN_STATUS=none E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{QUEUED_JOBS}'",
			"inactivity", "5=queued,6=waiting"),
	]
	for setup, reason, jobs in cases:
		out = _run_helpers(setup)
		assert f"REASON={reason}\n" in out, (setup, out)
		assert f"JOBS={jobs}\n" in out, (setup, out)
		assert "::" not in out


def test_flag_is_sticky_across_polls() -> None:
	out = _run_helpers(
		"RUN_ID=101 RUN_STATUS=in_progress JOBS_JSON='{\"jobs\":[]}'\n"
		"e2e_review_note_run_started\n"
		f"RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{QUEUED_JOBS}'"
	)
	assert "REASON=inactivity\n" in out


def test_flag_resets_when_watched_run_changes() -> None:
	# A started run is cancelled and the loop pivots to a queued successor.
	out = _run_helpers(
		"RUN_ID=100 RUN_STATUS=in_progress JOBS_JSON='{\"jobs\":[{\"id\":4,\"status\":\"in_progress\"}]}'\n"
		"e2e_review_note_run_started\n"
		f"RUN_ID=101 RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{QUEUED_JOBS}'"
	)
	assert "REASON=review_run_never_started\n" in out
	assert "JOBS=5=queued,6=waiting\n" in out


def test_pivot_inside_idle_window_reports_inactivity() -> None:
	# Queued run 100 is replaced by queued run 101 in the same state, so
	# LAST_ACTIVITY (500) predates the successor's first poll (1000).
	out = _run_helpers(
		"LAST_ACTIVITY=500 NOW=500 RUN_ID=100 RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=1 "
		f"JOBS_STEP_JSON='{QUEUED_JOBS}'\n"
		"e2e_review_note_run_started\n"
		f"NOW=1000 RUN_ID=101 RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{QUEUED_JOBS}'"
	)
	assert "REASON=inactivity\n" in out
	assert "JOBS=5=queued,6=waiting\n" in out
	# The same run watched since the start of the window still classifies.
	out = _run_helpers(
		"LAST_ACTIVITY=500 NOW=500 RUN_ID=101 RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=1 "
		f"JOBS_STEP_JSON='{QUEUED_JOBS}'\n"
		"e2e_review_note_run_started\n"
		f"NOW=1000 E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{QUEUED_JOBS}'"
	)
	assert "REASON=review_run_never_started\n" in out


def test_unknown_run_id_poll_keeps_flag() -> None:
	out = _run_helpers(
		"RUN_ID=101 RUN_STATUS=in_progress JOBS_JSON='{\"jobs\":[]}'\n"
		"e2e_review_note_run_started\n"
		"RUN_ID=unknown RUN_STATUS=none E2E_REVIEW_JOBS_STEP_FRESH=0\n"
		"e2e_review_note_run_started\n"
		f"RUN_ID=101 RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{QUEUED_JOBS}'"
	)
	assert "REASON=inactivity\n" in out


def test_completed_run_without_started_job_does_not_set_flag() -> None:
	# Cancelled while queued: completed, but no job ever ran; then re-queued.
	out = _run_helpers(
		"RUN_ID=101 RUN_STATUS=completed E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{\"jobs\":[{\"id\":5,\"status\":\"queued\"}]}'\n"
		"e2e_review_note_run_started\n"
		f"RUN_STATUS=queued JOBS_STEP_JSON='{QUEUED_JOBS}'"
	)
	assert "REASON=review_run_never_started\n" in out
	out = _run_helpers(
		"RUN_ID=101 RUN_STATUS=completed E2E_REVIEW_JOBS_STEP_FRESH=1 JOBS_STEP_JSON='{\"jobs\":[{\"id\":5,\"status\":\"completed\"}]}'\n"
		"e2e_review_note_run_started\n"
		f"RUN_STATUS=queued JOBS_STEP_JSON='{QUEUED_JOBS}'"
	)
	assert "REASON=inactivity\n" in out


def test_cached_jobs_json_dropped_on_run_change() -> None:
	text = _script()
	loop = text[text.index("while true; do"):]
	tag = 'E2E_REVIEW_JOBS_JSON_RUN_ID="$RUN_ID"'
	assert loop.count(tag) == 1
	assert loop.index('      JOBS_JSON=$(gh_api_safe_quiet_print') < loop.index(tag)
	reset = 'if [ -n "${JOBS_JSON:-}" ] && [ "${E2E_REVIEW_JOBS_JSON_RUN_ID:-}" != "$RUN_ID" ]; then\n    JOBS_JSON=""\n  fi'
	assert loop.count(reset) == 1
	step_block = loop.index("# 1) Current step name")
	assert step_block < loop.index(reset) < loop.index('    if [ -n "${JOBS_JSON:-}" ]; then\n      CURRENT_STEP=')


def test_job_statuses_drop_unsafe_entries() -> None:
	out = _run_helpers(
		"RUN_ID=101 RUN_STATUS=queued E2E_REVIEW_JOBS_STEP_FRESH=1 "
		"JOBS_STEP_JSON='{\"jobs\":[{\"id\":\"x::set-output\",\"status\":\"queued\"},"
		"{\"id\":7,\"status\":\"Bad Status::x\"},{\"id\":8,\"status\":\"queued\"}]}'"
	)
	assert "JOBS=8=queued\n" in out


GH_STUB = r"""
sleep() { :; }
gh_api_safe_quiet_print() {
  local api_path="$1" jq_filter="" body=""
  shift
  if [ "${1:-}" = "--jq" ]; then jq_filter="$2"; fi
  case "$api_path" in
    */actions/runs\?branch=*) body="$(cat "$FIXTURE_DIR/runs.json")" ;;
    */actions/runs/*/jobs*) body="$(cat "$FIXTURE_DIR/jobs.json")" ;;
    */actions/jobs/*/logs) body="" ;;
    */pulls/*/comments) body="[]" ;;
    */pulls/*) body='{"head":{"sha":"abc1234"}}' ;;
    *) return 1 ;;
  esac
  if [ -n "$jq_filter" ]; then
    printf '%s' "$body" | jq -r "$jq_filter"
  else
    printf '%s' "$body"
  fi
}
"""


def _run_script(run_status: str, job_status: str) -> tuple[str, str]:
	bash = _require_tools()
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		(tmp_path / "scripts").mkdir()
		(tmp_path / "scripts" / "comprehensive_test_and_release_gh_api.sh").write_text(GH_STUB, encoding="utf-8")
		(tmp_path / "runs.json").write_text(
			'{"workflow_runs":[{"id":101,"name":"Internal: AI Review & Autofix",'
			'"path":".github/workflows/internal-review.yml","event":"pull_request",'
			f'"status":"{run_status}","conclusion":null,'
			'"created_at":"2026-11-16T00:00:00Z","head_sha":"abc1234"}]}',
			encoding="utf-8",
		)
		(tmp_path / "jobs.json").write_text(
			f'{{"total_count":1,"jobs":[{{"id":5,"name":"review / codex-agent","status":"{job_status}","steps":[]}}]}}',
			encoding="utf-8",
		)
		output = tmp_path / "github_output"
		output.write_text("", encoding="utf-8")
		env = {
			"PATH": os.environ.get("PATH", ""),
			"HOME": tmp,
			"FIXTURE_DIR": tmp,
			"GITHUB_OUTPUT": str(output),
			"PR_NUMBER": "1",
			"ISSUE_NUMBER": "2",
			"TEST_REPO": "o/r",
			"REVIEW_TIMEOUT": "0",
			"REVIEW_PHASE_HANDOFF_BUDGET_MINUTES": "5",
			"POLL_INTERVAL": "1",
			"BAIT_SHA": "",
			"BAIT_CREATED_AT": "",
		}
		result = subprocess.run(
			[bash, str(SCRIPT)], cwd=tmp, env=env, capture_output=True, text=True, check=False, timeout=60
		)
		assert result.returncode == 1, (result.stdout, result.stderr)
		return result.stdout, output.read_text(encoding="utf-8")


def test_script_writes_never_started_reason_for_queued_run() -> None:
	stdout, outputs = _run_script("queued", "queued")
	assert "status=timeout\n" in outputs
	assert "timeout_reason=review_run_never_started\n" in outputs
	assert "E2E_REVIEW_TIMEOUT_REASON run=101 run_status=queued reason=review_run_never_started jobs=5=queued" in stdout


def test_script_writes_inactivity_reason_for_started_run() -> None:
	stdout, outputs = _run_script("in_progress", "in_progress")
	assert "status=timeout\n" in outputs
	assert "timeout_reason=inactivity\n" in outputs
	assert "E2E_REVIEW_TIMEOUT_REASON run=101 run_status=in_progress reason=inactivity jobs=5=in_progress" in stdout
