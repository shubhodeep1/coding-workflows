#!/usr/bin/env python3
"""Tests for scripts/collect_workflow_logs.py."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse


REPO_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR_PATH = REPO_ROOT / "scripts" / "collect_workflow_logs.py"


spec = importlib.util.spec_from_file_location("collect_workflow_logs", COLLECTOR_PATH)
assert spec is not None and spec.loader is not None
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def _write_exec(path: Path, body: str) -> None:
	path.write_text(body, encoding="utf-8")
	path.chmod(0o755)


def test_normalize_workflow_family_core_and_formerly_excluded():
	assert collector.normalize_workflow_family("AI Clarify", ".github/workflows/clarify.yml") == "clarify"
	assert collector.normalize_workflow_family("AI Plan", ".github/workflows/plan.yml") == "plan"
	assert collector.normalize_workflow_family("AI Implement", ".github/workflows/implement.yml") == "implement"
	assert collector.normalize_workflow_family("AI Review", ".github/workflows/review_autofix.yml") == "review_autofix"
	assert collector.normalize_workflow_family("AI Validate", ".github/workflows/validate.yml") == "validate"
	assert collector.normalize_workflow_family("AI Orchestrate", ".github/workflows/orchestrate.yml") == "orchestrate"
	assert collector.normalize_workflow_family("AI Orchestrate Poll", ".github/workflows/orchestrate_poll.yml") == "orchestrate_poll"
	assert collector.normalize_workflow_family("Clarify Respond", ".github/workflows/orchestrate_clarify_respond.yml") == "orchestrate_clarify_respond"
	assert collector.normalize_workflow_family("CI", ".github/workflows/ci.yml") == "ci"
	assert collector.normalize_workflow_family("Unknown", ".github/workflows/foo-bar.yml") == "foo_bar"
	assert collector.normalize_workflow_family("", "") == "other"


def test_compute_run_metrics_retries_and_duration_with_missing_timestamps():
	run = {
		"id": 12,
		"name": "AI Plan",
		"path": ".github/workflows/plan.yml",
		"_workflow_family": "plan",
		"status": "completed",
		"conclusion": "success",
		"run_attempt": 3,
		"created_at": "2026-04-10T10:00:00Z",
		"run_started_at": "2026-04-10T10:01:00Z",
		"updated_at": "2026-04-10T10:03:30Z",
	}
	row = collector.compute_run_metrics("owner/repo", run, jobs=[])
	assert row["run_attempt"] == 3
	assert row["retries"] == 2
	assert row["duration_seconds"] == 150

	run_missing = dict(run)
	run_missing["run_started_at"] = None
	row_missing = collector.compute_run_metrics("owner/repo", run_missing, jobs=[])
	assert row_missing["duration_seconds"] == 0


def test_extract_failure_point_step_precedence_then_job_fallback():
	jobs = [
		{
			"name": "lint",
			"conclusion": "failure",
			"steps": [
				{"name": "setup", "conclusion": "success"},
				{"name": "run lint", "conclusion": "failure"},
			],
		},
		{
			"name": "tests",
			"conclusion": "failure",
			"steps": [
				{"name": "run tests", "conclusion": "failure"},
			],
		},
	]
	point = collector.extract_failure_point(jobs)
	assert point == {"job_name": "lint", "step_name": "run lint"}

	point_job = collector.extract_failure_point([{"name": "build", "conclusion": "failure", "steps": []}])
	assert point_job == {"job_name": "build", "step_name": None}


def test_list_runs_for_repo_paginates_and_includes_all_families():
	orig = collector.gh_api_json
	calls = []

	def fake_gh_api_json(endpoint: str, *, token: str, retries: int = 3, backoff_seconds: float = 1.0):
		calls.append(endpoint)
		page = int(parse_qs(urlparse("https://x/" + endpoint).query).get("page", ["1"])[0])
		if page == 1:
			return {
				"workflow_runs": [
					{"id": 1, "name": "AI Plan", "path": ".github/workflows/plan.yml"},
					{"id": 2, "name": "AI Validate", "path": ".github/workflows/validate.yml"},
				]
			}
		if page == 2:
			return {
				"workflow_runs": [
					{"id": 3, "name": "AI Implement", "path": ".github/workflows/implement.yml"},
				]
			}
		return {"workflow_runs": []}

	collector.gh_api_json = fake_gh_api_json
	try:
		runs, capped = collector.list_runs_for_repo(
			"owner/repo",
			since_utc=datetime(2026, 4, 1, tzinfo=timezone.utc),
			per_page=100,
			max_pages=3,
			max_runs=0,
			token="x",
		)
	finally:
		collector.gh_api_json = orig

	assert capped is False
	assert [r["id"] for r in runs] == [1, 2, 3]
	assert [r["_workflow_family"] for r in runs] == ["plan", "validate", "implement"]
	assert len(calls) == 3


def test_list_jobs_for_run_paginates():
	orig = collector.gh_api_json
	calls = []

	def fake_gh_api_json(endpoint: str, *, token: str, retries: int = 3, backoff_seconds: float = 1.0):
		calls.append(endpoint)
		page = int(parse_qs(urlparse("https://x/" + endpoint).query).get("page", ["1"])[0])
		if page == 1:
			return {"jobs": [{"id": 1001}, {"id": 1002}]}
		if page == 2:
			return {"jobs": [{"id": 1003}]}
		return {"jobs": []}

	collector.gh_api_json = fake_gh_api_json
	try:
		jobs = collector.list_jobs_for_run(
			"owner/repo",
			101,
			per_page=100,
			max_pages=3,
			token="x",
		)
	finally:
		collector.gh_api_json = orig

	assert [j["id"] for j in jobs] == [1001, 1002, 1003]
	assert len(calls) == 3


def test_extract_log_excerpts_decodes_and_truncates_per_step():
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
		archive.writestr("logs/01_setup.txt", "A" * 20)
		archive.writestr("logs/02_run_tests.txt", "B" * 20)

	excerpts = collector.extract_log_excerpts(buffer.getvalue(), max_chars=8)
	assert excerpts == [
		{"step_name": "setup", "excerpt": "AAAAAAAA"},
		{"step_name": "run tests", "excerpt": "BBBBBBBB"},
	]


def test_select_notable_runs_for_logs_prioritizes_failed_then_retried_then_slow_per_repo():
	runs = [
		{
			"repository": "owner/repo-a",
			"run_id": 201,
			"conclusion": "failure",
			"retries": 0,
			"duration_seconds": 100,
			"created_at": "2026-04-10T11:00:00Z",
		},
		{
			"repository": "owner/repo-a",
			"run_id": 202,
			"conclusion": "success",
			"retries": 2,
			"duration_seconds": 300,
			"created_at": "2026-04-10T10:00:00Z",
		},
		{
			"repository": "owner/repo-b",
			"run_id": 301,
			"conclusion": "success",
			"retries": 0,
			"duration_seconds": 900,
			"created_at": "2026-04-10T09:00:00Z",
		},
		{
			"repository": "owner/repo-a",
			"run_id": 203,
			"conclusion": "success",
			"retries": 0,
			"duration_seconds": 800,
			"created_at": "2026-04-10T08:00:00Z",
		},
	]

	selected = collector.select_notable_runs_for_logs(runs, max_log_runs=3)
	assert [(item["repository"], item["run_id"]) for item in selected] == [
		("owner/repo-a", 201),
		("owner/repo-a", 202),
		("owner/repo-b", 301),
	]


def test_select_notable_runs_success_sampling():
	# Build 30 successful runs — the slow bucket picks the top SLOW_RUNS_PER_REPO (10)
	# by duration; remaining successful runs are candidates for random sampling.
	runs = [
		{
			"repository": "owner/repo",
			"run_id": i,
			"conclusion": "success",
			"retries": 0,
			"duration_seconds": 60,
			"created_at": f"2026-04-10T{10 + (i % 12):02d}:00:00Z",
		}
		for i in range(1, 31)
	]
	selected = collector.select_notable_runs_for_logs(runs, max_log_runs=20, success_sample_rate=0.07)
	sampled = [item for item in selected if item.get("_success_sampled")]
	non_sampled = [item for item in selected if not item.get("_success_sampled")]
	# Slow bucket picks 10 (SLOW_RUNS_PER_REPO); sampling picks from the remaining 20
	assert len(non_sampled) == collector.SLOW_RUNS_PER_REPO
	# ceil(20 * 0.07) = 2
	assert len(sampled) == 2
	assert all(item.get("_success_sampled") for item in sampled)
	assert all(not item.get("_success_sampled") for item in non_sampled)

	# Deterministic: same input produces same selection
	selected2 = collector.select_notable_runs_for_logs(runs, max_log_runs=20, success_sample_rate=0.07)
	assert [item["run_id"] for item in selected] == [item["run_id"] for item in selected2]

	# Rate 0 disables sampling — only slow runs remain
	selected_zero = collector.select_notable_runs_for_logs(runs, max_log_runs=20, success_sample_rate=0.0)
	assert all(not item.get("_success_sampled") for item in selected_zero)
	assert len(selected_zero) == collector.SLOW_RUNS_PER_REPO


def test_main_partial_jobs_failure_still_emits_report_with_errors():
	with tempfile.TemporaryDirectory(prefix="collector-test-") as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir(parents=True)
		store_file = tmp / "gh_store.json"
		report_file = tmp / "report.json"

		store = {
			"runs_pages": {
				"1": [
					{
						"id": 101,
						"name": "AI Plan",
						"path": ".github/workflows/plan.yml",
						"status": "completed",
						"conclusion": "success",
						"run_attempt": 2,
						"created_at": "2026-04-10T10:00:00Z",
						"run_started_at": "2026-04-10T10:01:00Z",
						"updated_at": "2026-04-10T10:03:00Z",
					},
					{
						"id": 102,
						"name": "AI Implement",
						"path": ".github/workflows/implement.yml",
						"status": "completed",
						"conclusion": "failure",
						"run_attempt": 1,
						"created_at": "2026-04-10T11:00:00Z",
						"run_started_at": "2026-04-10T11:00:30Z",
						"updated_at": "2026-04-10T11:05:30Z",
					},
					{
						"id": 104,
						"name": "AI Clarify",
						"path": ".github/workflows/clarify.yml",
						"status": "completed",
						"conclusion": "success",
						"run_attempt": 1,
						"created_at": "2026-04-10T12:00:00Z",
						"run_started_at": "2026-04-10T12:00:10Z",
						"updated_at": "2026-04-10T12:05:10Z",
					},
					{
						"id": 103,
						"name": "AI Validate",
						"path": ".github/workflows/validate.yml",
						"status": "completed",
						"conclusion": "success",
						"run_attempt": 1,
						"created_at": "2026-04-10T09:00:00Z",
						"run_started_at": "2026-04-10T09:00:10Z",
						"updated_at": "2026-04-10T09:01:00Z",
					},
				],
				"2": [],
			},
			"jobs": {
				"101": {
					"1": [
						{
							"id": 9001,
							"name": "plan",
							"conclusion": "success",
							"steps": [{"name": "step", "conclusion": "success"}],
						}
					],
					"2": [],
				}
			},
			"fail_jobs_for": [102],
			"log_files": {
				"101": {"01_plan.txt": "plan ok\n"},
				"102": {"01_failure.txt": "failure details\n"},
			},
			"fail_logs_for": [104],
		}
		store_file.write_text(json.dumps(store), encoding="utf-8")

		gh_mock = r'''#!/usr/bin/env python3
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

store_path = Path(os.environ["GH_MOCK_STORE"])
store = json.loads(store_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
if not args or args[0] != "api":
	print("unsupported", file=sys.stderr)
	sys.exit(1)

endpoint = ""
for arg in args[1:]:
	if not arg.startswith("-"):
		endpoint = arg
		break
if not endpoint:
	print("missing endpoint", file=sys.stderr)
	sys.exit(1)

if "/actions/runs?" in endpoint and "/jobs?" not in endpoint:
	query = parse_qs(urlparse("https://api/" + endpoint).query)
	page = query.get("page", ["1"])[0]
	print(json.dumps({"workflow_runs": store.get("runs_pages", {}).get(page, [])}))
	sys.exit(0)

m = re.search(r"/actions/runs/(\d+)/jobs\?", endpoint)
if m:
	run_id = int(m.group(1))
	if run_id in store.get("fail_jobs_for", []):
		print("jobs failed", file=sys.stderr)
		sys.exit(1)
	query = parse_qs(urlparse("https://api/" + endpoint).query)
	page = query.get("page", ["1"])[0]
	run_pages = store.get("jobs", {}).get(str(run_id), {})
	print(json.dumps({"jobs": run_pages.get(page, [])}))
	sys.exit(0)

m = re.search(r"/actions/runs/(\d+)/logs$", endpoint)
if m:
	run_id = int(m.group(1))
	if run_id in store.get("fail_logs_for", []):
		print("logs failed", file=sys.stderr)
		sys.exit(1)
	import io
	import zipfile
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
		for filename, content in store.get("log_files", {}).get(str(run_id), {}).items():
			archive.writestr(filename, content)
	sys.stdout.buffer.write(buffer.getvalue())
	sys.exit(0)

print(json.dumps({}))
'''
		_write_exec(bin_dir / "gh", gh_mock)

		env = os.environ.copy()
		env.update(
			{
				"GH_TOKEN": "token",
				"GH_MOCK_STORE": str(store_file),
				"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			}
		)
		proc = subprocess.run(
			[
				"python3",
				str(COLLECTOR_PATH),
				"--repo",
				"owner/repo",
				"--lookback-days",
				"7",
				"--max-pages",
				"2",
				"--max-log-runs",
				"3",
				"--output",
				str(report_file),
			],
			cwd=str(REPO_ROOT),
			env=env,
			capture_output=True,
			text=True,
		)
		assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

		report = json.loads(report_file.read_text(encoding="utf-8"))
		assert report["schema_version"] == "workflow_log_collector.v2"
		# All families are now collected — validate is included
		assert isinstance(report["scope"]["workflow_families"], list)
		assert report["summary"]["total_runs"] == 4
		assert report["summary"]["success_count"] == 3
		assert report["summary"]["failure_count"] == 1
		assert report["summary"]["cancelled_count"] == 0
		assert report["summary"]["other_count"] == 0
		assert isinstance(report["summary"]["p50_duration_seconds"], float)
		assert isinstance(report["summary"]["p95_duration_seconds"], float)
		assert "sampled_success_runs" in report["summary"]
		assert "success_sample_rate" in report["scope"]
		assert len(report["errors"]) == 2
		assert sorted(item["scope"] for item in report["errors"]) == ["jobs", "logs"]

		by_run_id = {item["run_id"]: item for item in report["runs"]}
		assert by_run_id[101]["log_excerpts"] == [{"step_name": "plan", "excerpt": "plan ok\n"}]
		assert by_run_id[102]["log_excerpts"] == [{"step_name": "failure", "excerpt": "failure details\n"}]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as exc:  # noqa: BLE001
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
