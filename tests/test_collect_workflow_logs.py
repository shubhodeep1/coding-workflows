#!/usr/bin/env python3
"""Tests for scripts/collect_workflow_logs.py."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse


REPO_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR_PATH = REPO_ROOT / "scripts" / "collect_workflow_logs.py"


spec = importlib.util.spec_from_file_location("collect_workflow_logs", COLLECTOR_PATH)
collector = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
spec.loader.exec_module(collector)


def _write_exec(path: Path, body: str) -> None:
	path.write_text(body, encoding="utf-8")
	path.chmod(0o755)


def test_normalize_workflow_family_core_and_exclusion():
	assert collector.normalize_workflow_family("AI Clarify", ".github/workflows/clarify.yml") == "clarify"
	assert collector.normalize_workflow_family("AI Plan", ".github/workflows/plan.yml") == "plan"
	assert collector.normalize_workflow_family("AI Implement", ".github/workflows/implement.yml") == "implement"
	assert collector.normalize_workflow_family("AI Review", ".github/workflows/review_autofix.yml") == "review_autofix"
	assert collector.normalize_workflow_family("AI Validate", ".github/workflows/validate.yml") is None
	assert collector.normalize_workflow_family("AI Orchestrate", ".github/workflows/orchestrate.yml") is None


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


def test_list_runs_for_repo_paginates_and_filters_core_scope():
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
	assert [r["id"] for r in runs] == [1, 3]
	assert [r["_workflow_family"] for r in runs] == ["plan", "implement"]
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
		assert report["schema_version"] == "workflow_log_collector.v1"
		assert report["scope"]["workflow_families"] == ["clarify", "plan", "implement", "review_autofix"]
		assert report["summary"]["total_runs"] == 2
		assert report["summary"]["success_count"] == 1
		assert report["summary"]["failure_count"] == 1
		assert report["summary"]["cancelled_count"] == 0
		assert isinstance(report["summary"]["p50_duration_seconds"], float)
		assert isinstance(report["summary"]["p95_duration_seconds"], float)
		assert len(report["errors"]) == 1
		assert report["errors"][0]["scope"] == "jobs"


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
