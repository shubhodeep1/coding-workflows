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
	assert collector.normalize_workflow_family("AI Orchestrate Poller", ".github/workflows/orchestrate_poll.yml") == "orchestrate_poll"
	assert collector.normalize_workflow_family("AI Orchestrate Poll", ".github/workflows/orchestrate_poll.yml") == "orchestrate_poll"
	assert collector.normalize_workflow_family("AI Orchestrate Clarify Respond", ".github/workflows/orchestrate_clarify_respond.yml") == "orchestrate_clarify_respond"
	assert collector.normalize_workflow_family("Clarify Respond", ".github/workflows/orchestrate_clarify_respond.yml") == "orchestrate_clarify_respond"
	assert collector.normalize_workflow_family("AI Issue PR Status", ".github/workflows/issue_pr_status.yml") == "issue_pr_status"
	assert collector.normalize_workflow_family("AI Cancel on PR Close", ".github/workflows/cancel_on_pr_close.yml") == "cancel_on_pr_close"
	assert collector.normalize_workflow_family("AI Memory Maintenance", ".github/workflows/memory_maintenance.yml") == "memory_maintenance"
	assert collector.normalize_workflow_family("CI", ".github/workflows/ci.yml") == "ci"
	assert collector.normalize_workflow_family("Not A Pipeline Workflow", ".github/workflows/custom.yml") == "custom"
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

	def fake_gh_api_json(
		endpoint: str,
		*,
		token: str,
		retries: int = 3,
		backoff_seconds: float = 1.0,
		request_headers: dict[str, str] | None = None,
		include_response_meta: bool = False,
	):
		calls.append(endpoint)
		page = int(parse_qs(urlparse("https://x/" + endpoint).query).get("page", ["1"])[0])
		meta = {"status_code": 200, "headers": {"etag": "W/\"abc\""}}
		if page == 1:
			payload = {
				"workflow_runs": [
					{"id": 1, "name": "AI Plan", "path": ".github/workflows/plan.yml"},
					{"id": 2, "name": "AI Validate", "path": ".github/workflows/validate.yml"},
				]
			}
			return (payload, meta) if include_response_meta else payload
		if page == 2:
			payload = {
				"workflow_runs": [
					{"id": 3, "name": "AI Implement", "path": ".github/workflows/implement.yml"},
					{"id": 4, "name": "Release CI", "path": ".github/workflows/ci.yml"},
				]
			}
			return (payload, meta) if include_response_meta else payload
		payload = {"workflow_runs": []}
		return (payload, meta) if include_response_meta else payload

	collector.gh_api_json = fake_gh_api_json
	try:
		runs, capped, meta = collector.list_runs_for_repo(
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
	assert [r["id"] for r in runs] == [1, 2, 3, 4]
	assert [r["_workflow_family"] for r in runs] == ["plan", "validate", "implement", "ci"]
	assert meta["not_modified"] is False
	assert meta["etag"] == "W/\"abc\""
	assert len(calls) == 3


def test_list_runs_for_repo_uses_etag_and_returns_not_modified() -> None:
	orig = collector.gh_api_json

	def fake_gh_api_json(
		endpoint: str,
		*,
		token: str,
		retries: int = 3,
		backoff_seconds: float = 1.0,
		request_headers: dict[str, str] | None = None,
		include_response_meta: bool = False,
	):
		assert request_headers == {"If-None-Match": "W/\"cached\""}
		payload = {}
		meta = {"status_code": 304, "headers": {"etag": "W/\"cached\""}}
		return (payload, meta) if include_response_meta else payload

	collector.gh_api_json = fake_gh_api_json
	try:
		runs, capped, meta = collector.list_runs_for_repo(
			"owner/repo",
			since_utc=datetime(2026, 4, 1, tzinfo=timezone.utc),
			per_page=100,
			max_pages=3,
			max_runs=0,
			token="x",
			etag="W/\"cached\"",
		)
	finally:
		collector.gh_api_json = orig

	assert runs == []
	assert capped is False
	assert meta["not_modified"] is True
	assert meta["etag"] == "W/\"cached\""


def test_main_reuses_cached_snapshot_on_304_and_skips_jobs_and_logs() -> None:
	with tempfile.TemporaryDirectory(prefix="collector-cache-test-") as td:
		report_file = Path(td) / "report.json"
		cached_run = {
			"id": 501,
			"name": "AI Implement",
			"path": ".github/workflows/implement.yml",
			"status": "completed",
			"conclusion": "failure",
			"run_attempt": 1,
			"created_at": "2026-04-10T11:00:00Z",
			"run_started_at": "2026-04-10T11:00:30Z",
			"updated_at": "2026-04-10T11:05:30Z",
			"_workflow_family": "implement",
		}
		cached_row = collector.compute_run_metrics("owner/repo", cached_run, jobs=[])
		cached_row["log_excerpts"] = [{"step_name": "failure", "excerpt": "cached log"}]
		cached_row["cost_telemetry"] = {"or_total_tokens": 0}

		cache_payload = {
			"schema_version": "v1",
			"repositories": {
				"owner/repo": {
					"runs_etag": "W/\"cached\"",
					"runs_window_start": "2026-04-01T00:00:00Z",
					"jobs_seen_set": [501],
					"logs_seen_set": [501],
					"last_updated": "2026-04-11T00:00:00Z",
					"runs_snapshot": [cached_run],
					"rows_snapshot": [cached_row],
				}
			},
		}

		orig_cache_read = collector._cache_read_context
		orig_cache_write = collector._cache_write_context
		orig_list_runs = collector.list_runs_for_repo
		orig_list_jobs = collector.list_jobs_for_run
		orig_fetch_logs = collector._fetch_run_log_archive

		calls = {"jobs": 0, "logs": 0}
		persisted: dict[str, dict] = {}

		def fake_cache_read_context(**_: object):
			return cache_payload, None, None

		def fake_cache_write_context(**kwargs: object):
			payload = kwargs.get("payload")
			if isinstance(payload, dict):
				persisted["payload"] = payload
			return True

		def fake_list_runs_for_repo(*args: object, **kwargs: object):
			assert kwargs.get("etag") == "W/\"cached\""
			return [], False, {"not_modified": True, "etag": "W/\"cached\"", "status_code": 304}

		def fake_list_jobs_for_run(*args: object, **kwargs: object):
			calls["jobs"] += 1
			raise AssertionError("jobs API should be skipped when cached row exists")

		def fake_fetch_run_log_archive(*args: object, **kwargs: object):
			calls["logs"] += 1
			raise AssertionError("log archive fetch should be skipped when cached data exists")

		collector._cache_read_context = fake_cache_read_context
		collector._cache_write_context = fake_cache_write_context
		collector.list_runs_for_repo = fake_list_runs_for_repo
		collector.list_jobs_for_run = fake_list_jobs_for_run
		collector._fetch_run_log_archive = fake_fetch_run_log_archive
		try:
			rc = collector.main(
				[
					"--repo",
					"owner/repo",
					"--since",
					"2026-04-01T00:00:00Z",
					"--max-log-runs",
					"1",
					"--output",
					str(report_file),
				]
			)
		finally:
			collector._cache_read_context = orig_cache_read
			collector._cache_write_context = orig_cache_write
			collector.list_runs_for_repo = orig_list_runs
			collector.list_jobs_for_run = orig_list_jobs
			collector._fetch_run_log_archive = orig_fetch_logs

		assert rc == 0
		assert calls == {"jobs": 0, "logs": 0}
		report = json.loads(report_file.read_text(encoding="utf-8"))
		assert report["summary"]["total_runs"] == 1
		assert report["runs"][0]["run_id"] == 501
		assert report["runs"][0]["log_excerpts"] == [{"step_name": "failure", "excerpt": "cached log"}]
		assert report["runs"][0]["cost_telemetry"] == {"or_total_tokens": 0}
		assert "payload" in persisted


def test_main_refetches_cached_logs_when_cost_telemetry_is_missing() -> None:
	with tempfile.TemporaryDirectory(prefix="collector-cache-telemetry-test-") as td:
		report_file = Path(td) / "report.json"
		cached_run = {
			"id": 502,
			"name": "AI Review",
			"path": ".github/workflows/review_autofix.yml",
			"status": "completed",
			"conclusion": "failure",
			"run_attempt": 1,
			"created_at": "2026-04-10T12:00:00Z",
			"run_started_at": "2026-04-10T12:00:30Z",
			"updated_at": "2026-04-10T12:05:30Z",
			"_workflow_family": "review_autofix",
		}
		cached_row = collector.compute_run_metrics("owner/repo", cached_run, jobs=[])
		cached_row["log_excerpts"] = [{"step_name": "review", "excerpt": "cached excerpt"}]

		cache_payload = {
			"schema_version": "v1",
			"repositories": {
				"owner/repo": {
					"runs_etag": "W/\"cached\"",
					"runs_window_start": "2026-04-01T00:00:00Z",
					"jobs_seen_set": [502],
					"logs_seen_set": [502],
					"last_updated": "2026-04-11T00:00:00Z",
					"runs_snapshot": [cached_run],
					"rows_snapshot": [cached_row],
				}
			},
		}

		orig_cache_read = collector._cache_read_context
		orig_cache_write = collector._cache_write_context
		orig_list_runs = collector.list_runs_for_repo
		orig_list_jobs = collector.list_jobs_for_run
		orig_fetch_logs = collector._fetch_run_log_archive

		calls = {"jobs": 0, "logs": 0}
		persisted: dict[str, dict] = {}

		def fake_cache_read_context(**_: object):
			return cache_payload, None, None

		def fake_cache_write_context(**kwargs: object):
			payload = kwargs.get("payload")
			if isinstance(payload, dict):
				persisted["payload"] = payload
			return True

		def fake_list_runs_for_repo(*args: object, **kwargs: object):
			assert kwargs.get("etag") == "W/\"cached\""
			return [], False, {"not_modified": True, "etag": "W/\"cached\"", "status_code": 304}

		def fake_list_jobs_for_run(*args: object, **kwargs: object):
			calls["jobs"] += 1
			raise AssertionError("jobs API should be skipped when cached row exists")

		def fake_fetch_run_log_archive(repo: str, run_id: int, *, token: str, cache=None):
			calls["logs"] += 1
			assert repo == "owner/repo"
			assert run_id == 502
			buffer = io.BytesIO()
			with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
				archive.writestr(
					"logs/01_review.txt",
					"INFO: openrouter usage phase=review call=pass1 model=openai/gpt-5.4 cache_enabled=true cache_breakpoint_enabled=false cache_breakpoint_fallback_retry=false prompt_tokens=10 completion_tokens=5 total_tokens=15 cache_creation_input_tokens=0 cache_read_input_tokens=0\nfresh excerpt\n",
				)
			return buffer.getvalue()

		collector._cache_read_context = fake_cache_read_context
		collector._cache_write_context = fake_cache_write_context
		collector.list_runs_for_repo = fake_list_runs_for_repo
		collector.list_jobs_for_run = fake_list_jobs_for_run
		collector._fetch_run_log_archive = fake_fetch_run_log_archive
		try:
			rc = collector.main(
				[
					"--repo",
					"owner/repo",
					"--since",
					"2026-04-01T00:00:00Z",
					"--max-log-runs",
					"1",
					"--output",
					str(report_file),
				]
			)
		finally:
			collector._cache_read_context = orig_cache_read
			collector._cache_write_context = orig_cache_write
			collector.list_runs_for_repo = orig_list_runs
			collector.list_jobs_for_run = orig_list_jobs
			collector._fetch_run_log_archive = orig_fetch_logs

		assert rc == 0
		assert calls == {"jobs": 0, "logs": 1}
		report = json.loads(report_file.read_text(encoding="utf-8"))
		run = report["runs"][0]
		assert run["run_id"] == 502
		assert run["log_excerpts"] == [
			{
				"step_name": "review",
				"excerpt": "INFO: openrouter usage phase=review call=pass1 model=openai/gpt-5.4 cache_enabled=true cache_breakpoint_enabled=false cache_breakpoint_fallback_retry=false prompt_tokens=10 completion_tokens=5 total_tokens=15 cache_creation_input_tokens=0 cache_read_input_tokens=0\nfresh excerpt\n",
			}
		]
		assert run["cost_telemetry"]["or_total_tokens"] == 15
		assert report["summary"]["cost_telemetry"]["runs_with_log_telemetry"] == 1
		assert persisted["payload"]["repositories"]["owner/repo"]["rows_snapshot"][0]["cost_telemetry"]["or_total_tokens"] == 15


def test_main_refetches_cached_logs_preserves_excerpts_and_dedupes_wrapper_child_telemetry() -> None:
	with tempfile.TemporaryDirectory(prefix="collector-cache-dedupe-test-") as td:
		report_file = Path(td) / "report.json"
		cached_run = {
			"id": 503,
			"name": "AI Review",
			"path": ".github/workflows/review_autofix.yml",
			"status": "completed",
			"conclusion": "failure",
			"run_attempt": 1,
			"created_at": "2026-04-10T12:30:00Z",
			"run_started_at": "2026-04-10T12:30:30Z",
			"updated_at": "2026-04-10T12:35:30Z",
			"_workflow_family": "review_autofix",
		}
		cached_row = collector.compute_run_metrics("owner/repo", cached_run, jobs=[])

		cache_payload = {
			"schema_version": "v1",
			"repositories": {
				"owner/repo": {
					"runs_etag": "W/\"cached\"",
					"runs_window_start": "2026-04-01T00:00:00Z",
					"jobs_seen_set": [503],
					"logs_seen_set": [503],
					"last_updated": "2026-04-11T00:00:00Z",
					"runs_snapshot": [cached_run],
					"rows_snapshot": [cached_row],
				}
			},
		}

		duplicate_openrouter_line = (
			"INFO: openrouter usage phase=review call=pass1 model=openai/gpt-5.4 "
			"cache_enabled=true cache_breakpoint_enabled=false cache_breakpoint_fallback_retry=false "
			"prompt_tokens=100 completion_tokens=25 total_tokens=125 "
			"cache_creation_input_tokens=30 cache_read_input_tokens=40"
		)
		duplicate_query_line = "SEMBLE_QUERY target=reviewer-context chunks=4 bytes=88 ms=3"
		duplicate_fallback_line = (
			"SEMBLE_FALLBACK target=reviewer-context reason=timeout context=contract-test ms=1"
		)
		runtime_fallback_line = "SEMBLE_FALLBACK target=overflow reason=exit=7 raw failure from semble ms=11"
		wrapper_content = "\n".join(
			[
				duplicate_openrouter_line,
				duplicate_query_line,
				duplicate_fallback_line,
				"wrapper-only detail",
			]
		) + "\n"
		child_pass1_content = "\n".join(
			[
				duplicate_openrouter_line,
				duplicate_query_line,
				duplicate_fallback_line,
				"pass1 detail",
			]
		) + "\n"
		child_pass2_content = "\n".join(
			[
				duplicate_openrouter_line,
				duplicate_query_line,
				runtime_fallback_line,
				"pass2 detail",
			]
		) + "\n"

		orig_cache_read = collector._cache_read_context
		orig_cache_write = collector._cache_write_context
		orig_list_runs = collector.list_runs_for_repo
		orig_list_jobs = collector.list_jobs_for_run
		orig_fetch_logs = collector._fetch_run_log_archive

		calls = {"jobs": 0, "logs": 0}
		persisted: dict[str, dict] = {}

		def fake_cache_read_context(**_: object):
			return cache_payload, None, None

		def fake_cache_write_context(**kwargs: object):
			payload = kwargs.get("payload")
			if isinstance(payload, dict):
				persisted["payload"] = payload
			return True

		def fake_list_runs_for_repo(*args: object, **kwargs: object):
			assert kwargs.get("etag") == "W/\"cached\""
			return [], False, {"not_modified": True, "etag": "W/\"cached\"", "status_code": 304}

		def fake_list_jobs_for_run(*args: object, **kwargs: object):
			calls["jobs"] += 1
			raise AssertionError("jobs API should be skipped when cached row exists")

		def fake_fetch_run_log_archive(repo: str, run_id: int, *, token: str, cache=None):
			calls["logs"] += 1
			assert repo == "owner/repo"
			assert run_id == 503
			buffer = io.BytesIO()
			with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
				archive.writestr("logs/01_review.txt", wrapper_content)
				archive.writestr("logs/01_review/02_pass1.txt", child_pass1_content)
				archive.writestr("logs/01_review/03_pass2.txt", child_pass2_content)
			return buffer.getvalue()

		collector._cache_read_context = fake_cache_read_context
		collector._cache_write_context = fake_cache_write_context
		collector.list_runs_for_repo = fake_list_runs_for_repo
		collector.list_jobs_for_run = fake_list_jobs_for_run
		collector._fetch_run_log_archive = fake_fetch_run_log_archive
		try:
			rc = collector.main(
				[
					"--repo",
					"owner/repo",
					"--since",
					"2026-04-01T00:00:00Z",
					"--max-log-runs",
					"1",
					"--output",
					str(report_file),
				]
			)
		finally:
			collector._cache_read_context = orig_cache_read
			collector._cache_write_context = orig_cache_write
			collector.list_runs_for_repo = orig_list_runs
			collector.list_jobs_for_run = orig_list_jobs
			collector._fetch_run_log_archive = orig_fetch_logs

		assert rc == 0
		assert calls == {"jobs": 0, "logs": 1}
		report = json.loads(report_file.read_text(encoding="utf-8"))
		run = report["runs"][0]
		expected_excerpts = [
			{"step_name": "review", "excerpt": wrapper_content},
			{"step_name": "review/pass1", "excerpt": child_pass1_content},
			{"step_name": "review/pass2", "excerpt": child_pass2_content},
		]
		assert run["run_id"] == 503
		assert run["log_excerpts"] == expected_excerpts
		assert run["cost_telemetry"]["or_calls"] == 2
		assert run["cost_telemetry"]["or_total_tokens"] == 250
		assert run["cost_telemetry"]["semble_query_calls"] == 2
		assert run["cost_telemetry"]["semble_query_bytes"] == 176
		assert run["cost_telemetry"]["semble_fallbacks"] == 2
		assert run["cost_telemetry"]["semble_contract_test_fallbacks"] == 1
		assert run["cost_telemetry"]["semble_runtime_fallbacks"] == 1
		persisted_row = persisted["payload"]["repositories"]["owner/repo"]["rows_snapshot"][0]
		assert persisted_row["log_excerpts"] == expected_excerpts
		assert persisted_row["cost_telemetry"]["or_calls"] == 2
		assert persisted_row["cost_telemetry"]["or_total_tokens"] == 250
		assert persisted_row["cost_telemetry"]["semble_query_calls"] == 2
		assert persisted_row["cost_telemetry"]["semble_fallbacks"] == 2


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


def test_select_runs_for_log_export_categories_deterministic_and_capped():
	runs = [
		{
			"repository": "owner/repo",
			"run_id": 11,
			"conclusion": "failure",
			"duration_seconds": 500,
			"created_at": "2026-04-10T11:00:00Z",
		},
		{
			"repository": "owner/repo",
			"run_id": 12,
			"conclusion": "success",
			"duration_seconds": 900,
			"created_at": "2026-04-10T12:00:00Z",
		},
		{
			"repository": "owner/repo",
			"run_id": 13,
			"conclusion": "success",
			"duration_seconds": 120,
			"created_at": "2026-04-10T10:00:00Z",
		},
		{
			"repository": "owner/repo",
			"run_id": 14,
			"conclusion": "failure",
			"duration_seconds": 300,
			"created_at": "2026-04-10T09:00:00Z",
		},
	]

	categories = collector.select_runs_for_log_export_categories(runs, max_log_runs=2)
	assert list(categories.keys()) == ["errors", "slow", "recent"]
	assert [item["run_id"] for item in categories["errors"]] == [11, 14]
	assert [item["run_id"] for item in categories["slow"]] == [12, 11]
	assert [item["run_id"] for item in categories["recent"]] == [12, 11]


def test_select_runs_for_log_export_categories_tiebreakers_are_deterministic():
	runs = [
		{
			"repository": "owner/repo",
			"run_id": 21,
			"conclusion": "failure",
			"duration_seconds": 500,
			"created_at": "2026-04-10T11:00:00Z",
		},
		{
			"repository": "owner/repo",
			"run_id": 22,
			"conclusion": "failure",
			"duration_seconds": 500,
			"created_at": "2026-04-10T11:00:00Z",
		},
		{
			"repository": "owner/repo",
			"run_id": 23,
			"conclusion": "success",
			"duration_seconds": 500,
			"created_at": "2026-04-10T11:00:00Z",
		},
	]

	categories = collector.select_runs_for_log_export_categories(runs, max_log_runs=3)
	assert [item["run_id"] for item in categories["errors"]] == [22, 21]
	assert [item["run_id"] for item in categories["slow"]] == [23, 22, 21]
	assert [item["run_id"] for item in categories["recent"]] == [23, 22, 21]


def test_extract_full_logs_decodes_without_truncation():
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
		archive.writestr("logs/01_failure.txt", "E" * 5000)

	full_logs = collector.extract_full_logs(buffer.getvalue())
	assert full_logs == [{"step_name": "failure", "content": "E" * 5000}]


def test_apply_cost_telemetry_from_full_logs_preserves_review_warning_signals():
	run = {
		"repository": "owner/repo",
		"run_id": 410,
		"conclusion": "failure",
		"duration_seconds": 12,
		"workflow_family": "review_autofix",
	}
	full_logs = [
		{
			"step_name": "review",
			"content": "\n".join(
				[
					"INFO: openrouter usage phase=review call=pass1 model=openai/gpt-5.4 cache_enabled=true cache_breakpoint_enabled=false cache_breakpoint_fallback_retry=false prompt_tokens=100 completion_tokens=25 total_tokens=125 cache_creation_input_tokens=30 cache_read_input_tokens=40",
					"SEMBLE_FALLBACK target=reviewer-context reason=timeout context=contract-test ms=5",
					"BREAK_GLASS: phase=editor reason=manual-override",
					"CONTEXT_BUDGET_WARN: phase=review prompt_tokens=200000 model_context_window=272000 ratio=0.7353 threshold=190400",
				]
			),
		}
	]

	collector._apply_cost_telemetry_from_full_logs(run, full_logs)
	telemetry = run["cost_telemetry"]
	assert telemetry["or_total_tokens"] == 125
	assert telemetry["semble_fallbacks"] == 1
	assert telemetry["semble_contract_test_fallbacks"] == 1
	assert telemetry["semble_runtime_fallbacks"] == 0
	assert telemetry["break_glass_count"] == 1
	assert telemetry["context_budget_warn_count"] == 1
	assert telemetry["wall_clock_p50_ms"] == 12000
	assert telemetry["wall_clock_p99_ms"] == 12000

	report = collector.build_report(["owner/repo"], [run], [])
	assert report["summary"]["cost_telemetry"]["runs_with_log_telemetry"] == 1
	assert report["summary"]["cost_telemetry"]["semble_contract_test_fallbacks"] == 1
	assert report["summary"]["cost_telemetry"]["break_glass_count"] == 1
	assert report["summary"]["cost_telemetry"]["context_budget_warn_count"] == 1


def test_structured_cost_telemetry_line_key_strips_trailing_whitespace():
	line = "SEMBLE_QUERY target=reviewer-context chunks=4 bytes=88 ms=3 \t\r\n"
	assert (
		collector._structured_cost_telemetry_line_key(line)
		== "SEMBLE_QUERY target=reviewer-context chunks=4 bytes=88 ms=3"
	)


def test_apply_cost_telemetry_from_full_logs_dedupes_wrapper_child_structured_lines_only():
	duplicate_openrouter_line = (
		"INFO: openrouter usage phase=review call=pass1 model=openai/gpt-5.4 "
		"cache_enabled=true cache_breakpoint_enabled=false cache_breakpoint_fallback_retry=false "
		"prompt_tokens=100 completion_tokens=25 total_tokens=125 "
		"cache_creation_input_tokens=30 cache_read_input_tokens=40"
	)
	duplicate_query_line = "SEMBLE_QUERY target=reviewer-context chunks=4 bytes=88 ms=3"
	duplicate_fallback_line = (
		"SEMBLE_FALLBACK target=reviewer-context reason=timeout context=contract-test ms=5"
	)
	runtime_fallback_line = "SEMBLE_FALLBACK target=overflow reason=exit=7 raw failure from semble ms=11"
	run = {
		"repository": "owner/repo",
		"run_id": 411,
		"conclusion": "failure",
		"duration_seconds": 12,
		"workflow_family": "review_autofix",
	}
	full_logs = [
		{
			"step_name": "review",
			"content": "\n".join(
				[
					duplicate_openrouter_line,
					duplicate_query_line,
					duplicate_fallback_line,
					"wrapper-only detail",
				]
			) + "\n",
		},
		{
			"step_name": "review/pass1",
			"content": "\n".join(
				[
					duplicate_openrouter_line,
					duplicate_query_line,
					duplicate_fallback_line,
					"pass1 detail",
				]
			) + "\n",
		},
		{
			"step_name": "review/pass2",
			"content": "\n".join(
				[
					duplicate_openrouter_line,
					duplicate_query_line,
					runtime_fallback_line,
					"pass2 detail",
				]
			) + "\n",
		},
	]

	collector._apply_cost_telemetry_from_full_logs(run, full_logs)
	telemetry = run["cost_telemetry"]
	assert telemetry["or_prompt_tokens"] == 200
	assert telemetry["or_completion_tokens"] == 50
	assert telemetry["or_total_tokens"] == 250
	assert telemetry["or_calls"] == 2
	assert telemetry["semble_query_calls"] == 2
	assert telemetry["semble_query_bytes"] == 176
	assert telemetry["semble_fallbacks"] == 2
	assert telemetry["semble_contract_test_fallbacks"] == 1
	assert telemetry["semble_runtime_fallbacks"] == 1
	assert telemetry["wall_clock_p50_ms"] == 12000
	assert telemetry["wall_clock_p99_ms"] == 12000

	report = collector.build_report(["owner/repo"], [run], [])
	summary = report["summary"]["cost_telemetry"]
	assert summary["runs_with_log_telemetry"] == 1
	assert summary["or_calls"] == 2
	assert summary["or_total_tokens"] == 250
	assert summary["semble_query_calls"] == 2
	assert summary["semble_fallbacks"] == 2
	assert summary["semble_contract_test_fallbacks"] == 1
	assert summary["semble_runtime_fallbacks"] == 1


def test_fetch_run_log_archive_retries_transient_then_succeeds():
	orig_gh_api_bytes = collector.gh_api_bytes
	orig_sleep = collector.time.sleep
	call_retries: list[int] = []
	sleep_calls: list[float] = []
	attempts = [
		RuntimeError(
			"gh api failed for repos/owner/repo/actions/runs/410/logs (exit=1): 502 Bad Gateway"
		),
		b"archive-bytes",
	]
	cache: dict[tuple[str, int], bytes | Exception] = {}

	def fake_gh_api_bytes(
		endpoint: str,
		*,
		token: str,
		retries: int = 3,
		backoff_seconds: float = 1.0,
	) -> bytes:
		_ = backoff_seconds
		assert endpoint == "repos/owner/repo/actions/runs/410/logs"
		assert token == "token"
		call_retries.append(retries)
		outcome = attempts.pop(0)
		if isinstance(outcome, Exception):
			raise outcome
		return outcome

	collector.gh_api_bytes = fake_gh_api_bytes
	collector.time.sleep = lambda seconds: sleep_calls.append(seconds)
	try:
		payload = collector._fetch_run_log_archive("owner/repo", 410, token="token", cache=cache)
	finally:
		collector.gh_api_bytes = orig_gh_api_bytes
		collector.time.sleep = orig_sleep

	assert payload == b"archive-bytes"
	assert call_retries == [1, 1]
	assert sleep_calls == [collector.LOG_ARCHIVE_FETCH_BACKOFF_SECONDS]
	assert cache[("owner/repo", 410)] == b"archive-bytes"


def test_fetch_run_log_archive_classifies_missing_archive_soft_fail():
	orig_gh_api_bytes = collector.gh_api_bytes
	call_retries: list[int] = []
	cache: dict[tuple[str, int], bytes | Exception] = {}

	def fake_gh_api_bytes(
		endpoint: str,
		*,
		token: str,
		retries: int = 3,
		backoff_seconds: float = 1.0,
	) -> bytes:
		_ = backoff_seconds
		assert endpoint == "repos/owner/repo/actions/runs/404/logs"
		assert token == "token"
		call_retries.append(retries)
		raise RuntimeError(
			"gh api failed for repos/owner/repo/actions/runs/404/logs (exit=1): HTTP 404 Not Found"
		)

	collector.gh_api_bytes = fake_gh_api_bytes
	try:
		try:
			collector._fetch_run_log_archive("owner/repo", 404, token="token", cache=cache)
			raise AssertionError("expected missing-archive soft-fail")
		except Exception as exc:  # noqa: BLE001
			message = str(exc)
			assert message.startswith("partial_data:missing_log_archive ")
			assert "repository=owner/repo" in message
			assert "run_id=404" in message

		try:
			collector._fetch_run_log_archive("owner/repo", 404, token="token", cache=cache)
			raise AssertionError("expected cached missing-archive soft-fail")
		except Exception as cached_exc:  # noqa: BLE001
			assert str(cached_exc).startswith("partial_data:missing_log_archive ")
	finally:
		collector.gh_api_bytes = orig_gh_api_bytes

	assert call_retries == [1]


def test_fetch_run_log_archive_classifies_410_missing_archive_soft_fail():
	orig_gh_api_bytes = collector.gh_api_bytes
	call_retries: list[int] = []
	cache: dict[tuple[str, int], bytes | Exception] = {}

	def fake_gh_api_bytes(
		endpoint: str,
		*,
		token: str,
		retries: int = 3,
		backoff_seconds: float = 1.0,
	) -> bytes:
		_ = backoff_seconds
		assert endpoint == "repos/owner/repo/actions/runs/410/logs"
		assert token == "token"
		call_retries.append(retries)
		raise RuntimeError(
			"gh api failed for repos/owner/repo/actions/runs/410/logs (exit=1): HTTP 410 Gone"
		)

	collector.gh_api_bytes = fake_gh_api_bytes
	try:
		try:
			collector._fetch_run_log_archive("owner/repo", 410, token="token", cache=cache)
			raise AssertionError("expected missing-archive soft-fail")
		except Exception as exc:  # noqa: BLE001
			message = str(exc)
			assert message.startswith("partial_data:missing_log_archive ")
			assert "repository=owner/repo" in message
			assert "run_id=410" in message
	finally:
		collector.gh_api_bytes = orig_gh_api_bytes

	assert call_retries == [1]


def test_fetch_run_log_archive_retry_exhaustion_raises_last_error():
	orig_gh_api_bytes = collector.gh_api_bytes
	orig_sleep = collector.time.sleep
	call_retries: list[int] = []
	sleep_calls: list[float] = []
	cache: dict[tuple[str, int], bytes | Exception] = {}

	def fake_gh_api_bytes(
		endpoint: str,
		*,
		token: str,
		retries: int = 3,
		backoff_seconds: float = 1.0,
	) -> bytes:
		_ = backoff_seconds
		assert endpoint == "repos/owner/repo/actions/runs/412/logs"
		assert token == "token"
		call_retries.append(retries)
		raise RuntimeError(
			"gh api failed for repos/owner/repo/actions/runs/412/logs (exit=1): 502 Bad Gateway"
		)

	collector.gh_api_bytes = fake_gh_api_bytes
	collector.time.sleep = lambda seconds: sleep_calls.append(seconds)
	try:
		try:
			collector._fetch_run_log_archive("owner/repo", 412, token="token", cache=cache)
			raise AssertionError("expected retry exhaustion failure")
		except Exception as exc:  # noqa: BLE001
			assert "502 Bad Gateway" in str(exc)
	finally:
		collector.gh_api_bytes = orig_gh_api_bytes
		collector.time.sleep = orig_sleep

	assert call_retries == [1, 1, 1]
	assert sleep_calls == [
		collector.LOG_ARCHIVE_FETCH_BACKOFF_SECONDS,
		collector.LOG_ARCHIVE_FETCH_BACKOFF_SECONDS * 2,
	]
	cached_error = cache[("owner/repo", 412)]
	assert isinstance(cached_error, Exception)
	assert "502 Bad Gateway" in str(cached_error)


def test_fetch_run_log_archive_non_retryable_failure_path():
	orig_gh_api_bytes = collector.gh_api_bytes
	call_retries: list[int] = []
	cache: dict[tuple[str, int], bytes | Exception] = {}

	def fake_gh_api_bytes(
		endpoint: str,
		*,
		token: str,
		retries: int = 3,
		backoff_seconds: float = 1.0,
	) -> bytes:
		_ = backoff_seconds
		assert endpoint == "repos/owner/repo/actions/runs/411/logs"
		assert token == "token"
		call_retries.append(retries)
		raise RuntimeError(
			"gh api failed for repos/owner/repo/actions/runs/411/logs (exit=1): HTTP 400 Bad Request"
		)

	collector.gh_api_bytes = fake_gh_api_bytes
	try:
		try:
			collector._fetch_run_log_archive("owner/repo", 411, token="token", cache=cache)
			raise AssertionError("expected non-retryable failure")
		except Exception as exc:  # noqa: BLE001
			assert "400 Bad Request" in str(exc)
			assert not str(exc).startswith("partial_data:missing_log_archive ")
	finally:
		collector.gh_api_bytes = orig_gh_api_bytes

	assert call_retries == [1]
	cached_error = cache[("owner/repo", 411)]
	assert isinstance(cached_error, Exception)
	assert "400 Bad Request" in str(cached_error)


def test_fetch_run_log_archive_empty_payload_classifies_missing_archive_soft_fail():
	orig_gh_api_bytes = collector.gh_api_bytes
	call_retries: list[int] = []
	cache: dict[tuple[str, int], bytes | Exception] = {}

	def fake_gh_api_bytes(
		endpoint: str,
		*,
		token: str,
		retries: int = 3,
		backoff_seconds: float = 1.0,
	) -> bytes:
		_ = backoff_seconds
		assert endpoint == "repos/owner/repo/actions/runs/413/logs"
		assert token == "token"
		call_retries.append(retries)
		return b""

	collector.gh_api_bytes = fake_gh_api_bytes
	try:
		try:
			collector._fetch_run_log_archive("owner/repo", 413, token="token", cache=cache)
			raise AssertionError("expected missing-archive soft-fail")
		except Exception as exc:  # noqa: BLE001
			message = str(exc)
			assert message.startswith("partial_data:missing_log_archive ")
			assert "repository=owner/repo" in message
			assert "run_id=413" in message
			assert "detail=empty_log_archive_payload" in message
	finally:
		collector.gh_api_bytes = orig_gh_api_bytes

	assert call_retries == [1]
	cached_error = cache[("owner/repo", 413)]
	assert isinstance(cached_error, Exception)
	assert str(cached_error).startswith("partial_data:missing_log_archive ")


def test_fetch_run_log_archive_sanitizes_missing_archive_detail_field():
	orig_gh_api_bytes = collector.gh_api_bytes
	cache: dict[tuple[str, int], bytes | Exception] = {}

	def fake_gh_api_bytes(
		endpoint: str,
		*,
		token: str,
		retries: int = 3,
		backoff_seconds: float = 1.0,
	) -> bytes:
		_ = token, retries, backoff_seconds
		assert endpoint == "repos/owner/repo/actions/runs/414/logs"
		raise RuntimeError(
			"gh api failed for repos/owner/repo/actions/runs/414/logs (exit=1): HTTP 404 Not Found detail=raw"
		)

	collector.gh_api_bytes = fake_gh_api_bytes
	try:
		try:
			collector._fetch_run_log_archive("owner/repo", 414, token="token", cache=cache)
			raise AssertionError("expected missing-archive soft-fail")
		except Exception as exc:  # noqa: BLE001
			message = str(exc)
			assert message.startswith("partial_data:missing_log_archive ")
			assert "run_id=414" in message
			detail_part = message.split("detail=", 1)[1]
			assert "=" not in detail_part
	finally:
		collector.gh_api_bytes = orig_gh_api_bytes


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
			"fail_logs_for": {
				"104": "gh api failed for repos/owner/repo/actions/runs/104/logs (exit=1): HTTP 404 Not Found",
			},
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
	fail_logs_for = store.get("fail_logs_for", {})
	if isinstance(fail_logs_for, dict):
		message = fail_logs_for.get(str(run_id))
		if message:
			print(message, file=sys.stderr)
			sys.exit(1)
	elif run_id in fail_logs_for:
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
				"AI_MEMORY_ENABLED": "false",
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
		assert report["scope"]["workflow_families"] == ["clarify", "implement", "plan", "validate"]
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
		log_errors = [item for item in report["errors"] if item["scope"] == "logs"]
		assert len(log_errors) == 1
		assert log_errors[0]["message"].startswith("partial_data:missing_log_archive ")
		assert "run_id=104" in log_errors[0]["message"]

		by_run_id = {item["run_id"]: item for item in report["runs"]}
		assert by_run_id[101]["log_excerpts"] == [{"step_name": "plan", "excerpt": "plan ok\n"}]
		assert by_run_id[102]["log_excerpts"] == [{"step_name": "failure", "excerpt": "failure details\n"}]
		assert "log_excerpts" not in by_run_id[104]
		assert "log_excerpts" not in by_run_id[103]


def test_main_log_output_dir_writes_categorized_full_logs_and_dedupes_downloads():
	with tempfile.TemporaryDirectory(prefix="collector-log-export-test-") as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir(parents=True)
		store_file = tmp / "gh_store.json"
		report_file = tmp / "report.json"
		log_output_dir = tmp / "log-output"

		store = {
			"runs_pages": {
				"1": [
					{
						"id": 201,
						"name": "AI Implement",
						"path": ".github/workflows/implement.yml",
						"status": "completed",
						"conclusion": "failure",
						"run_attempt": 1,
						"created_at": "2026-04-10T14:00:00Z",
						"run_started_at": "2026-04-10T14:00:00Z",
						"updated_at": "2026-04-10T14:06:40Z",
					},
					{
						"id": 202,
						"name": "AI Plan",
						"path": ".github/workflows/plan.yml",
						"status": "completed",
						"conclusion": "success",
						"run_attempt": 1,
						"created_at": "2026-04-10T13:00:00Z",
						"run_started_at": "2026-04-10T13:00:00Z",
						"updated_at": "2026-04-10T13:08:20Z",
					},
					{
						"id": 203,
						"name": "AI Clarify",
						"path": ".github/workflows/clarify.yml",
						"status": "completed",
						"conclusion": "success",
						"run_attempt": 1,
						"created_at": "2026-04-10T12:00:00Z",
						"run_started_at": "2026-04-10T12:00:00Z",
						"updated_at": "2026-04-10T12:01:40Z",
					},
					{
						"id": 204,
						"name": "AI Validate",
						"path": ".github/workflows/validate.yml",
						"status": "completed",
						"conclusion": "success",
						"run_attempt": 1,
						"created_at": "2026-04-10T11:00:00Z",
						"run_started_at": "2026-04-10T11:00:00Z",
						"updated_at": "2026-04-10T11:05:00Z",
					},
				],
				"2": [],
			},
			"jobs": {
				"201": {
					"1": [
						{
							"id": 9201,
							"name": "implement",
							"conclusion": "failure",
							"steps": [
								{"name": "setup", "conclusion": "success"},
								{"name": "run implement", "conclusion": "failure"},
							],
						}
					],
					"2": [],
				}
			},
			"log_files": {
				"201": {"01_failure.txt": "E" * 5000},
				"202": {"01_plan.txt": "plan full output\n"},
				"203": {"01_clarify.txt": "clarify full output\n"},
				"204": {"01_validate.txt": "validate full output\n"},
			},
			"log_call_counts": {},
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
	query = parse_qs(urlparse("https://api/" + endpoint).query)
	page = query.get("page", ["1"])[0]
	run_pages = store.get("jobs", {}).get(str(run_id), {})
	print(json.dumps({"jobs": run_pages.get(page, [])}))
	sys.exit(0)

m = re.search(r"/actions/runs/(\d+)/logs$", endpoint)
if m:
	run_id = int(m.group(1))
	counts = store.setdefault("log_call_counts", {})
	key = str(run_id)
	counts[key] = int(counts.get(key, 0)) + 1
	store_path.write_text(json.dumps(store), encoding="utf-8")
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
				"2",
				"--log-output-dir",
				str(log_output_dir),
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
		summary = json.loads((log_output_dir / "summary.json").read_text(encoding="utf-8"))
		assert summary == report
		assert report["scope"]["workflow_families"] == ["clarify", "implement", "plan", "validate"]
		assert report["summary"]["total_runs"] == 4

		by_run_id = {item["run_id"]: item for item in report["runs"]}
		assert len(by_run_id[201]["log_excerpts"][0]["excerpt"]) == collector.LOG_EXCERPT_MAX_CHARS
		assert by_run_id[201]["log_excerpts"][0]["excerpt"] == "E" * collector.LOG_EXCERPT_MAX_CHARS
		assert "log_excerpts" not in by_run_id[203]
		assert "log_excerpts" not in by_run_id[204]

		errors_dir = log_output_dir / "errors" / "owner_repo" / "implement" / "201"
		slow_impl_dir = log_output_dir / "slow" / "owner_repo" / "implement" / "201"
		slow_plan_dir = log_output_dir / "slow" / "owner_repo" / "plan" / "202"
		recent_impl_dir = log_output_dir / "recent" / "owner_repo" / "implement" / "201"
		recent_plan_dir = log_output_dir / "recent" / "owner_repo" / "plan" / "202"
		for run_dir in (errors_dir, slow_impl_dir, slow_plan_dir, recent_impl_dir, recent_plan_dir):
			assert (run_dir / "metadata.json").exists()

		assert (errors_dir / "step-001-failure.log").read_text(encoding="utf-8") == "E" * 5000
		assert (slow_plan_dir / "step-001-plan.log").read_text(encoding="utf-8") == "plan full output\n"

		assert not (log_output_dir / "errors" / "owner_repo" / "clarify" / "203").exists()
		assert not (log_output_dir / "recent" / "owner_repo" / "validate" / "204").exists()

		store_after = json.loads(store_file.read_text(encoding="utf-8"))
		assert store_after["log_call_counts"] == {"201": 1, "202": 1}


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
