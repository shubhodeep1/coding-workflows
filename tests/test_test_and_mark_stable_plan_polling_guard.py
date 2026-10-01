#!/usr/bin/env python3
"""Regression checks for plan-run polling guard in test-and-mark-stable workflow."""

from __future__ import annotations

import json
import os
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


def pull_reads():
	# wait-implement's PR lookups so far, this call included: the clock for
	# STUB_PR_NUMBER_AFTER and STUB_RUN_PAGES_SWITCH_AFTER.
	with open(os.environ["STUB_GH_LOG"], encoding="utf-8") as log:
		return sum(1 for line in log if "/pulls?" in json.loads(line)[1])


def run_pages():
	# STUB_RUN_PAGES until STUB_RUN_PAGES_SWITCH_AFTER PR lookups, then STUB_RUN_PAGES_LATER.
	pages_path = os.environ["STUB_RUN_PAGES"]
	if os.environ.get("STUB_RUN_PAGES_SWITCH_AFTER") and pull_reads() > int(os.environ["STUB_RUN_PAGES_SWITCH_AFTER"]):
		pages_path = os.environ["STUB_RUN_PAGES_LATER"]
	return json.loads(open(pages_path, encoding="utf-8").read())


if path == issue_path:
	# STUB_LABELS until STUB_LABELS_SWITCH_AFTER issue reads, then STUB_LABELS_LATER.
	with open(os.environ["STUB_GH_LOG"], encoding="utf-8") as log:
		issue_reads = sum(1 for line in log if json.loads(line)[1:2] == [issue_path])
	labels = os.environ["STUB_LABELS"]
	if os.environ.get("STUB_LABELS_SWITCH_AFTER") and issue_reads > int(os.environ["STUB_LABELS_SWITCH_AFTER"]):
		labels = os.environ["STUB_LABELS_LATER"]
	payload = {"labels": [{"name": name} for name in labels.split(",") if name]}
elif path.startswith(issue_path + "/comments"):
	payload = []
elif "/pulls?" in path:
	# wait-implement's PR lookup: one open PR when STUB_PR_NUMBER is set,
	# and only after STUB_PR_NUMBER_AFTER lookups when that is set too.
	payload = [{"number": int(os.environ["STUB_PR_NUMBER"])}] if os.environ.get("STUB_PR_NUMBER") else []
	if os.environ.get("STUB_PR_NUMBER_AFTER") and pull_reads() <= int(os.environ["STUB_PR_NUMBER_AFTER"]):
		payload = []
elif "/actions/runs?" in path and os.environ.get("STUB_UNPAGED_RUNS_INVALID") and "&page=" not in path:
	# The other-active check's page-1 read (fetch_plan_runs_json) is the only
	# runs request without &page=; this makes just that read unreadable.
	payload = {"workflow_runs": None}
elif "/actions/runs?" in path:
	pages = run_pages()
	match = re.search(r"[?&]page=(\d+)", path)
	page = int(match.group(1)) if match else 1
	if os.environ.get("STUB_RUN_PAGE_FAILS") == str(page):
		# A failed read the way gh reports one: a message on stderr, exit 1.
		sys.stderr.write("HTTP 502: Bad Gateway\n")
		sys.exit(1)
	payload = {"workflow_runs": pages[page - 1] if page <= len(pages) else []}
elif re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/\d+", path):
	# wait-implement's cached run read: the run as the current pages show it,
	# or a failed read when STUB_RUN_BY_ID_FAILS is set.
	if os.environ.get("STUB_RUN_BY_ID_FAILS"):
		sys.stderr.write("HTTP 502: Bad Gateway\n")
		sys.exit(1)
	run_id = int(path.rsplit("/", 1)[1])
	found = [run for page_runs in run_pages() for run in (page_runs or []) if run.get("id") == run_id]
	if not found:
		sys.stderr.write("HTTP 404: Not Found\n")
		sys.exit(1)
	payload = found[0]
elif re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/\d+/jobs\?.*", path):
	payload = {"jobs": []}
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


# Stub `date` for wait-implement tests: `date +%s` returns a clock that
# advances 100 s per call (counter in STUB_DATE_COUNTER); anything else goes
# to the real date.
STUB_DATE = r'''#!/usr/bin/env python3
import os, sys
if sys.argv[1:] == ["+%s"]:
	counter = os.environ["STUB_DATE_COUNTER"]
	ticks = int(open(counter).read()) if os.path.exists(counter) else 0
	with open(counter, "w") as handle:
		handle.write(str(ticks + 1))
	sys.stdout.write(str(1790000000 + 100 * ticks) + "\n")
	sys.exit(0)
real_date = "/usr/bin/date" if os.path.exists("/usr/bin/date") else "/bin/date"
os.execv(real_date, [real_date] + sys.argv[1:])
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


def _run_phase_step(step_id: str, pages: list, labels: str = "", pr_number: int | None = None, issue_title: str = ISSUE_TITLE, pr_after: int | None = None, pages_later: list | None = None, switch_after: int = 0, fail_page: int | None = None, phase_timeout: str = "60", fake_clock: bool = False, run_by_id_fails: bool = False) -> tuple[int, dict[str, str], list[list[str]], str]:
	# Runs the real wait-clarify / wait-implement script against the stub gh
	# and returns its exit code, GITHUB_OUTPUT, gh calls, and combined output.
	# fake_clock puts a stub `date` on PATH whose `date +%s` advances 100 s
	# per call, so wait-implement's 60-second grace period elapses at once.
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
		if fake_clock:
			date = bin_dir / "date"
			date.write_text(STUB_DATE, encoding="utf-8")
			date.chmod(0o755)
		pages_file = tmp_path / "pages.json"
		pages_file.write_text(json.dumps(pages), encoding="utf-8")
		pages_later_file = tmp_path / "pages_later.json"
		pages_later_file.write_text(json.dumps(pages_later if pages_later is not None else pages), encoding="utf-8")
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
			"PHASE_TIMEOUT": phase_timeout,
			"POLL_INTERVAL": "0",
			"GITHUB_OUTPUT": str(output_file),
			"STUB_GH_LOG": str(log_file),
			"STUB_RUN_PAGES": str(pages_file),
			"STUB_RUN_PAGES_LATER": str(pages_later_file),
			"STUB_DATE_COUNTER": str(tmp_path / "date_counter"),
			"STUB_LABELS": labels,
		})
		if pr_number is not None:
			env["STUB_PR_NUMBER"] = str(pr_number)
		if pr_after is not None:
			env["STUB_PR_NUMBER_AFTER"] = str(pr_after)
		if pages_later is not None:
			env["STUB_RUN_PAGES_SWITCH_AFTER"] = str(switch_after)
		if fail_page is not None:
			env["STUB_RUN_PAGE_FAILS"] = str(fail_page)
		if run_by_id_fails:
			env["STUB_RUN_BY_ID_FAILS"] = "1"
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


OUR_IMPL_RUN_ID = 36720184070


def _other_issue_completed_impl_page() -> list[dict]:
	# Run 36717635823 (issue #5693): page 1 of the window held one completed
	# Implement run, the alt-model job's cancelled run, among skipped runs.
	return [_run(601, name="Internal: AI Implement", title=ALT_TITLE, conclusion="cancelled", created_at="2026-09-29T00:51:00Z")] + _noise_page(1000, count=99)


def _our_impl_run_page(conclusion: str | None = None, status: str = "in_progress") -> list[dict]:
	# A short page 2 holding our Implement run, older than every page-1 run.
	return _noise_page(2000, count=37) + [_run(OUR_IMPL_RUN_ID, name="Internal: AI Implement", conclusion=conclusion, status=status, created_at="2026-09-29T00:48:42Z")]


def _run_by_id_reads(calls: list[list[str]]) -> list[str]:
	return [call[1] for call in calls if call[1].endswith(f"/actions/runs/{OUR_IMPL_RUN_ID}")]


def test_implement_keeps_waiting_while_our_run_is_active_behind_other_completed_runs() -> None:
	# Issue #5693: page 1 showed "1 total, 0 active" (another issue's cancelled
	# run) while our run was in progress past it; the step failed with "All 1
	# implement workflow run(s) completed". It must wait for our run instead.
	pages = [_other_issue_completed_impl_page(), _our_impl_run_page()]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, pr_number=42, pr_after=5, fake_clock=True)
	assert rc == 0, log
	assert outputs.get("status") == "success", outputs
	assert outputs.get("run_id") == str(OUR_IMPL_RUN_ID), outputs
	assert outputs.get("pr_number") == "42", outputs
	assert "no PR was created" not in log, log
	assert f"Implement run {OUR_IMPL_RUN_ID} for issue #4712 is still active" in log, log
	# The next terminal check re-reads the cached run by ID instead of walking.
	assert f"Implement run {OUR_IMPL_RUN_ID} for issue #4712 is still in_progress" in log, log
	assert len(_run_by_id_reads(calls)) == 1, _run_by_id_reads(calls)
	page_two_reads = [path for path in _run_pages_requested(calls) if "&page=2&" in path]
	# One walk at the first terminal check, one run-ID capture once the PR appears.
	assert len(page_two_reads) == 2, page_two_reads
	assert all(f"created=>{APPROVED_AT}" in path for path in page_two_reads), page_two_reads


def test_implement_fails_with_our_run_id_when_our_run_completed_without_a_pr() -> None:
	pages = [_other_issue_completed_impl_page(), _our_impl_run_page(conclusion="failure", status="completed")]
	rc, outputs, _calls, log = _run_phase_step("wait-implement", pages, fake_clock=True)
	assert rc == 1, log
	assert outputs.get("status") == "implement_failed", outputs
	assert f"::error::All 1 implement workflow run(s) completed but no PR was created — issue #4712's newest Implement run {OUR_IMPL_RUN_ID} concluded failure" in log, log


def test_implement_fails_when_no_run_ran_for_our_issue() -> None:
	# The guard is not weakened: a complete walk with no non-skipped Implement
	# run for the issue (a skipped one of ours does not count) still fails.
	page_two = _noise_page(2000, count=10) + [_run(700, name="Internal: AI Implement", conclusion="skipped")]
	rc, outputs, _calls, log = _run_phase_step("wait-implement", [_other_issue_completed_impl_page(), page_two], fake_clock=True)
	assert rc == 1, log
	assert outputs.get("status") == "implement_failed", outputs
	assert f"::error::All 1 implement workflow run(s) completed but no PR was created — none of them ran for issue #4712: no non-skipped Implement run titled '{ISSUE_TITLE}' was created after {APPROVED_AT}" in log, log


def test_implement_treats_an_unreadable_page_as_unknown_until_the_inactivity_limit() -> None:
	# PHASE_TIMEOUT=1 and the fake clock make the second poll exceed the limit.
	pages = [_other_issue_completed_impl_page(), _our_impl_run_page(conclusion="failure", status="completed")]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, fail_page=2, phase_timeout="1", fake_clock=True)
	assert rc == 1, log
	assert outputs.get("status") == "timeout", outputs
	assert "Implement runs finished: a runs page could not be read — treating as unknown, waiting" in log, log
	assert "no PR was created" not in log, log
	assert "::error::Implement phase stalled — no activity for 1 minutes" in log, log
	# PR #5712 review: the second terminal check, 100 s after the unreadable
	# walk, is inside IMPL_SCOPED_RETRY_SECONDS and does not walk again.
	assert "a runs page could not be read 100s ago — walking again after 120s, waiting" in log, log
	assert sum("&page=2&" in path for path in _run_pages_requested(calls)) == 1, _run_pages_requested(calls)


def test_implement_walks_again_after_an_unreadable_page_once_the_retry_interval_passed() -> None:
	# PHASE_TIMEOUT=5 gives four terminal checks 100 s apart: walk (unreadable),
	# throttled, walk again at 200 s, throttled; then the inactivity limit.
	pages = [_other_issue_completed_impl_page(), _our_impl_run_page(conclusion="failure", status="completed")]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, fail_page=2, phase_timeout="5", fake_clock=True)
	assert rc == 1, log
	assert outputs.get("status") == "timeout", outputs
	assert "no PR was created" not in log, log
	assert log.count("a runs page could not be read — treating as unknown, waiting") == 2, log
	assert log.count("walking again after 120s, waiting") == 2, log
	assert sum("&page=2&" in path for path in _run_pages_requested(calls)) == 2, _run_pages_requested(calls)


def test_implement_treats_a_capped_walk_as_unknown_and_reads_at_most_ten_pages() -> None:
	pages = [_other_issue_completed_impl_page()] + [_noise_page(3000 + 100 * i) for i in range(11)]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, phase_timeout="5", fake_clock=True)
	assert rc == 1, log
	assert outputs.get("status") == "timeout", outputs
	assert "every page up to 10 was full — treating as unknown, waiting" in log, log
	assert "no PR was created" not in log, log
	# PR #5712 review: the window only grows, so the cap is kept and later
	# terminal checks never walk again.
	assert "an earlier walk found every page up to 10 full — treating as unknown, waiting" in log, log
	paged = [path for path in _run_pages_requested(calls) if "&page=" in path]
	# One walk of 10 pages across every terminal check; never page 11.
	assert len(paged) == 10, len(paged)
	assert not any("&page=11&" in path for path in paged), paged


def test_implement_walks_again_once_the_cached_run_completes() -> None:
	# The first terminal check caches our active run; by the second our run has
	# failed, so the cached read sees "completed" and a new walk decides.
	pages = [_other_issue_completed_impl_page(), _our_impl_run_page()]
	pages_later = [_other_issue_completed_impl_page(), _our_impl_run_page(conclusion="failure", status="completed")]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, pages_later=pages_later, switch_after=3, fake_clock=True)
	assert rc == 1, log
	assert outputs.get("status") == "implement_failed", outputs
	assert f"Implement run {OUR_IMPL_RUN_ID} for issue #4712 is still active" in log, log
	assert f"newest Implement run {OUR_IMPL_RUN_ID} concluded failure" in log, log
	assert len(_run_by_id_reads(calls)) == 1, _run_by_id_reads(calls)


def test_implement_throttles_the_walk_when_the_cached_run_cannot_be_read() -> None:
	# PR #5712 review round 2: a read of the cached run that keeps failing must
	# not send every poll back to a full walk. PHASE_TIMEOUT=5 gives four
	# terminal checks 100 s apart: walk (caches our active run), by-ID read
	# fails (throttled), fails again 100 s later (throttled), walk again at
	# 200 s; then the inactivity limit.
	pages = [_other_issue_completed_impl_page(), _our_impl_run_page()]
	rc, outputs, calls, log = _run_phase_step("wait-implement", pages, phase_timeout="5", fake_clock=True, run_by_id_fails=True)
	assert rc == 1, log
	assert outputs.get("status") == "timeout", outputs
	assert "no PR was created" not in log, log
	assert f"Implement run {OUR_IMPL_RUN_ID} could not be read 0s ago — walking again after 120s, waiting" in log, log
	assert f"Implement run {OUR_IMPL_RUN_ID} could not be read 100s ago — walking again after 120s, waiting" in log, log
	assert log.count(f"Implement run {OUR_IMPL_RUN_ID} for issue #4712 is still active") == 2, log
	assert len(_run_by_id_reads(calls)) == 3, _run_by_id_reads(calls)
	assert sum("&page=2&" in path for path in _run_pages_requested(calls)) == 2, _run_pages_requested(calls)


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
