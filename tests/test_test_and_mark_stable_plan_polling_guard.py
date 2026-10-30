#!/usr/bin/env python3
"""Regression checks for plan-run polling guard in test-and-mark-stable workflow."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

ISSUE_NUMBER = "4712"
ISSUE_TITLE = "[E2E Smoke Test] Update smoke-test canary (run 1)"
CREATED_AFTER = "2026-09-28T03:46:08Z"

# Stub `gh`: serves the issue labels, an empty comment list, and the
# actions/runs pages from STUB_RUN_PAGES (a JSON list of pages); applies --jq
# with the real jq; logs every call to STUB_GH_LOG.
STUB_GH = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys
args = sys.argv[1:]
with open(os.environ["STUB_GH_LOG"], "a", encoding="utf-8") as log:
	log.write(json.dumps(args) + "\n")
if not args or args[0] != "api":
	sys.exit(1)
path = args[1]
jq_filter = args[args.index("--jq") + 1] if "--jq" in args else None
issue_path = "repos/" + os.environ["TEST_REPO"] + "/issues/" + os.environ["ISSUE_NUMBER"]
if path == issue_path:
	# STUB_LABELS until STUB_LABELS_SWITCH_AFTER issue reads, then STUB_LABELS_LATER.
	with open(os.environ["STUB_GH_LOG"], encoding="utf-8") as log:
		issue_reads = sum(1 for line in log if json.loads(line)[1:2] == [issue_path])
	labels = os.environ["STUB_LABELS"]
	if os.environ.get("STUB_LABELS_SWITCH_AFTER") and issue_reads > int(os.environ["STUB_LABELS_SWITCH_AFTER"]):
		labels = os.environ["STUB_LABELS_LATER"]
	payload = {"labels": [{"name": name} for name in labels.split(",") if name]}
elif path.startswith(issue_path + "/comments") and "-f" in args:
	# Phase 3a's /approved POST (issue #8391).
	payload = {"id": 9001, "created_at": os.environ.get("STUB_POST_CREATED_AT", "2026-09-29T00:48:40Z")}
elif path.startswith(issue_path + "/comments"):
	if os.environ.get("STUB_COMMENTS_FAIL"):
		sys.stderr.write("HTTP 502: Bad Gateway\n")
		sys.exit(1)
	payload = json.loads(os.environ.get("STUB_COMMENTS", "[]"))
elif "/pulls?" in path:
	# wait-implement's PR lookup: one open PR when STUB_PR_NUMBER is set.
	with open(os.environ["STUB_GH_LOG"], encoding="utf-8") as log:
		pr_reads = sum(1 for line in log if "/pulls?" in json.loads(line)[1])
	pr_number = os.environ.get("STUB_PR_RECHECK_NUMBER") if pr_reads > 1 else os.environ.get("STUB_PR_NUMBER")
	payload = [{"number": int(pr_number)}] if pr_number else []
elif "/actions/runs?" in path and "status=in_progress" in path:
	# wait-implement's stall diagnostics read of every in-progress run.
	payload = {"workflow_runs": json.loads(os.environ.get("STUB_IN_PROGRESS_RUNS", "[]"))}
elif "/actions/runs?" in path and os.environ.get("STUB_UNPAGED_RUNS_INVALID") and "&page=" not in path:
	# The other-active check's page-1 read (fetch_plan_runs_json) is the only
	# runs request without &page=; this makes just that read unreadable.
	payload = {"workflow_runs": None}
elif "/actions/runs?" in path:
	pages = json.loads(open(os.environ["STUB_RUN_PAGES"], encoding="utf-8").read())
	if os.environ.get("STUB_RUN_PAGES_LATER"):
		with open(os.environ["STUB_GH_LOG"], encoding="utf-8") as log:
			runs_reads = sum(1 for line in log if "/actions/runs?" in json.loads(line)[1])
		if runs_reads >= 3:
			pages = json.loads(open(os.environ["STUB_RUN_PAGES_LATER"], encoding="utf-8").read())
	match = re.search(r"[?&]page=(\d+)", path)
	page = int(match.group(1)) if match else 1
	if os.environ.get("STUB_RUN_PAGE_FAILS") == str(page):
		# A failed read the way gh reports one: a message on stderr, exit 1.
		sys.stderr.write("HTTP 502: Bad Gateway\n")
		sys.exit(1)
	payload = {"workflow_runs": pages[page - 1] if page <= len(pages) else []}
elif re.search(r"/actions/runs/\d+$", path):
	payload = json.loads(os.environ.get("STUB_ACTIVE_RUN_JSON", "null"))
elif re.search(r"/actions/runs/\d+/jobs\?", path):
	payload = json.loads(os.environ.get("STUB_JOBS_JSON", '{"jobs": []}'))
elif "/commits?" in path:
	payload = []
else:
	sys.stderr.write("stub gh: unexpected path " + path + "\n")
	sys.exit(1)
text = json.dumps(payload)
if jq_filter is not None:
	text = subprocess.run(["jq", "-r", jq_filter], input=text, capture_output=True, text=True, check=True).stdout.rstrip("\n")
sys.stdout.write(text)
'''


def _read_workflow() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _wait_plan_script() -> str:
	doc = yaml.safe_load(_read_workflow())
	for step in doc["jobs"]["e2e-smoke-test"]["steps"]:
		if step.get("id") == "wait-plan":
			return step["run"].replace("${{ steps.create-issue.outputs.created_after }}", CREATED_AFTER)
	raise AssertionError("wait-plan step not found")


def _run(run_id: int, name: str = "Internal: AI Plan", title: str = ISSUE_TITLE, conclusion: str | None = "success", status: str = "completed", created_at: str = "2026-09-28T03:50:56Z") -> dict:
	return {"id": run_id, "name": name, "display_title": title, "conclusion": conclusion, "status": status, "created_at": created_at}


def _noise_page(first_id: int, count: int = 100) -> list[dict]:
	# The shape that pushed the real Plan run off page 1: skipped issue_comment
	# runs of every workflow, including skipped Plan runs for this very issue.
	return [
		_run(first_id + i, name="Internal: AI Plan" if i % 2 else "Internal: AI Orchestrate Clarify Respond", conclusion="skipped", created_at="2026-09-28T03:56:40Z")
		for i in range(count)
	]


def _run_wait_plan(pages: list[list[dict]], labels: str, labels_later: str | None = None, switch_after: int = 0, plan_phase_timeout: str = "60", unpaged_runs_invalid: bool = False) -> tuple[int, dict[str, str], list[list[str]], str]:
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		gh = bin_dir / "gh"
		gh.write_text(STUB_GH, encoding="utf-8")
		gh.chmod(0o755)
		sleep = bin_dir / "sleep"
		sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
		sleep.chmod(0o755)
		pages_file = tmp_path / "pages.json"
		pages_file.write_text(json.dumps(pages), encoding="utf-8")
		log_file = tmp_path / "gh.log"
		log_file.touch()
		output_file = tmp_path / "github_output"
		output_file.touch()
		env = dict(os.environ)
		env.update({
			"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			"TEST_REPO": "owner/repo",
			"ISSUE_NUMBER": ISSUE_NUMBER,
			"ISSUE_TITLE": ISSUE_TITLE,
			"PLAN_PHASE_TIMEOUT": plan_phase_timeout,
			"POLL_INTERVAL": "0",
			"GITHUB_OUTPUT": str(output_file),
			"STUB_GH_LOG": str(log_file),
			"STUB_RUN_PAGES": str(pages_file),
			"STUB_LABELS": labels,
		})
		if unpaged_runs_invalid:
			env["STUB_UNPAGED_RUNS_INVALID"] = "1"
		if labels_later is not None:
			env["STUB_LABELS_LATER"] = labels_later
			env["STUB_LABELS_SWITCH_AFTER"] = str(switch_after)
		proc = subprocess.run(["bash", "-c", _wait_plan_script()], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
		outputs: dict[str, str] = {}
		for line in output_file.read_text(encoding="utf-8").splitlines():
			key, _, value = line.partition("=")
			outputs[key] = value
		calls = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
		return proc.returncode, outputs, calls, proc.stdout + proc.stderr


def _run_latest_scoped_run_field(pages: list, fail_page: int | None = None) -> tuple[int, str, str]:
	# Runs latest_scoped_run_field on its own, with the step's gh helpers and
	# the same stub gh, and returns its exit code, stdout, and stderr.
	script = _wait_plan_script()
	functions = script[script.index("PLAN_RUN_LOOKUP_MAX_PAGES=10"):script.index("# Both success exits")]
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		gh = bin_dir / "gh"
		gh.write_text(STUB_GH, encoding="utf-8")
		gh.chmod(0o755)
		# gh_api_safe retries a transient 502 with a 5/10/20 s backoff; keep
		# that instant here as the step-level harnesses below do.
		sleep = bin_dir / "sleep"
		sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
		sleep.chmod(0o755)
		pages_file = tmp_path / "pages.json"
		pages_file.write_text(json.dumps(pages), encoding="utf-8")
		log_file = tmp_path / "gh.log"
		log_file.touch()
		env = dict(os.environ)
		env.update({
			"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			"TEST_REPO": "owner/repo",
			"ISSUE_NUMBER": ISSUE_NUMBER,
			"ISSUE_TITLE": ISSUE_TITLE,
			"STUB_GH_LOG": str(log_file),
			"STUB_RUN_PAGES": str(pages_file),
			"STUB_LABELS": "",
		})
		if fail_page is not None:
			env["STUB_RUN_PAGE_FAILS"] = str(fail_page)
		body = "set -euo pipefail\n. ./scripts/comprehensive_test_and_release_gh_api.sh\n" + functions + "\nlatest_scoped_run_field Plan id\n"
		proc = subprocess.run(["bash", "-c", body], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
		return proc.returncode, proc.stdout, proc.stderr


def _run_pages_requested(calls: list[list[str]]) -> list[str]:
	return [call[1] for call in calls if "/actions/runs?" in call[1]]


def _poll_run_pages_requested(calls: list[list[str]]) -> list[str]:
	# Only the paged polling reads; wait-implement's stall diagnostics read
	# runs lists without &page= (issue #8376).
	return [path for path in _run_pages_requested(calls) if "&page=" in path]


def _stall_diag_reads(calls: list[list[str]]) -> list[str]:
	return [path for path in _run_pages_requested(calls) if "&page=" not in path]


def test_plan_label_without_a_captured_run_id_fails_at_plan_capture() -> None:
	# Issue #4723: the Plan label is present but the lookup finds no run.
	rc, outputs, calls, log = _run_wait_plan([[_run(1, name="Internal: AI Clarify")]], "ai:awaiting-approval")
	assert rc == 1, log
	assert outputs.get("status") == "run_id_missing", outputs
	assert "run_id" not in outputs, outputs
	assert "::error::Issue #4712 reached ai:awaiting-approval but no non-skipped Plan run" in log
	# A short window may still be missing a not-yet-indexed run, so the
	# capture keeps its 5 attempts there: one status poll plus 5 one-page reads.
	assert len(_run_pages_requested(calls)) == 1 + 5, _run_pages_requested(calls)


def test_auto_approved_plan_without_a_captured_run_id_fails_at_plan_capture() -> None:
	rc, outputs, _calls, log = _run_wait_plan([[]], "ai:implementing")
	assert rc == 1, log
	assert outputs.get("status") == "run_id_missing", outputs
	assert "run_id" not in outputs, outputs


def test_plan_run_past_the_first_page_is_found_by_paging() -> None:
	# Run 36374918973: the Plan run sat behind ~100 newer skipped runs.
	pages = [_noise_page(1000), _noise_page(2000, count=40) + [_run(777)]]
	rc, outputs, calls, log = _run_wait_plan(pages, "ai:awaiting-approval")
	assert rc == 0, log
	assert outputs.get("run_id") == "777", outputs
	assert outputs.get("status") == "success", outputs
	requested = _run_pages_requested(calls)
	# The status poll reads page 1 once; the capture reads pages 1 and 2.
	assert [("&page=1&" in path, "&page=2&" in path) for path in requested] == [(True, False), (True, False), (False, True)], requested
	assert all(f"created=>{CREATED_AFTER}" in path for path in requested), requested


def test_paging_stops_at_the_page_holding_a_match() -> None:
	pages = [[_run(555)], [_run(999, created_at="2026-09-28T03:40:00Z")]]
	rc, outputs, calls, log = _run_wait_plan(pages, "ai:awaiting-approval")
	assert rc == 0, log
	assert outputs.get("run_id") == "555", outputs
	assert not any("&page=2&" in path for path in _run_pages_requested(calls))


def test_plan_run_for_another_issue_is_never_accepted() -> None:
	pages = [[_run(999, title="[E2E Smoke Test alt-model] update canary (run 1)"), _run(998, conclusion="skipped")]]
	rc, outputs, _calls, log = _run_wait_plan(pages, "ai:awaiting-approval")
	assert rc == 1, log
	assert outputs.get("status") == "run_id_missing", outputs
	assert "run_id" not in outputs, outputs


def test_paging_is_bounded() -> None:
	pages = [_noise_page(1000 + 100 * i) for i in range(12)]
	rc, outputs, calls, log = _run_wait_plan(pages, "ai:awaiting-approval")
	assert rc == 1, log
	assert outputs.get("status") == "run_id_missing", outputs
	requested = _run_pages_requested(calls)
	assert any("&page=10&" in path for path in requested), requested
	assert not any("&page=11&" in path for path in requested), requested
	# One single-page status poll, then one 10-page capture walk: a walk that
	# read every full page up to the cap is not retried (PR #4730 review), and
	# the per-poll cost stays one call (CLAUDE.md §15).
	assert len(requested) == 1 + 10, len(requested)
	assert "&page=1&" in requested[0] and "&page=1&" in requested[1] and "&page=2&" in requested[2], requested[:3]


def test_lookup_returns_1_when_a_later_page_cannot_be_read() -> None:
	# Conformance audit of PR #4730: gh_api_safe_print echoes its
	# "::error::gh api call failed" line to stdout, so a failed read reached
	# the page walk as non-JSON text and ended it as a short page (exit 0)
	# instead of the documented exit 1.
	rc, out, err = _run_latest_scoped_run_field([_noise_page(1000)], fail_page=2)
	assert rc == 1, (rc, out, err)
	assert out == "", out


def test_lookup_returns_1_when_a_page_is_malformed() -> None:
	rc, out, err = _run_latest_scoped_run_field([_noise_page(1000), None])
	assert rc == 1, (rc, out, err)
	assert out == "", out


def test_lookup_exit_codes_for_a_match_a_short_window_and_the_page_cap() -> None:
	assert _run_latest_scoped_run_field([_noise_page(1000), [_run(777)]])[:2] == (0, "777")
	assert _run_latest_scoped_run_field([_noise_page(1000), _noise_page(2000, count=40)])[:2] == (0, "")
	assert _run_latest_scoped_run_field([_noise_page(1000 + 100 * i) for i in range(10)])[:2] == (2, "")


def _completed_plan_page(noise_first_id: int = 3000, extra: list[dict] | None = None) -> list[dict]:
	# A full page 1 whose newest non-skipped Plan run for the issue completed,
	# which sends the step into its "other active Plan runs" check.
	runs = [_run(900, created_at="2026-09-28T03:56:50Z")] + (extra or [])
	return runs + _noise_page(noise_first_id, count=100 - len(runs))


def _active_check_pages_requested(calls: list[list[str]]) -> list[str]:
	# The other-active check reads page 1 without &page= (fetch_plan_runs_json)
	# and later pages with it; the poll and the capture always pass &page=.
	requested = _run_pages_requested(calls)
	return [path for path in requested if "&page=" not in path or not path.split("&page=")[1].startswith("1&")]


def test_other_active_plan_run_past_the_first_page_keeps_the_step_waiting() -> None:
	# PR #4730 review round 2: the real Plan run is older, still active, and
	# sits behind 100 newer runs; a newer non-skipped Plan run has completed.
	pages = [_completed_plan_page(), _noise_page(4000, count=40) + [_run(777, conclusion=None, status="in_progress")]]
	rc, outputs, calls, log = _run_wait_plan(pages, "", labels_later="ai:awaiting-approval", switch_after=2)
	assert rc == 0, log
	assert outputs.get("status") == "success", outputs
	assert "Found 1 other active Plan run(s)" in log, log
	assert "Plan workflow completed but issue lacks expected labels" not in log, log
	assert any("&page=2&" in path for path in _active_check_pages_requested(calls)), _run_pages_requested(calls)


def test_other_active_check_stops_at_a_short_page_and_fails() -> None:
	pages = [_completed_plan_page(), _noise_page(4000, count=40)]
	rc, outputs, calls, log = _run_wait_plan(pages, "")
	assert rc == 1, log
	assert outputs.get("status") == "plan_failed", outputs
	active_check = _active_check_pages_requested(calls)
	assert len(active_check) == 2, active_check
	assert "&page=" not in active_check[0] and "&page=2&" in active_check[1], active_check


def test_other_active_check_is_bounded_by_the_page_cap() -> None:
	pages = [_completed_plan_page()] + [_noise_page(4000 + 100 * i) for i in range(12)]
	rc, outputs, calls, log = _run_wait_plan(pages, "")
	assert rc == 1, log
	assert outputs.get("status") == "plan_failed", outputs
	active_check = _active_check_pages_requested(calls)
	assert len(active_check) == 10, active_check
	assert "&page=10&" in active_check[-1], active_check


def test_other_active_plan_run_on_page_one_reads_one_page() -> None:
	pages = [_completed_plan_page(extra=[_run(901, conclusion=None, status="in_progress")]), _noise_page(4000)]
	rc, outputs, calls, log = _run_wait_plan(pages, "", labels_later="ai:awaiting-approval", switch_after=2)
	assert rc == 0, log
	assert "Found 1 other active Plan run(s)" in log, log
	assert not any("&page=2&" in path for path in _run_pages_requested(calls)), _run_pages_requested(calls)


def test_unreadable_later_page_retries_instead_of_failing() -> None:
	pages = [_completed_plan_page(), None]
	rc, outputs, calls, log = _run_wait_plan(pages, "", labels_later="ai:awaiting-approval", switch_after=2)
	assert rc == 0, log
	assert "Unable to confirm concurrent Plan runs on page 2 yet — retrying" in log, log
	assert "Plan workflow completed but issue lacks expected labels" not in log, log


def test_unreadable_later_page_fails_as_a_stall_after_the_inactivity_limit() -> None:
	# PR #4730 review round 3: the retry `continue`s before the loop's own
	# inactivity check, so it applies the limit itself instead of polling
	# until the job's timeout-minutes. PLAN_PHASE_TIMEOUT=0 makes it due at once.
	pages = [_completed_plan_page(), None]
	rc, outputs, _calls, log = _run_wait_plan(pages, "", plan_phase_timeout="0")
	assert rc == 1, log
	assert outputs.get("status") == "timeout", outputs
	assert "Unable to confirm concurrent Plan runs on page 2 yet" in log, log
	assert "::error::Plan phase stalled — no activity for 0 minutes while concurrent Plan runs could not be confirmed" in log, log


def test_unreadable_first_page_fails_as_a_stall_after_the_inactivity_limit() -> None:
	pages = [_completed_plan_page()]
	rc, outputs, _calls, log = _run_wait_plan(pages, "", plan_phase_timeout="0", unpaged_runs_invalid=True)
	assert rc == 1, log
	assert outputs.get("status") == "timeout", outputs
	assert "Unable to confirm concurrent Plan runs yet" in log, log
	assert "::error::Plan phase stalled — no activity for 0 minutes while concurrent Plan runs could not be confirmed" in log, log


def test_other_active_plan_runs_avoids_inline_fallback_substitution() -> None:
	wf = _read_workflow()

	assert "OTHER_ACTIVE_PLAN_RUNS=$(gh api \"repos/${TEST_REPO}/actions/runs?per_page=50&created=>${{ steps.create-issue.outputs.created_after }}\"" not in wf
	assert "--jq '[.workflow_runs[] | select(.name | test(\"Plan\"; \"i\")) | select(.status == \"in_progress\" or .status == \"queued\")] | length' 2>/dev/null || echo \"0\")" not in wf


def test_plan_runs_payload_is_shape_validated_before_counting() -> None:
	wf = _read_workflow()
	wf_lines = wf.splitlines()
	retry_guard = 'if ! _plan_runs_json_valid "$PLAN_RUNS_JSON"; then'
	retry_message = 'echo "  ⏳ Unable to confirm concurrent Plan runs yet — retrying"'
	sleep_stmt = 'sleep "$POLL_INTERVAL"'

	assert "_plan_runs_json_valid()" in wf
	assert "jq -se 'length == 1 and (.[0] | type == \"object\" and (.workflow_runs | type == \"array\"))'" in wf
	assert "PLAN_RUNS_JSON=$(fetch_plan_runs_json || echo \"\")" in wf
	assert "PLAN_RUNS_JSON='{\"workflow_runs\":[]}'" not in wf
	assert retry_guard in wf
	retry_guard_idx = next(i for i, line in enumerate(wf_lines) if retry_guard in line)
	retry_message_idx = next(i for i, line in enumerate(wf_lines) if i > retry_guard_idx and retry_message in line)
	sleep_idx = next(i for i, line in enumerate(wf_lines) if i > retry_message_idx and sleep_stmt in line)
	continue_idx = next(i for i, line in enumerate(wf_lines) if i > sleep_idx and line.strip() == "continue")
	assert retry_guard_idx < retry_message_idx < sleep_idx < continue_idx
	assert "if _OTHER_ACTIVE_PLAN_RUNS=$(printf '%s' \"$PLAN_RUNS_JSON\"" in wf


def test_other_active_plan_runs_is_numeric_before_arithmetic_comparison() -> None:
	wf = _read_workflow()

	numeric_guard = 'if ! [[ "$OTHER_ACTIVE_PLAN_RUNS" =~ ^[0-9]+$ ]]; then'
	comparison = 'if [ "$OTHER_ACTIVE_PLAN_RUNS" -gt 0 ]; then'

	assert numeric_guard in wf
	assert "OTHER_ACTIVE_PLAN_RUNS=0" in wf
	assert comparison in wf
	assert wf.index(numeric_guard) < wf.index(comparison)


IMPL_CREATED_AFTER_EXPR = "${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}"
APPROVED_AT = "2026-09-29T00:48:40Z"


def _phase_step_script(step_id: str) -> str:
	doc = yaml.safe_load(_read_workflow())
	for step in doc["jobs"]["e2e-smoke-test"]["steps"]:
		if step.get("id") == step_id:
			script = step["run"].replace(IMPL_CREATED_AFTER_EXPR, APPROVED_AT)
			return script.replace("${{ steps.create-issue.outputs.created_after }}", CREATED_AFTER)
	raise AssertionError(f"{step_id} step not found")


def _run_phase_step(step_id: str, pages: list, labels: str = "", pr_number: int | None = None, issue_title: str = ISSUE_TITLE, terminal: bool = False, failed_page: int | None = None, pr_recheck_number: int | None = None, active_run: dict | None = None, later_pages: list | None = None, terminal_timeout: str = "0", labels_later: str | None = None, switch_after: int = 0, jobs_json: dict | None = None, in_progress_runs: list | None = None, extra_env: dict[str, str] | None = None, comments: list | None = None, comments_fail: bool = False, post_created_at: str | None = None, window_created_after: str = APPROVED_AT) -> tuple[int, dict[str, str], list[list[str]], str]:
	# Runs the real wait-clarify / wait-implement script against the stub gh
	# and returns its exit code, GITHUB_OUTPUT, gh calls, and combined output.
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		gh = bin_dir / "gh"
		gh.write_text(STUB_GH, encoding="utf-8")
		gh.chmod(0o755)
		sleep = bin_dir / "sleep"
		sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
		sleep.chmod(0o755)
		if terminal:
			# Advance past the real 60-second grace period without waiting.
			fake_date = bin_dir / "date"
			fake_date.write_text('#!/bin/sh\nif [ "$1" != "+%s" ]; then exec /bin/date "$@"; fi\nn=$(cat "$STUB_DATE_COUNT" 2>/dev/null || echo 0)\necho "$((n + 1))" > "$STUB_DATE_COUNT"\nif [ "$n" -lt 2 ]; then echo 0; else echo "$((70 + (n - 2) * 120))"; fi\n', encoding="utf-8")
			fake_date.chmod(0o755)
		pages_file = tmp_path / "pages.json"
		pages_file.write_text(json.dumps(pages), encoding="utf-8")
		if later_pages is not None:
			later_file = tmp_path / "later_pages.json"
			later_file.write_text(json.dumps(later_pages), encoding="utf-8")
		log_file = tmp_path / "gh.log"
		log_file.touch()
		output_file = tmp_path / "github_output"
		output_file.touch()
		env = dict(os.environ)
		env.update({
			"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			"TEST_REPO": "owner/repo",
			"ISSUE_NUMBER": ISSUE_NUMBER,
			"ISSUE_TITLE": issue_title,
			# wait-implement reads its window start from env: (issue #8376).
			"IMPL_WINDOW_CREATED_AFTER": window_created_after,
			"GITHUB_RUN_ID": "40932430968",
			"PHASE_TIMEOUT": "60",
			"POLL_INTERVAL": "0",
			"GITHUB_OUTPUT": str(output_file),
			"STUB_GH_LOG": str(log_file),
			"STUB_RUN_PAGES": str(pages_file),
			"STUB_LABELS": labels,
		})
		if pr_number is not None:
			env["STUB_PR_NUMBER"] = str(pr_number)
		if pr_recheck_number is not None:
			env["STUB_PR_RECHECK_NUMBER"] = str(pr_recheck_number)
		if failed_page is not None:
			env["STUB_RUN_PAGE_FAILS"] = str(failed_page)
		if active_run is not None:
			env["STUB_ACTIVE_RUN_JSON"] = json.dumps(active_run)
		if terminal:
			env["STUB_DATE_COUNT"] = str(tmp_path / "date_count")
			env["PHASE_TIMEOUT"] = terminal_timeout
		if later_pages is not None:
			env["STUB_RUN_PAGES_LATER"] = str(later_file)
		if labels_later is not None:
			env["STUB_LABELS_LATER"] = labels_later
			env["STUB_LABELS_SWITCH_AFTER"] = str(switch_after)
		if jobs_json is not None:
			env["STUB_JOBS_JSON"] = json.dumps(jobs_json)
		if in_progress_runs is not None:
			env["STUB_IN_PROGRESS_RUNS"] = json.dumps(in_progress_runs)
		if comments is not None:
			env["STUB_COMMENTS"] = json.dumps(comments)
		if comments_fail:
			env["STUB_COMMENTS_FAIL"] = "1"
		if post_created_at is not None:
			env["STUB_POST_CREATED_AT"] = post_created_at
		if extra_env:
			env.update(extra_env)
		proc = subprocess.run(["bash", "-c", _phase_step_script(step_id)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
		outputs: dict[str, str] = {}
		for line in output_file.read_text(encoding="utf-8").splitlines():
			key, _, value = line.partition("=")
			outputs[key] = value
		calls = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
		return proc.returncode, outputs, calls, proc.stdout + proc.stderr


ALT_TITLE = "[E2E Smoke Test alt-model] update canary (run 1)"


def test_clarify_capture_pages_past_page_one_and_skips_other_issues() -> None:
	# The parallel alt-model job's Clarify run is newer and on page 1; ours is
	# on page 2. Before issue #4723's sibling fix the capture read one page
	# without a title filter and took the other job's run.
	pages = [[_run(501, name="Internal: AI Clarify", title=ALT_TITLE, created_at="2026-09-28T03:49:00Z")] + _noise_page(1000, count=99), [_run(444, name="Internal: AI Clarify")]]
	rc, outputs, calls, log = _run_phase_step("wait-clarify", pages, labels="ai:planning")
	assert rc == 0, log
	assert outputs.get("run_id") == "444", outputs
	assert outputs.get("status") == "success", outputs
	requested = _run_pages_requested(calls)
	assert ["&page=1&" in path for path in requested] == [True, False], requested
	assert "&page=2&" in requested[1] and f"created=>{CREATED_AFTER}" in requested[1], requested


def test_clarify_without_a_captured_run_id_fails_at_clarify_capture() -> None:
	pages = [[_run(501, name="Internal: AI Clarify", title=ALT_TITLE), _run(502, name="Internal: AI Clarify", conclusion="skipped")]]
	rc, outputs, calls, log = _run_phase_step("wait-clarify", pages, labels="ai:planning")
	assert rc == 1, log
	assert outputs.get("status") == "run_id_missing", outputs
	assert "run_id" not in outputs, outputs
	assert "::error::Issue #4712 completed Clarify but no non-skipped Clarify run titled" in log, log
	# A short window may still be missing a not-yet-indexed run: 5 one-page attempts.
	assert len(_run_pages_requested(calls)) == 5, _run_pages_requested(calls)


def test_implement_capture_finds_our_run_behind_newer_runs() -> None:
	# Run 36504041362: when the PR appeared our Implement run sat at index 137
	# of the window, behind a newer alt-model Implement run and skipped runs.
	pages = [
		[_run(601, name="Internal: AI Implement", title=ALT_TITLE, created_at="2026-09-29T00:49:25Z")] + _noise_page(1000, count=99),
		_noise_page(2000, count=37) + [_run(36504892608, name="Internal: AI Implement", created_at="2026-09-29T00:48:42Z")],
	]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, pr_number=42)
	assert rc == 0, log
	assert outputs.get("run_id") == "36504892608", outputs
	assert outputs.get("pr_number") == "42", outputs
	assert outputs.get("status") == "success", outputs
	requested = _run_pages_requested(calls)
	assert len(requested) == 2 and all(f"created=>{APPROVED_AT}" in path for path in requested), requested


def test_implement_without_a_captured_run_id_fails_at_implement_capture() -> None:
	pages = [[_run(601, name="Internal: AI Implement", title=ALT_TITLE)]]
	rc, outputs, _calls, log = _run_phase_step("wait-implement", pages, pr_number=42)
	assert rc == 1, log
	assert outputs.get("status") == "run_id_missing", outputs
	assert "run_id" not in outputs and "pr_number" not in outputs, outputs
	assert "::error::Issue #4712 completed Implement but no non-skipped Implement run titled" in log, log


def test_implement_capture_is_bounded_and_a_full_walk_is_not_retried() -> None:
	pages = [_noise_page(1000 + 100 * i) for i in range(12)]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, pr_number=42)
	assert rc == 1, log
	assert outputs.get("status") == "run_id_missing", outputs
	requested = _run_pages_requested(calls)
	assert len(requested) == 10, len(requested)
	assert "&page=10&" in requested[-1], requested[-1]


def test_implement_capture_retries_an_unreadable_page() -> None:
	# A failed read is retried (5 attempts), never treated as the end of the window.
	pages = [_noise_page(1000), None]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, pr_number=42)
	assert rc == 1, log
	assert outputs.get("status") == "run_id_missing", outputs
	assert sum("&page=2&" in path for path in _run_pages_requested(calls)) == 5, _run_pages_requested(calls)


def test_clarify_and_implement_fail_before_polling_without_an_issue_title() -> None:
	# PR #5035 review: the captures match runs by display_title, so an empty
	# title fails the step up front, as wait-plan does, instead of polling the
	# phase and then failing at capture (or matching a run with an empty title).
	for step_id, phase, kwargs in (("wait-clarify", "Clarify", {"labels": "ai:planning"}), ("wait-implement", "Implement", {"pr_number": 42})):
		pages = [[_run(444, name=f"Internal: AI {phase}", title="")]]
		rc, outputs, calls, log = _run_phase_step(step_id, pages, issue_title="", **kwargs)
		assert rc == 1, log
		assert outputs.get("status") == "run_id_missing", outputs
		assert "run_id" not in outputs, outputs
		assert f"::error::Missing issue title for {phase} run scoping" in log, log
		assert calls == [], calls


def test_implement_terminal_check_does_not_fail_for_active_issue_run_on_page_two() -> None:
	# Regression: the first 50 runs belong to other issues; our run is still active.
	pages = [[_run(1000 + i, name="Internal: AI Implement", title=ALT_TITLE) for i in range(100)], [_run(777, name="Internal: AI Implement", conclusion=None, status="in_progress")]]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, terminal=True)
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert "All 1 implement workflow run(s) completed but no PR was created" not in log
	assert any("&page=2&" in path for path in _run_pages_requested(calls)), calls
	assert not any("/pulls?" in call[1] for call in calls[1:]), calls


def test_implement_terminal_check_waits_for_older_active_issue_run() -> None:
	# A completed matching run on page one cannot mask another active run.
	pages = [[_run(778, name="Internal: AI Implement")] + [_run(1000 + i, name="Internal: AI Implement", title=ALT_TITLE) for i in range(99)], [_run(777, name="Internal: AI Implement", conclusion=None, status="queued")]]
	rc, outputs, _calls, log = _run_phase_step("wait-implement", pages, terminal=True)
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert "status=implement_failed" not in log


def test_implement_terminal_check_reports_scoped_completed_run() -> None:
	pages = [[_run(99, name="Internal: AI Implement", title=ALT_TITLE), _run(777, name="Internal: AI Implement", conclusion="failure")]]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, terminal=True)
	assert rc == 1 and outputs.get("status") == "implement_failed", (outputs, log)
	assert "All 1 implement workflow run(s) completed but no PR was created (issue run_id=777 conclusion=failure)" in log
	assert len(_run_pages_requested(calls)) == 3, calls  # progress + two scoped confirmations


def test_implement_terminal_check_ignores_other_titles_and_skipped_runs() -> None:
	pages = [[_run(99, name="Internal: AI Implement", title=ALT_TITLE), _run(777, name="Internal: AI Implement", conclusion="skipped")]]
	rc, outputs, _calls, log = _run_phase_step("wait-implement", pages, terminal=True)
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert "status=implement_failed" not in log


def test_implement_terminal_check_treats_unreadable_and_malformed_pages_as_unknown() -> None:
	# A matching completed run on page 1 stops the progress counter there, so
	# the terminal walk itself must reach the bad page 2 and fail closed.
	first_page = [_run(778, name="Internal: AI Implement", conclusion="failure")] + [_run(1000 + i, name="Internal: AI Implement", title=ALT_TITLE) for i in range(99)]
	for second_page, failed_page in (([], 2), (None, None), ([_run(777, name="Internal: AI Implement", conclusion="failure", status="bad_status")], None), ([_run(777, name="Internal: AI Implement", conclusion="failure") | {"id": "777"}], None), ([_run(777, name="Internal: AI Implement", conclusion="\n::error::injected")], None)):
		rc, outputs, calls, log = _run_phase_step("wait-implement", [first_page, second_page], terminal=True, failed_page=failed_page)
		assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
		assert any("&page=2&" in path for path in _run_pages_requested(calls)), calls


def test_implement_terminal_check_treats_full_tenth_page_as_unknown() -> None:
	pages = [[_run(777, name="Internal: AI Implement", conclusion="failure")] + _noise_page(1000, 99)] + [_noise_page(2000 + 100 * i) for i in range(9)]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, terminal=True)
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert any("&page=10&" in path for path in _run_pages_requested(calls)), calls
	assert not any("&page=11&" in path for path in _run_pages_requested(calls)), calls


def test_implement_terminal_check_rechecks_newly_indexed_active_run() -> None:
	pages = [[_run(777, name="Internal: AI Implement", conclusion="failure")]]
	later = [[_run(778, name="Internal: AI Implement", conclusion=None, status="in_progress"), *pages[0]]]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, terminal=True, later_pages=later)
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert len(_poll_run_pages_requested(calls)) == 3, calls


def test_implement_terminal_check_reuses_known_active_run_id() -> None:
	# The progress counter stops at page 1 (our completed run 778), so the
	# terminal walk runs, finds active 777 on page 2, and the next poll checks
	# 777 by ID instead of walking page 2 again.
	active = _run(777, name="Internal: AI Implement", conclusion=None, status="in_progress", created_at="2026-09-28T03:49:00Z")
	pages = [[_run(778, name="Internal: AI Implement")] + [_run(1000 + i, name="Internal: AI Implement", title=ALT_TITLE) for i in range(99)], [active]]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, terminal=True, terminal_timeout="2", active_run=active)
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert any(call[1].endswith("/actions/runs/777") for call in calls), calls
	assert sum("&page=2&" in path for path in _run_pages_requested(calls)) == 1, calls


def test_implement_progress_counter_finds_issue_run_behind_full_noise_page() -> None:
	# Issue #7968 (run 39926633796): the old counter read one unscoped 50-run
	# page, never saw this issue's Implement run, and ended as a 30-minute
	# timeout instead of implement_failed with the run ID.
	pages = [_noise_page(1000, 100), [_run(777, name="Internal: AI Implement", conclusion="failure")]]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, terminal=True)
	assert rc == 1 and outputs.get("status") == "implement_failed", (outputs, log)
	assert "issue run_id=777 conclusion=failure" in log, log


def test_implement_progress_counter_ignores_other_issue_runs() -> None:
	pages = [[_run(601, name="Internal: AI Implement", title=ALT_TITLE, conclusion="failure")]]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, terminal=True, terminal_timeout="2")
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert "impl_runs: 0 total" in log, log
	# Only the counter's page-1 read on each of the two polls; the terminal walk never runs.
	assert len(_poll_run_pages_requested(calls)) == 2, _run_pages_requested(calls)
	assert all("&page=1&" in path for path in _poll_run_pages_requested(calls)), _run_pages_requested(calls)


def test_implement_progress_counter_logs_return_to_awaiting_approval() -> None:
	pages = [[_run(777, name="Internal: AI Implement", conclusion=None, status="in_progress")]]
	rc, outputs, _calls, log = _run_phase_step("wait-implement", pages, terminal=True, terminal_timeout="2", labels="ai:implementing", labels_later="ai:awaiting-approval", switch_after=1)
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert "returned to ai:awaiting-approval — latest scoped Implement run: id=777 status=in_progress conclusion=unknown" in log, log


def test_implement_progress_counter_uses_issue_scoped_paginated_reads() -> None:
	script = _phase_step_script("wait-implement")
	assert "actions/runs?per_page=50&created=>${IMPL_CREATED_AFTER}" not in script
	counter = script[script.index("scoped_implement_progress_counts() {"):script.index("IMPL_PREV_LABELS=\"\"")]
	assert "actions/runs?per_page=100&page=${progress_page}&created=>${IMPL_CREATED_AFTER}" in counter
	assert "select(.display_title == $title)" in counter
	assert "SCOPED_RUN_LOOKUP_MAX_PAGES" in counter
	helper = script[script.index("scoped_implement_runs_complete() {"):script.index("capture_run_id() {")]
	assert "select(.display_title == $title)" in helper


def test_implement_terminal_check_preserves_pr_recheck() -> None:
	rc, outputs, calls, log = _run_phase_step("wait-implement", [[_run(777, name="Internal: AI Implement")]], terminal=True, pr_recheck_number=42)
	assert rc == 0 and outputs.get("status") == "success", (outputs, log)
	assert outputs.get("pr_number") == "42", outputs
	assert len(_run_pages_requested(calls)) >= 2, calls


STALL_DIAG_CALL = '( log_implement_stall_diagnostics ) || echo "  stall-diag: diagnostics failed (ignored)"'


def _pending_implement_run(run_id: int = 777) -> dict:
	return _run(run_id, name="Internal: AI Implement", conclusion=None, status="pending")


def test_implement_stall_logs_pending_run_diagnostics() -> None:
	# Issue #8376 (run 40876853022): the only scoped Implement run stayed
	# pending for 30 minutes while the issue carried ai:implementing.
	pending = _pending_implement_run()
	active = pending | {"run_started_at": None, "event": "issue_comment", "updated_at": "2026-09-29T00:49:00Z", "run_attempt": 1}
	jobs = {"jobs": [{"name": "implement", "status": "queued", "conclusion": None, "started_at": None, "runner_name": None, "steps": []}]}
	holder = _run(700, name="Internal: AI Implement", conclusion=None, status="in_progress", created_at="2026-09-29T00:40:00Z") | {"run_started_at": "2026-09-29T00:40:05Z", "event": "issue_comment"}
	rc, outputs, calls, log = _run_phase_step("wait-implement", [[pending]], labels="ai:implementing", terminal=True, terminal_timeout="2", active_run=active, jobs_json=jobs, in_progress_runs=[holder, pending])
	assert rc == 1, log
	assert outputs == {"status": "timeout"}, outputs
	assert "::error::Implement phase stalled — no activity for 2 minutes" in log, log
	assert 'stall-diag latest-run: id=777 name="Internal: AI Implement" event="issue_comment" status="pending"' in log, log
	assert "run_started_at=null" in log, log
	assert 'stall-diag job: name="implement" status="queued" conclusion=null started_at=null' in log, log
	assert "stall-diag window-run: none" in log, log
	assert "stall-diag in-progress-run: id=700 title_matches=true" in log, log
	assert "in-progress-run: id=777" not in log, log
	assert "::warning::Issue #4712 carries ai:implementing but its latest scoped Implement run 777 is still pending at the stall" in log, log
	assert any(call[1].endswith("/actions/runs/777") for call in calls), calls
	assert any(call[1].endswith("/actions/runs/777/jobs?per_page=30") for call in calls), calls
	diag = _stall_diag_reads(calls)
	assert len(diag) == 2, diag
	assert f"created=>{APPROVED_AT}" in diag[0] and "status=in_progress" in diag[1], diag


def test_implement_stall_diagnostics_are_fail_open() -> None:
	# Unreadable by-ID payload and a malformed in-progress list.
	rc, outputs, _calls, log = _run_phase_step("wait-implement", [[_pending_implement_run()]], labels="ai:implementing", terminal=True, terminal_timeout="2", in_progress_runs=[1, 2])
	assert rc == 1, log
	assert outputs == {"status": "timeout"}, outputs
	assert "stall-diag latest-run: unavailable" in log, log
	assert "stall-diag jobs: none" in log, log
	assert "stall-diag in-progress-run: unavailable" in log, log
	# No scoped run at all: the by-ID reads are skipped, the list reads still run.
	rc, outputs, calls, log = _run_phase_step("wait-implement", [[]], terminal=True, terminal_timeout="2")
	assert rc == 1, log
	assert outputs == {"status": "timeout"}, outputs
	assert "stall-diag latest-run: none (no scoped Implement run in window)" in log, log
	assert "::warning::" not in log, log
	assert not any(re.search(r"/actions/runs/\d+", call[1]) for call in calls), calls
	assert len(_stall_diag_reads(calls)) == 2, calls


def test_implement_stall_diagnostics_only_on_stall_path() -> None:
	rc, outputs, calls, log = _run_phase_step("wait-implement", [[_run(777, name="Internal: AI Implement")]], pr_number=42)
	assert rc == 0 and outputs.get("status") == "success", (outputs, log)
	assert "stall-diag" not in log, log
	assert not any("status=in_progress" in call[1] for call in calls), calls
	pages = [[_run(99, name="Internal: AI Implement", title=ALT_TITLE), _run(777, name="Internal: AI Implement", conclusion="failure")]]
	rc, outputs, _calls, log = _run_phase_step("wait-implement", pages, terminal=True)
	assert rc == 1 and outputs.get("status") == "implement_failed", (outputs, log)
	assert "stall-diag" not in log, log
	script = _phase_step_script("wait-implement")
	assert "INACTIVITY_LIMIT=$((PHASE_TIMEOUT * 60))" in script
	assert script.count(STALL_DIAG_CALL) == 1
	error_at = script.index('echo "::error::Implement phase stalled — no activity for ${EFFECTIVE_LIMIT_MINUTES} minutes${LIMIT_SUFFIX}"')
	status_at = script.index('echo "status=timeout" >> "$GITHUB_OUTPUT"', error_at)
	call_at = script.index(STALL_DIAG_CALL)
	assert error_at < status_at < call_at < script.index("exit 1", status_at)
	assert script[call_at + len(STALL_DIAG_CALL):script.index("exit 1", status_at)].strip() == ""


def test_implement_stall_diagnostics_do_not_inject_workflow_commands() -> None:
	hostile = _run(700, name="Internal: AI Implement", title="x\n::error::injected\n##[error]legacy", conclusion=None, status="in_progress")
	rc, outputs, _calls, log = _run_phase_step("wait-implement", [[_pending_implement_run()]], labels="ai:implementing", terminal=True, terminal_timeout="2", in_progress_runs=[hostile])
	assert rc == 1 and outputs == {"status": "timeout"}, (outputs, log)
	assert "stall-diag in-progress-run: id=700 title_matches=false" in log, log
	assert "::error::injected" in log, log  # present only escaped inside the JSON string
	for line in log.splitlines():
		assert not line.lstrip().startswith("::error::injected"), line
		assert not line.lstrip().startswith("##[error]legacy"), line


def test_implement_stall_warning_uses_fresh_run_status() -> None:
	# The by-ID read overrides the 30s-cached status for the warning.
	started = _pending_implement_run() | {"status": "in_progress", "run_started_at": "2026-09-29T00:49:30Z"}
	rc, outputs, _calls, log = _run_phase_step("wait-implement", [[_pending_implement_run()]], labels="ai:implementing", terminal=True, terminal_timeout="2", active_run=started)
	assert rc == 1 and outputs == {"status": "timeout"}, (outputs, log)
	assert 'stall-diag latest-run: id=777 name="Internal: AI Implement"' in log and 'status="in_progress"' in log, log
	assert "::warning::" not in log, log
	# The reverse: a stale cached in_progress and a fresh pending still warns.
	cached = _run(777, name="Internal: AI Implement", conclusion=None, status="in_progress")
	rc, outputs, _calls, log = _run_phase_step("wait-implement", [[cached]], labels="ai:implementing", terminal=True, terminal_timeout="2", active_run=_pending_implement_run())
	assert rc == 1 and outputs == {"status": "timeout"}, (outputs, log)
	assert "::warning::Issue #4712 carries ai:implementing but its latest scoped Implement run 777 is still pending at the stall" in log, log


def test_implement_window_start_comes_from_step_env() -> None:
	doc = yaml.safe_load(_read_workflow())
	step = next(s for s in doc["jobs"]["e2e-smoke-test"]["steps"] if s.get("id") == "wait-implement")
	assert step["env"]["IMPL_WINDOW_CREATED_AFTER"] == IMPL_CREATED_AFTER_EXPR, step.get("env")
	assert 'IMPL_CREATED_AFTER="${IMPL_WINDOW_CREATED_AFTER:-}"' in step["run"]
	assert IMPL_CREATED_AFTER_EXPR not in step["run"]


def test_implement_stall_diagnostics_mark_truncated_run_lists() -> None:
	holders = [_run(1000 + n, name="Internal: AI Implement", conclusion=None, status="in_progress") for n in range(22)]
	rc, outputs, _calls, log = _run_phase_step("wait-implement", [[_pending_implement_run()]], labels="ai:implementing", terminal=True, terminal_timeout="2", in_progress_runs=holders)
	assert rc == 1 and outputs == {"status": "timeout"}, (outputs, log)
	assert log.count("stall-diag in-progress-run: id=") == 20, log
	assert "stall-diag in-progress-run: truncated, 2 more" in log, log


def test_implement_stall_window_run_notes_a_full_page() -> None:
	# The window-run read is one page of 100 runs; a full page is noted.
	pages = [[_pending_implement_run()] + _noise_page(1000, count=99)]
	rc, outputs, _calls, log = _run_phase_step("wait-implement", pages, labels="ai:implementing", terminal=True, terminal_timeout="2")
	assert rc == 1 and outputs == {"status": "timeout"}, (outputs, log)
	assert "stall-diag window-run: note: only the newest 100 runs in the window were read" in log, log
	rc, outputs, _calls, log = _run_phase_step("wait-implement", [[_pending_implement_run()]], labels="ai:implementing", terminal=True, terminal_timeout="2")
	assert "stall-diag window-run: note:" not in log, log


# --- Backport of #8243 (working-run tracking) and #8259 (pre-push gate step
# budget) from main (issue #8376, release run 40876853022). ------------------


def _jobs_lookup_run_ids(calls: list[list[str]]) -> set[str]:
	# Only the per-poll step-name read (per_page=10); the stall diagnostics
	# read the latest run's jobs with per_page=30.
	ids = set()
	for call in calls:
		path = call[1] if len(call) > 1 else ""
		if "/actions/runs/" in path and "/jobs?per_page=10" in path:
			ids.add(path.split("/actions/runs/")[1].split("/jobs?")[0])
	return ids


def test_implement_step_lookup_uses_in_progress_run_behind_newer_pending_run() -> None:
	# Issue #8241 (run 40608957733): a second Implement run held pending by the
	# ai-implement-<repo>-<issue> concurrency group became the newest run; the
	# step-name lookup read its (empty) jobs and the gate declared a stall.
	pages = [[
		_run(777, name="Internal: AI Implement", conclusion=None, status="in_progress", created_at="2026-09-28T03:50:56Z"),
		_run(778, name="Internal: AI Implement", conclusion=None, status="pending", created_at="2026-09-28T03:55:00Z"),
	]]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, terminal=True, terminal_timeout="2")
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert _jobs_lookup_run_ids(calls) == {"777"}, calls
	assert "2 total, 2 active, latest: pending" in log, log


def test_implement_step_lookup_falls_back_to_latest_run_when_none_in_progress() -> None:
	pages = [[_run(778, name="Internal: AI Implement", conclusion=None, status="pending")]]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, terminal=True, terminal_timeout="2")
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert _jobs_lookup_run_ids(calls) == {"778"}, calls
	assert not any("&page=2&" in path for path in _run_pages_requested(calls)), calls


def test_implement_step_lookup_finds_in_progress_run_on_later_page() -> None:
	# The counter stops at the first matching page, so a pending peer on a
	# full page 1 must not hide the in_progress run on page 2.
	pending = _run(778, name="Internal: AI Implement", conclusion=None, status="pending", created_at="2026-09-28T03:55:00Z")
	working = _run(777, name="Internal: AI Implement", conclusion=None, status="in_progress", created_at="2026-09-28T03:50:56Z")
	pages = [[pending] + _noise_page(1000, 99), [working]]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, terminal=True, terminal_timeout="2")
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert _jobs_lookup_run_ids(calls) == {"777"}, calls
	assert "1 total, 1 active, latest: pending" in log, log


def test_implement_activity_state_tracks_working_run() -> None:
	script = _phase_step_script("wait-implement")
	counter = script[script.index("scoped_implement_progress_counts() {"):script.index("IMPL_PREV_LABELS=\"\"")]
	assert 'select(.status == "in_progress")' in counter
	assert 'IMPL_RUN_ID_WORKING="$IMPL_RUN_ID_LATEST"' in counter
	assert "actions/runs/${IMPL_RUN_ID_WORKING}/jobs?per_page=10" in script
	assert "actions/runs/${IMPL_RUN_ID_LATEST}/jobs?per_page=10" not in script
	cur_state = next(line for line in script.splitlines() if line.strip().startswith("CUR_STATE="))
	assert "working=${IMPL_RUN_ID_WORKING}" in cur_state, cur_state
	assert "PHASE_TIMEOUT" in script
	assert "Implement phase stalled" in script


PREPUSH_GATE_STEP = "Pre-push affected-test gate"


def _in_progress_step_jobs(step_name: str) -> dict:
	return {"jobs": [{"steps": [
		{"name": "Set up job", "status": "completed"},
		{"name": step_name, "status": "in_progress"},
	]}]}


def _working_implement_pages() -> list:
	return [[_run(777, name="Internal: AI Implement", conclusion=None, status="in_progress", created_at="2026-09-28T03:50:56Z")]]


def test_implement_prepush_gate_step_gets_its_own_stall_limit() -> None:
	# Issue #8244 (run 40610855593): one 31-minute "Pre-push affected-test
	# gate" step outlived the 30-minute Phase 3b inactivity limit. The fake
	# clock makes IDLE 0, 120, 240 seconds; the normal limit is 60 s, the
	# gate step's is 180 s, so the wait survives idle 120 and stalls at 240.
	rc, outputs, calls, log = _run_phase_step(
		"wait-implement", _working_implement_pages(), terminal=True, terminal_timeout="1",
		jobs_json=_in_progress_step_jobs(PREPUSH_GATE_STEP),
		extra_env={"PREPUSH_GATE_STEP_STALL_MINUTES": "3"},
	)
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert "idle 120s (timeout in ~1m)" in log, log
	assert "(pre-push gate step: limit 3m)" in log.split("idle 120s", 1)[1].splitlines()[0], log
	assert "Implement phase stalled — no activity for 3 minutes (pre-push gate step: limit 3m)" in log, log
	assert "idle 240s" not in log, log


def test_implement_other_steps_keep_the_normal_stall_limit() -> None:
	rc, outputs, calls, log = _run_phase_step(
		"wait-implement", _working_implement_pages(), terminal=True, terminal_timeout="1",
		jobs_json=_in_progress_step_jobs("Run Codex implementation"),
		extra_env={"PREPUSH_GATE_STEP_STALL_MINUTES": "3"},
	)
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert "::error::Implement phase stalled — no activity for 1 minutes\n" in log + "\n", log
	assert "pre-push gate step" not in log, log
	assert "idle 120s" not in log, log


def test_implement_prepush_gate_limit_never_shortens_the_normal_limit() -> None:
	# An extended value below PHASE_TIMEOUT leaves the normal limit in place.
	rc, outputs, calls, log = _run_phase_step(
		"wait-implement", _working_implement_pages(), terminal=True, terminal_timeout="2",
		jobs_json=_in_progress_step_jobs(PREPUSH_GATE_STEP),
		extra_env={"PREPUSH_GATE_STEP_STALL_MINUTES": "1"},
	)
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert "Implement phase stalled — no activity for 2 minutes" in log, log
	assert "pre-push gate step" not in log, log


def test_implement_prepush_gate_limit_rejects_a_non_numeric_value() -> None:
	# A non-numeric override falls back to PHASE_TIMEOUT without an arithmetic error.
	rc, outputs, calls, log = _run_phase_step(
		"wait-implement", _working_implement_pages(), terminal=True, terminal_timeout="1",
		jobs_json=_in_progress_step_jobs(PREPUSH_GATE_STEP),
		extra_env={"PREPUSH_GATE_STEP_STALL_MINUTES": "a[$(touch /tmp/x)]"},
	)
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert "Implement phase stalled — no activity for 1 minutes" in log, log
	assert "pre-push gate step" not in log, log
	assert "syntax error" not in log and "unbound variable" not in log, log


def test_implement_prepush_gate_limit_keys_on_the_working_run_step_only() -> None:
	script = _phase_step_script("wait-implement")
	assert 'PREPUSH_GATE_STEP_NAME="Pre-push affected-test gate"' in script
	assert 'PREPUSH_GATE_STEP_STALL_MINUTES="${PREPUSH_GATE_STEP_STALL_MINUTES:-35}"' in script
	assert 'if [ "$IMPL_STEP_NAME" = "$PREPUSH_GATE_STEP_NAME" ] && [ "$PREPUSH_GATE_STALL_LIMIT" -gt "$INACTIVITY_LIMIT" ]; then' in script
	assert 'if [ "$IDLE" -ge "$EFFECTIVE_LIMIT" ]; then' in script
	assert "actions/runs/${IMPL_RUN_ID_WORKING}/jobs?per_page=10" in script
	cur_state = next(line for line in script.splitlines() if line.strip().startswith("CUR_STATE="))
	assert cur_state.strip() == 'CUR_STATE="labels=${LABELS};impl_run=${IMPL_RUN_STATUS};total=${IMPL_RUN_TOTAL};active=${IMPL_RUN_IN_PROGRESS};step=${IMPL_STEP_NAME};head=${COMMIT_COUNT};working=${IMPL_RUN_ID_WORKING}"', cur_state
	assert "INACTIVITY_LIMIT=$((PHASE_TIMEOUT * 60))" in script
	# The job env default matches main's (35 minutes).
	doc = yaml.safe_load(_read_workflow())
	assert doc["jobs"]["e2e-smoke-test"]["env"]["PREPUSH_GATE_STEP_STALL_MINUTES"] == 35
	# The literal must match the step implement.yml runs, or the extended
	# limit silently stops applying (skipped when the sandbox hides the file).
	# stable's implement.yml does not carry the step yet (it ships from main,
	# issue #8376), so only check uniqueness when the step is present.
	implement_path = REPO_ROOT / ".github" / "workflows" / "implement.yml"
	if implement_path.is_file():
		assert implement_path.read_text(encoding="utf-8").count(f"- name: {PREPUSH_GATE_STEP}\n") in (0, 1)


def test_implement_replay_40876853022_working_run_through_prepush_gate() -> None:
	# Issue #8376 replay (scoped per the maintainer note): the working run 777
	# is in the window and in_progress while a newer duplicate 778 is pending.
	# Phase 3b reads 777's steps; the 30-minute guard does not trip while its
	# "Pre-push affected-test gate" step runs (30m28s in the incident); the
	# 35-minute gate limit still ends a step that never finishes. The fake
	# clock advances IDLE by 120 s per poll after the first two.
	pages = [[
		_run(777, name="Internal: AI Implement", conclusion=None, status="in_progress", created_at="2026-09-28T03:50:56Z"),
		_run(778, name="Internal: AI Implement", conclusion=None, status="pending", created_at="2026-09-28T03:51:43Z"),
	]]
	rc, outputs, calls, log = _run_phase_step(
		"wait-implement", pages, labels="ai:implementing", terminal=True, terminal_timeout="30",
		jobs_json=_in_progress_step_jobs(PREPUSH_GATE_STEP),
	)
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert _jobs_lookup_run_ids(calls) == {"777"}, calls
	# Past the 30-minute guard (1800 s) and past the incident's 1828 s step.
	assert "idle 1920s" in log, log
	assert "Implement phase stalled — no activity for 30 minutes\n" not in log + "\n", log
	assert "Implement phase stalled — no activity for 35 minutes (pre-push gate step: limit 35m)" in log, log
	assert "stall-diag latest-run:" in log, log

# --- Issue #8391 (run 40932430968): Phase 3a starts Phase 3b's window at the
# earliest trusted /approved, the plan's auto-approval included. ------------

PLAN_AUTO_AT = "2026-10-29T19:01:15Z"
E2E_POSTED_AT = "2026-10-29T19:02:06Z"


def _approval_comment(created_at: str, body: str = "/approved [auto-approved-by-plan]\n\nAuto approval.", association: str = "OWNER", user_type: str = "User") -> dict:
	return {"id": 1, "body": body, "created_at": created_at, "author_association": association, "user": {"login": "owner", "type": user_type}}


def _approve_posts(calls: list[list[str]]) -> list[list[str]]:
	return [call for call in calls if "/comments" in call[1] and "-f" in call]


def _approve_reads(calls: list[list[str]]) -> list[list[str]]:
	return [call for call in calls if call[1].endswith("/comments?per_page=100")]


def test_approve_window_starts_at_earlier_plan_auto_approval() -> None:
	comments = [_approval_comment(PLAN_AUTO_AT), _approval_comment(E2E_POSTED_AT, body="/approved\n\nE2E smoke test auto-approval (run 40932430968).")]
	rc, outputs, calls, log = _run_phase_step("approve", [[]], comments=comments, post_created_at=E2E_POSTED_AT)
	assert rc == 0, log
	assert len(_approve_posts(calls)) == 1, calls
	assert outputs.get("approved_at") == PLAN_AUTO_AT, outputs
	assert outputs.get("approval_source") == "plan_auto", outputs
	assert f"approval_source=plan_auto approved_at={PLAN_AUTO_AT} posted_at={E2E_POSTED_AT}" in log, log


def test_approve_without_prior_approval_uses_posted_timestamp() -> None:
	comments = [_approval_comment(E2E_POSTED_AT, body="/approved\n\nE2E smoke test auto-approval.")]
	rc, outputs, calls, log = _run_phase_step("approve", [[]], comments=comments, post_created_at=E2E_POSTED_AT)
	assert rc == 0, log
	assert len(_approve_posts(calls)) == 1, calls
	assert outputs.get("approved_at") == E2E_POSTED_AT, outputs
	assert outputs.get("approval_source") == "e2e", outputs


def test_approve_ignores_untrusted_and_malformed_approvals() -> None:
	comments = [
		_approval_comment("2026-10-29T18:50:00Z", body="/approved", association="NONE"),
		_approval_comment("2026-10-29T18:51:00Z", body="/approved", user_type="Bot"),
		_approval_comment(PLAN_AUTO_AT + "\nx=y"),
		_approval_comment("2026-10-29T18:52:00Z", body="please /approved"),
	]
	rc, outputs, _calls, log = _run_phase_step("approve", [[]], comments=comments, post_created_at=E2E_POSTED_AT)
	assert rc == 0, log
	assert outputs == {"approved_at": E2E_POSTED_AT, "approval_source": "e2e"}, outputs


def test_approve_comment_read_failure_fails_open() -> None:
	rc, outputs, calls, log = _run_phase_step("approve", [[]], comments_fail=True, post_created_at=E2E_POSTED_AT)
	assert rc == 0, log
	assert outputs == {"approved_at": E2E_POSTED_AT, "approval_source": "e2e"}, outputs
	assert len(_approve_reads(calls)) == 3, calls


def test_approve_malformed_post_timestamp_falls_back_to_the_clock() -> None:
	rc, outputs, _calls, log = _run_phase_step("approve", [[]], comments=[_approval_comment(PLAN_AUTO_AT)], post_created_at="not-a-time")
	assert rc == 0, log
	assert re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", outputs.get("approved_at", "")), outputs
	# The real clock is later than the plan approval, so the plan's comment wins.
	assert outputs.get("approved_at") == PLAN_AUTO_AT and outputs.get("approval_source") == "plan_auto", outputs


def test_replay_run_40932430968_window_includes_plan_approved_working_run() -> None:
	# Plan auto-approval 19:01:15, working run 40936238289 created 19:01:20,
	# Phase 3a /approved 19:02:06, pending duplicate 40936365554 created 19:02:09.
	# The working run's "Pre-push affected-test gate" step ran 30m42s.
	rc, outputs, _calls, log = _run_phase_step("approve", [[]], comments=[_approval_comment(PLAN_AUTO_AT)], post_created_at=E2E_POSTED_AT)
	assert rc == 0 and outputs.get("approved_at") == PLAN_AUTO_AT, (outputs, log)
	pages = [[
		_run(40936365554, name="Internal: AI Implement", conclusion=None, status="pending", created_at="2026-10-29T19:02:09Z"),
		_run(40936238289, name="Internal: AI Implement", conclusion=None, status="in_progress", created_at="2026-10-29T19:01:20Z"),
	]]
	rc, outputs, calls, log = _run_phase_step(
		"wait-implement", pages, labels="ai:implementing", terminal=True, terminal_timeout="30",
		jobs_json=_in_progress_step_jobs(PREPUSH_GATE_STEP), window_created_after=outputs["approved_at"],
	)
	assert all(f"created=>{PLAN_AUTO_AT}" in path for path in _poll_run_pages_requested(calls)), calls
	assert _jobs_lookup_run_ids(calls) == {"40936238289"}, calls
	# Past 30 minutes (and the 30m42s gate step) without the plain stall firing.
	assert "idle 1920s" in log, log
	assert "Implement phase stalled — no activity for 30 minutes\n" not in log + "\n", log
	assert rc == 1 and outputs.get("status") == "timeout", (outputs, log)
	assert "(pre-push gate step: limit 35m)" in log, log


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
