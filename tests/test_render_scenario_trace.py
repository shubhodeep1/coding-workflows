#!/usr/bin/env python3
"""Tests for scripts/render_scenario_trace.py."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RENDERER_PATH = REPO_ROOT / "scripts" / "render_scenario_trace.py"
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))


spec = importlib.util.spec_from_file_location("render_scenario_trace", RENDERER_PATH)
assert spec is not None and spec.loader is not None
renderer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = renderer
spec.loader.exec_module(renderer)


@contextlib.contextmanager
def _capture_std():
	out = io.StringIO()
	err = io.StringIO()
	with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
		yield out, err


def _write_json(path: Path, payload: object) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(payload), encoding="utf-8")


def _read_json(path: Path) -> dict:
	return json.loads(path.read_text(encoding="utf-8"))


def test_render_writes_trace_with_all_step_types() -> None:
	os.environ["EVENTS_JSONL_ENABLED"] = "false"
	report = {
		"schema_version": renderer.COLLECTOR_SCHEMA_VERSION,
		"runs": [
			{
				"run_id": 123,
				"workflow_family": "implement",
				"log_excerpts": [
					{
						"step_name": "Agent",
						"excerpt": "\n".join(
							[
								"2026-07-14T06:00:00Z >>> [model]",
								"2026-07-14T06:00:01Z Please update the workflow.",
								"2026-07-14T06:00:02Z Include the new trace step.",
								"2026-07-14T06:00:03Z <<< [model]",
								"2026-07-14T06:00:04Z I will add the trace renderer.",
								"2026-07-14T06:00:05Z [tool_call] apply_patch {\"input\": \"*** Begin Patch\\n*** End Patch\"}",
								"2026-07-14T06:00:06Z [tool_result] {\"output\": \"Done!\", \"exit_status\": 0}",
							]
						),
					}
				],
			}
		],
	}

	with tempfile.TemporaryDirectory(prefix="scenario-trace-happy-") as td:
		report_path = Path(td) / "workflow_log_report.json"
		output_dir = Path(td) / "traces"
		_write_json(report_path, report)

		with _capture_std():
			written_paths = renderer.render(None, report_path, output_dir)

		assert [path.name for path in written_paths] == ["123.scenario.json"]
		trace = _read_json(output_dir / "123.scenario.json")
		assert trace["schema_version"] == renderer.SCHEMA_VERSION
		assert trace["run_id"] == 123
		assert trace["phase"] == "implement"
		assert [step["type"] for step in trace["steps"]] == [
			"user_message",
			"assistant_text",
			"tool_call",
			"tool_result",
		]
		assert trace["steps"][0]["content"] == "Please update the workflow.\nInclude the new trace step."
		assert trace["steps"][0]["tokens"] is None
		assert trace["steps"][1]["content"] == "I will add the trace renderer."
		assert trace["steps"][2]["name"] == "apply_patch"
		assert trace["steps"][2]["args"] == {"input": "*** Begin Patch\n*** End Patch"}
		assert trace["steps"][3]["output"] == "Done!"
		assert trace["steps"][3]["exit_status"] == 0


def test_main_fail_open_on_malformed_run_and_keeps_parseable_run() -> None:
	os.environ["EVENTS_JSONL_ENABLED"] = "false"
	report = {
		"schema_version": renderer.COLLECTOR_SCHEMA_VERSION,
		"runs": [
			{
				"run_id": 100,
				"workflow_family": "plan",
				"log_excerpts": [
					{
						"step_name": "Agent",
						"excerpt": "\n".join(
							[
								"2026-07-14T06:10:00Z >>> [model]",
								"2026-07-14T06:10:01Z Summarize the plan.",
								"2026-07-14T06:10:02Z <<< [model]",
								"2026-07-14T06:10:03Z Plan summary ready.",
							]
						),
					}
				],
			},
			{
				"run_id": 200,
				"workflow_family": "implement",
				"log_excerpts": [
					{
						"step_name": "Agent",
						"excerpt": "2026-07-14T06:11:00Z [tool_call] apply_patch {not-json",
					}
				],
			},
			{
				"run_id": 300,
				"workflow_family": "implement",
				"log_excerpts": [
					{
						"step_name": "Agent",
						"excerpt": "2026-07-14T06:12:00Z [tool_call]",
					}
				],
			},
			{
				"run_id": 400,
				"workflow_family": "implement",
				"log_excerpts": [
					{
						"step_name": "Agent",
						"excerpt": "2026-07-14T06:13:00Z unmarked log line",
					}
				],
			},
		],
	}

	with tempfile.TemporaryDirectory(prefix="scenario-trace-fail-open-") as td:
		report_path = Path(td) / "workflow_log_report.json"
		output_dir = Path(td) / "traces"
		_write_json(report_path, report)

		with _capture_std() as (out, err):
			rc = renderer.main(["--input", str(report_path), "--output-dir", str(output_dir)])

		assert rc == 0
		assert err.getvalue() == ""
		stdout = out.getvalue()
		assert "WORKFLOW_SCENARIO_TRACE_WRITTEN: run_id=100 phase=plan" in stdout
		assert "WORKFLOW_SCENARIO_TRACE_PARSE_FAIL: run_id=200 phase=implement reason=tool_call args is not valid JSON" in stdout
		assert "WORKFLOW_SCENARIO_TRACE_PARSE_FAIL: run_id=300 phase=implement reason=tool_call payload is empty" in stdout
		assert "WORKFLOW_SCENARIO_TRACE_PARSE_FAIL: run_id=400 phase=implement reason=log excerpts did not contain any supported scenario markers" in stdout
		assert (output_dir / "100.scenario.json").is_file()
		assert not (output_dir / "200.scenario.json").exists()
		assert not (output_dir / "300.scenario.json").exists()
		assert not (output_dir / "400.scenario.json").exists()


def test_render_honors_run_id_filter_and_schema_version_stamp() -> None:
	os.environ["EVENTS_JSONL_ENABLED"] = "false"
	report = {
		"schema_version": renderer.COLLECTOR_SCHEMA_VERSION,
		"runs": [
			{
				"run_id": 301,
				"workflow_family": "clarify",
				"log_excerpts": [
					{
						"step_name": "Agent",
						"excerpt": "2026-07-14T06:20:00Z >>> [model]\n2026-07-14T06:20:01Z Clarify the issue.",
					}
				],
			},
			{
				"run_id": 302,
				"workflow_family": "review_autofix",
				"log_excerpts": [
					{
						"step_name": "Agent",
						"excerpt": "2026-07-14T06:21:00Z <<< [model]\n2026-07-14T06:21:01Z Reviewer note.",
					}
				],
			},
		],
	}

	with tempfile.TemporaryDirectory(prefix="scenario-trace-filter-") as td:
		report_path = Path(td) / "workflow_log_report.json"
		output_dir = Path(td) / "traces"
		_write_json(report_path, report)

		with _capture_std():
			written_paths = renderer.render("302", report_path, output_dir)

		assert [path.name for path in written_paths] == ["302.scenario.json"]
		assert not (output_dir / "301.scenario.json").exists()
		trace = _read_json(output_dir / "302.scenario.json")
		assert trace["schema_version"] == renderer.SCHEMA_VERSION
		assert trace["steps"] == [
			{
				"type": "assistant_text",
				"ts": "2026-07-14T06:21:00Z",
				"content": "Reviewer note.",
				"tokens": None,
			}
		]


def main() -> int:
	test_render_writes_trace_with_all_step_types()
	test_main_fail_open_on_malformed_run_and_keeps_parseable_run()
	test_render_honors_run_id_filter_and_schema_version_stamp()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
