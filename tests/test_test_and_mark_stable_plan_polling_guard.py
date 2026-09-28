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
if path == issue_path:
	payload = {"labels": [{"name": name} for name in os.environ["STUB_LABELS"].split(",") if name]}
elif path.startswith(issue_path + "/comments"):
	payload = []
elif "/actions/runs?" in path:
	pages = json.loads(open(os.environ["STUB_RUN_PAGES"], encoding="utf-8").read())
	match = re.search(r"[?&]page=(\d+)", path)
	page = int(match.group(1)) if match else 1
	payload = {"workflow_runs": pages[page - 1] if page <= len(pages) else []}
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


def _run_wait_plan(pages: list[list[dict]], labels: str) -> tuple[int, dict[str, str], list[list[str]], str]:
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
			"PLAN_PHASE_TIMEOUT": "60",
			"POLL_INTERVAL": "0",
			"GITHUB_OUTPUT": str(output_file),
			"STUB_GH_LOG": str(log_file),
			"STUB_RUN_PAGES": str(pages_file),
			"STUB_LABELS": labels,
		})
		proc = subprocess.run(["bash", "-c", _wait_plan_script()], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
		outputs: dict[str, str] = {}
		for line in output_file.read_text(encoding="utf-8").splitlines():
			key, _, value = line.partition("=")
			outputs[key] = value
		calls = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
		return proc.returncode, outputs, calls, proc.stdout + proc.stderr


def _run_pages_requested(calls: list[list[str]]) -> list[str]:
	return [call[1] for call in calls if "/actions/runs?" in call[1]]


def test_plan_label_without_a_captured_run_id_fails_at_plan_capture() -> None:
	# Issue #4723: the Plan label is present but the lookup finds no run.
	rc, outputs, _calls, log = _run_wait_plan([[_run(1, name="Internal: AI Clarify")]], "ai:awaiting-approval")
	assert rc == 1, log
	assert outputs.get("status") == "run_id_missing", outputs
	assert "run_id" not in outputs, outputs
	assert "::error::Issue #4712 reached ai:awaiting-approval but no non-skipped Plan run" in log


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
	# One single-page status poll, then 5 capture attempts of 10 pages each:
	# the per-poll cost stays one call (CLAUDE.md §15).
	assert len(requested) == 1 + 5 * 10, len(requested)
	assert "&page=1&" in requested[0] and "&page=1&" in requested[1] and "&page=2&" in requested[2], requested[:3]


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


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
