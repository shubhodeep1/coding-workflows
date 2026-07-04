#!/usr/bin/env python3
"""Focused tests for scripts/workflow_retro.py."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "workflow_retro.py"
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("workflow_retro", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
workflow_retro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow_retro)


@contextlib.contextmanager
def _patched_module_attrs(module, **replacements):
	originals = {name: getattr(module, name) for name in replacements}
	try:
		for name, value in replacements.items():
			setattr(module, name, value)
		yield
	finally:
		for name, value in originals.items():
			setattr(module, name, value)


@contextlib.contextmanager
def _patched_path_read_text(replacement):
	original = Path.read_text
	Path.read_text = replacement
	try:
		yield
	finally:
		Path.read_text = original


def _write_json(path: Path, payload: object) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(payload), encoding="utf-8")


def _cost_telemetry(
	*,
	or_prompt_tokens: int,
	or_completion_tokens: int,
	or_total_tokens: int,
	or_cache_write_tokens: int,
	or_cache_read_tokens: int,
	or_calls: int,
) -> dict[str, object]:
	return {
		"log_parsed": True,
		"or_prompt_tokens": or_prompt_tokens,
		"or_completion_tokens": or_completion_tokens,
		"or_total_tokens": or_total_tokens,
		"or_cache_write_tokens": or_cache_write_tokens,
		"or_cache_read_tokens": or_cache_read_tokens,
		"or_calls": or_calls,
		"break_glass_count": 0,
		"context_budget_warn_count": 0,
		"wall_clock_p50_ms": 1200,
		"wall_clock_p99_ms": 1200,
	}


def test_main_writes_deterministic_weekly_retro_context() -> None:
	report = {
		"runs": [
			{
				"repository": "owner/repo",
				"run_id": 101,
				"workflow_name": "AI Review",
				"workflow_family": "review_autofix",
				"conclusion": "success",
				"duration_seconds": 120,
				"created_at": "2026-06-22T11:00:00Z",
				"log_summary": "LABEL_REPAIR issue=41 issue_state=open pr_state=open pr_merged=false",
				"cost_telemetry": _cost_telemetry(
					or_prompt_tokens=60,
					or_completion_tokens=20,
					or_total_tokens=80,
					or_cache_write_tokens=20,
					or_cache_read_tokens=20,
					or_calls=1,
				),
			},
			{
				"repository": "owner/repo",
				"run_id": 102,
				"workflow_name": "AI Orchestrate Poller",
				"workflow_family": "orchestrate_poll",
				"conclusion": "failure",
				"duration_seconds": 300,
				"created_at": "2026-06-23T10:00:00Z",
				"log_excerpts": [
					{
						"step_name": "repair",
						"excerpt": "::warning::LABEL_REPAIR issue=42 failed: mock failure",
					}
				],
				"cost_telemetry": _cost_telemetry(
					or_prompt_tokens=40,
					or_completion_tokens=30,
					or_total_tokens=70,
					or_cache_write_tokens=0,
					or_cache_read_tokens=20,
					or_calls=1,
				),
			},
			{
				"repository": "other/repo",
				"run_id": 999,
				"workflow_name": "Ignored",
				"workflow_family": "review_autofix",
				"conclusion": "success",
				"duration_seconds": 10,
				"created_at": "2026-06-24T10:00:00Z",
			},
		],
		"summary": {},
		"scope": {"repositories": ["owner/repo"]},
		"errors": [],
	}

	merged_prs = [
		{
			"number": 11,
			"title": "Retro PR one",
			"url": "https://github.com/owner/repo/pull/11",
			"merged_at": "2026-06-22T12:00:00Z",
		},
		{
			"number": 12,
			"title": "Retro PR two",
			"url": "https://github.com/owner/repo/pull/12",
			"merged_at": "2026-06-24T12:00:00Z",
		},
	]

	run_events = [
		{
			"workflow": "review_autofix",
			"event_type": "phase_started",
			"pr_number": 11,
			"run_id": "review-1",
			"timestamp": "2026-06-22T12:30:00Z",
		},
		{
			"workflow": "review_autofix",
			"event_type": "phase_started",
			"pr_number": 11,
			"run_id": "review-2",
			"timestamp": "2026-06-22T13:00:00Z",
		},
		{
			"workflow": "review_autofix",
			"event_type": "phase_started",
			"pr_number": 12,
			"run_id": "review-3",
			"timestamp": "2026-06-24T13:00:00Z",
		},
	]

	def _fake_fetch_merged_pull_requests(*, repo: str, since_utc, until_utc):
		assert repo == "owner/repo"
		assert since_utc.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-06-19T09:00:00Z"
		assert until_utc.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-06-26T09:00:00Z"
		return merged_prs

	def _fake_load_ai_memory_run_events(*, repo_root: Path, since_utc, until_utc, memory_branch: str, memory_root_relative: str):
		assert repo_root == REPO_ROOT
		assert memory_branch == "ai-memory"
		assert memory_root_relative == "ai-memory"
		return run_events
	_fake_load_ai_memory_run_events.last_diagnostics = {"json_decode_errors": 1, "non_dict_payloads": 0}

	with tempfile.TemporaryDirectory(prefix="workflow-retro-test-") as td:
		td_path = Path(td)
		report_path = td_path / "workflow_log_report.json"
		markdown_path = td_path / "retro.md"
		json_path = td_path / "retro.json"
		_write_json(report_path, report)

		with _patched_module_attrs(
			workflow_retro,
			fetch_merged_pull_requests=_fake_fetch_merged_pull_requests,
			load_ai_memory_run_events=_fake_load_ai_memory_run_events,
		):
			rc = workflow_retro.main(
				[
					"--report",
					str(report_path),
					"--output",
					str(markdown_path),
					"--json-output",
					str(json_path),
					"--repo",
					"owner/repo",
					"--repo-root",
					str(REPO_ROOT),
					"--lookback-days",
					"7",
					"--now",
					"2026-06-26T09:00:00Z",
				]
			)

		assert rc == 0
		payload = json.loads(json_path.read_text(encoding="utf-8"))
		markdown = markdown_path.read_text(encoding="utf-8")

	assert payload["schema_version"] == "workflow_retro.v1"
	assert payload["has_activity"] is True
	assert payload["generated_at"] == "2026-06-26T09:00:00Z"
	assert payload["repository"] == "owner/repo"
	assert payload["window"]["week_label"] == "2026-W26"
	assert payload["summary"]["total_runs"] == 2
	assert payload["summary"]["failure_count"] == 1
	assert payload["summary"]["merged_pr_count"] == 2
	assert payload["review_autofix"]["counted_prs"] == 2
	assert payload["review_autofix"]["median_iterations"] == 1.5
	assert payload["judge_cycle_proxy"]["count"] == 1
	assert payload["cost_telemetry"]["or_total_tokens"] == 150
	assert payload["cost_telemetry"]["cache_hit_rate"] == 0.25
	assert payload["warnings"] == [
		"AI memory run ledger skipped malformed entries (json_decode_errors=1, non_dict_payloads=0)."
	]
	assert {item["bucket"] for item in payload["stall_reasons"]} == {
		"label_repair_failed",
		"open_pr_label_repair",
	}
	assert "# Weekly Workflow Retro Context" in markdown
	assert "## Snapshot" in markdown
	assert "## Stall Signals" in markdown
	assert "Warning: AI memory run ledger skipped malformed entries" in markdown
	assert "`2026-W26`" in markdown


def test_main_fails_open_on_pr_and_ai_memory_reads() -> None:
	report = {
		"runs": [
			{
				"repository": "owner/repo",
				"run_id": 201,
				"workflow_name": "AI Review",
				"workflow_family": "review_autofix",
				"conclusion": "success",
				"duration_seconds": 90,
				"created_at": "2026-06-22T11:00:00Z",
			}
		],
		"summary": {},
		"scope": {"repositories": ["owner/repo"]},
		"errors": [],
	}

	with tempfile.TemporaryDirectory(prefix="workflow-retro-fail-open-") as td:
		td_path = Path(td)
		report_path = td_path / "workflow_log_report.json"
		markdown_path = td_path / "retro.md"
		json_path = td_path / "retro.json"
		_write_json(report_path, report)

		with _patched_module_attrs(
			workflow_retro,
			fetch_merged_pull_requests=lambda **_: (_ for _ in ()).throw(RuntimeError("gh unavailable")),
			load_ai_memory_run_events=lambda **_: (_ for _ in ()).throw(RuntimeError("missing ai-memory branch")),
		):
			rc = workflow_retro.main(
				[
					"--report",
					str(report_path),
					"--output",
					str(markdown_path),
					"--json-output",
					str(json_path),
					"--repo",
					"owner/repo",
					"--repo-root",
					str(REPO_ROOT),
					"--lookback-days",
					"7",
					"--now",
					"2026-06-26T09:00:00Z",
				]
			)

		assert rc == 0
		payload = json.loads(json_path.read_text(encoding="utf-8"))
		markdown = markdown_path.read_text(encoding="utf-8")

	assert payload["summary"]["merged_pr_count"] == 0
	assert len(payload["warnings"]) == 2
	assert "Merged PR query failed open" in payload["warnings"][0]
	assert "AI memory read failed open" in payload["warnings"][1]
	assert "Warning: Merged PR query failed open" in markdown
	assert "Warning: AI memory read failed open" in markdown


def test_fetch_merged_pull_requests_extends_search_end_day_but_post_filters_exact_window() -> None:
	captured: dict[str, str] = {}

	def _fake_gh_json(args, *, stdin_text=None):
		assert stdin_text is None
		for idx, item in enumerate(args):
			if item == "-f" and idx + 1 < len(args) and args[idx + 1].startswith("searchQuery="):
				captured["search_query"] = args[idx + 1].split("=", 1)[1]
		return {
			"data": {
				"search": {
					"pageInfo": {"hasNextPage": False, "endCursor": None},
					"nodes": [
						{
							"number": 11,
							"title": "Within window",
							"url": "https://github.com/owner/repo/pull/11",
							"mergedAt": "2026-06-26T08:59:59Z",
						},
						{
							"number": 12,
							"title": "After window",
							"url": "https://github.com/owner/repo/pull/12",
							"mergedAt": "2026-06-26T09:00:01Z",
						},
					],
				}
			}
		}

	with _patched_module_attrs(workflow_retro, _gh_json=_fake_gh_json):
		results = workflow_retro.fetch_merged_pull_requests(
			repo="owner/repo",
			since_utc=workflow_retro._parse_iso8601("2026-06-19T09:00:00Z"),
			until_utc=workflow_retro._parse_iso8601("2026-06-26T09:00:00Z"),
		)

	assert captured["search_query"] == "repo:owner/repo is:pr is:merged merged:2026-06-19..2026-06-27 sort:updated-desc"
	assert [item["number"] for item in results] == [11]


def test_load_ai_memory_run_events_streams_ledger_files() -> None:
	with tempfile.TemporaryDirectory(prefix="workflow-retro-ledger-") as td:
		branch_dir = Path(td) / "branch-root"
		memory_root = branch_dir / "ai-memory"
		ledger_path = memory_root / "runs" / "run-1" / "ledger" / "events.jsonl"
		ledger_path.parent.mkdir(parents=True, exist_ok=True)
		ledger_path.write_text(
			'\n'.join(
				[
					'{"timestamp":"2026-06-22T12:30:00Z","run_id":"review-1","workflow":"review_autofix","event_type":"phase_started","pr_number":11}',
					'{not-json',
					'[]',
					'{"timestamp":"2026-06-28T12:30:00Z","run_id":"review-2"}',
				]
			)
			+ '\n',
			encoding="utf-8",
		)

		original_read_text = Path.read_text

		def _guarded_read_text(self, *args, **kwargs):
			if self == ledger_path:
				raise AssertionError("load_ai_memory_run_events should stream ledger files")
			return original_read_text(self, *args, **kwargs)

		with _patched_module_attrs(
			workflow_retro,
			read_memory_root_from_branch=lambda *args, **kwargs: branch_dir,
			resolve_memory_root_dir=lambda *args, **kwargs: memory_root,
		):
			with _patched_path_read_text(_guarded_read_text):
					events = workflow_retro.load_ai_memory_run_events(
						repo_root=REPO_ROOT,
						since_utc=workflow_retro._parse_iso8601("2026-06-19T09:00:00Z"),
						until_utc=workflow_retro._parse_iso8601("2026-06-26T09:00:00Z"),
						memory_branch="ai-memory",
						memory_root_relative="ai-memory",
					)

	assert [event["run_id"] for event in events] == ["review-1"]
	assert getattr(workflow_retro.load_ai_memory_run_events, "last_diagnostics") == {
		"json_decode_errors": 1,
		"non_dict_payloads": 1,
	}


def test_build_weekly_retro_payload_truncates_prompt_facing_lists() -> None:
	total_merged_prs = workflow_retro.MERGED_PR_LIST_LIMIT + 25
	reviewed_prs = workflow_retro.REVIEW_ITERATION_LIST_LIMIT + 2
	report = {
		"runs": [
			{
				"repository": "owner/repo",
				"run_id": idx + 1,
				"workflow_name": f"Workflow {idx + 1}",
				"workflow_family": f"family-{idx + 1:02d}",
				"conclusion": "success" if idx % 2 == 0 else "failure",
				"duration_seconds": 60 + idx,
				"created_at": "2026-06-22T11:00:00Z",
			}
			for idx in range(workflow_retro.WORKFLOW_FAMILY_LIMIT + 3)
		],
		"summary": {},
		"scope": {"repositories": ["owner/repo"]},
		"errors": [],
	}
	merged_prs = [
		{
			"number": idx + 1,
			"title": f"Retro PR {idx + 1}",
			"url": f"https://github.com/owner/repo/pull/{idx + 1}",
			"merged_at": "2026-06-22T12:00:00Z",
		}
		for idx in range(total_merged_prs)
	]
	run_events = [
		{
			"workflow": "review_autofix",
			"event_type": "phase_started",
			"pr_number": idx + 1,
			"run_id": f"review-{idx + 1}",
			"timestamp": "2026-06-22T12:30:00Z",
		}
		for idx in range(reviewed_prs)
	]

	def _fake_fetch_merged_pull_requests(*, repo: str, since_utc, until_utc):
		assert repo == "owner/repo"
		return merged_prs

	def _fake_load_ai_memory_run_events(*, repo_root: Path, since_utc, until_utc, memory_branch: str, memory_root_relative: str):
		assert repo_root == REPO_ROOT
		return run_events

	with _patched_module_attrs(
		workflow_retro,
		fetch_merged_pull_requests=_fake_fetch_merged_pull_requests,
		load_ai_memory_run_events=_fake_load_ai_memory_run_events,
	):
		payload = workflow_retro.build_weekly_retro_payload(
			report,
			repo="owner/repo",
			since_utc=workflow_retro._parse_iso8601("2026-06-19T09:00:00Z"),
			until_utc=workflow_retro._parse_iso8601("2026-06-26T09:00:00Z"),
			repo_root=REPO_ROOT,
			memory_branch="ai-memory",
			memory_root_relative="ai-memory",
		)

	bounds = payload["bounded_lists"]
	assert payload["summary"]["merged_pr_count"] == total_merged_prs
	assert len(payload["merged_prs"]) == workflow_retro.MERGED_PR_LIST_LIMIT
	assert bounds["merged_prs"]["omitted"] == total_merged_prs - workflow_retro.MERGED_PR_LIST_LIMIT
	assert payload["review_autofix"]["counted_prs"] == reviewed_prs
	assert len(payload["review_autofix"]["by_pr"]) == workflow_retro.REVIEW_ITERATION_LIST_LIMIT
	assert bounds["review_autofix_by_pr"]["omitted"] == reviewed_prs - workflow_retro.REVIEW_ITERATION_LIST_LIMIT
	assert len(payload["review_autofix"]["merged_prs_without_review_data"]) == workflow_retro.REVIEW_MISSING_PR_LIST_LIMIT
	assert bounds["review_autofix_missing_prs"]["omitted"] == 3
	assert len(payload["workflow_families"]) == workflow_retro.WORKFLOW_FAMILY_LIMIT
	assert bounds["workflow_families"]["omitted"] == 3
	assert any("Truncated merged_prs JSON list" in item for item in payload["data_gaps"])
	assert any("Truncated review_autofix.by_pr JSON list" in item for item in payload["data_gaps"])
	assert any("Truncated merged_prs_without_review_data JSON list" in item for item in payload["data_gaps"])
	assert any("Truncated workflow_families JSON list" in item for item in payload["data_gaps"])

	markdown = workflow_retro.render_retro_context_markdown(payload)
	assert "additional merged PR(s) omitted for brevity." in markdown
	assert "additional PR iteration row(s) omitted for brevity." in markdown
	assert "additional merged PR(s) without review_autofix data omitted for brevity." in markdown
	assert "additional workflow family row(s) omitted for brevity." in markdown


def test_build_weekly_retro_payload_flags_zero_activity_week() -> None:
	report = {
		"runs": [],
		"summary": {},
		"scope": {"repositories": ["owner/repo"]},
		"errors": [],
	}

	def _fake_fetch_merged_pull_requests(*, repo: str, since_utc, until_utc):
		return []

	def _fake_load_ai_memory_run_events(*, repo_root: Path, since_utc, until_utc, memory_branch: str, memory_root_relative: str):
		return []

	with _patched_module_attrs(
		workflow_retro,
		fetch_merged_pull_requests=_fake_fetch_merged_pull_requests,
		load_ai_memory_run_events=_fake_load_ai_memory_run_events,
	):
		payload = workflow_retro.build_weekly_retro_payload(
			report,
			repo="owner/repo",
			since_utc=workflow_retro._parse_iso8601("2026-06-19T09:00:00Z"),
			until_utc=workflow_retro._parse_iso8601("2026-06-26T09:00:00Z"),
			repo_root=REPO_ROOT,
			memory_branch="ai-memory",
			memory_root_relative="ai-memory",
		)

	assert payload["has_activity"] is False
	assert payload["summary"]["total_runs"] == 0
	assert payload["summary"]["merged_pr_count"] == 0


def main() -> int:
	test_main_writes_deterministic_weekly_retro_context()
	test_main_fails_open_on_pr_and_ai_memory_reads()
	test_fetch_merged_pull_requests_extends_search_end_day_but_post_filters_exact_window()
	test_load_ai_memory_run_events_streams_ledger_files()
	test_build_weekly_retro_payload_truncates_prompt_facing_lists()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
