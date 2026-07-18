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


def test_render_keeps_marker_block_open_across_excerpt_boundaries() -> None:
	os.environ["EVENTS_JSONL_ENABLED"] = "false"
	report = {
		"schema_version": renderer.COLLECTOR_SCHEMA_VERSION,
		"runs": [
			{
				"run_id": 401,
				"workflow_family": "implement",
				"log_excerpts": [
					{
						"step_name": "Agent",
						"excerpt": "2026-07-14T06:30:00Z >>> [model]",
					},
					{
						"step_name": "Agent",
						"excerpt": "\n".join(
							[
								"2026-07-14T06:30:01Z First line stays with the open block.",
								"2026-07-14T06:30:02Z Second line also stays with it.",
								"2026-07-14T06:30:03Z <<< [model]",
								"2026-07-14T06:30:04Z Response follows in the next block.",
							]
						),
					},
				],
			}
		],
	}

	with tempfile.TemporaryDirectory(prefix="scenario-trace-boundaries-") as td:
		report_path = Path(td) / "workflow_log_report.json"
		output_dir = Path(td) / "traces"
		_write_json(report_path, report)

		with _capture_std():
			written_paths = renderer.render(None, report_path, output_dir)

		assert [path.name for path in written_paths] == ["401.scenario.json"]
		trace = _read_json(output_dir / "401.scenario.json")
		assert trace["steps"] == [
			{
				"type": "user_message",
				"ts": "2026-07-14T06:30:00Z",
				"content": "First line stays with the open block.\nSecond line also stays with it.",
				"tokens": None,
			},
			{
				"type": "assistant_text",
				"ts": "2026-07-14T06:30:03Z",
				"content": "Response follows in the next block.",
				"tokens": None,
			},
		]


def test_helper_parsers_cover_edge_branches() -> None:
	assert renderer._normalize_excerpts("not-a-list") == []
	assert renderer._normalize_excerpts(
		[
			"skip",
			{},
			{"step_name": "Agent", "excerpt": ""},
			{"step_name": "Agent", "excerpt": "kept excerpt"},
		]
	) == [{"step_name": "Agent", "excerpt": "kept excerpt"}]
	assert renderer._split_timestamp("plain line") == (None, "plain line")
	assert renderer._detect_marker("[tool_result]  kept output") == ("tool_result", "kept output")

	name, args = renderer._parse_tool_call_payload('{"name": "apply_patch", "input": "patch"}')
	assert name == "apply_patch"
	assert args == {"input": "patch"}

	name, args = renderer._parse_tool_call_payload("run_shell")
	assert name == "run_shell"
	assert args == {}

	name, args = renderer._parse_tool_call_payload("run_shell first line\nsecond line")
	assert name == "run_shell"
	assert args == "first line\nsecond line"

	assert renderer._finalize_block(None) is None
	assert (
		renderer._finalize_block(
			renderer._Block(kind="assistant_text", ts="2026-07-15T00:00:00Z", lines=[])
		)
		is None
	)
	assert (
		renderer._finalize_block(
			renderer._Block(kind="unknown", ts="2026-07-15T00:00:00Z", lines=["ignored"])
		)
		is None
	)

	try:
		renderer._parse_steps(
			[
				{
					"step_name": "Agent",
					"excerpt": "2026-07-15T00:00:00Z >>> [model]",
				}
			]
		)
	except renderer.TraceParseError as exc:
		assert str(exc) == "marked excerpts did not yield any scenario steps"
	else:
		raise AssertionError("expected marked excerpts without content to fail open")


def test_build_tool_result_step_covers_fallback_shapes() -> None:
	ts = "2026-07-15T00:00:00Z"

	assert renderer._build_tool_result_step(renderer._Block(kind="tool_result", ts=ts, lines=[])) == {
		"type": "tool_result",
		"ts": ts,
		"output": "",
		"exit_status": None,
	}
	assert renderer._build_tool_result_step(
		renderer._Block(
			kind="tool_result",
			ts=ts,
			lines=['{"result": "ok", "exit_status": "7"}'],
		)
	) == {
		"type": "tool_result",
		"ts": ts,
		"output": "ok",
		"exit_status": 7,
	}
	assert renderer._build_tool_result_step(
		renderer._Block(kind="tool_result", ts=ts, lines=['{"alpha": 1, "beta": 2}'])
	) == {
		"type": "tool_result",
		"ts": ts,
		"output": '{"alpha": 1, "beta": 2}',
		"exit_status": None,
	}
	assert renderer._build_tool_result_step(
		renderer._Block(kind="tool_result", ts=ts, lines=["exit_status=-3 partial output", "tail line"])
	) == {
		"type": "tool_result",
		"ts": ts,
		"output": "partial output\ntail line",
		"exit_status": -3,
	}
	assert renderer._build_tool_result_step(
		renderer._Block(kind="tool_result", ts=ts, lines=["plain text fallback"])
	) == {
		"type": "tool_result",
		"ts": ts,
		"output": "plain text fallback",
		"exit_status": None,
	}


def test_render_fail_open_for_report_shape_and_write_failures() -> None:
	os.environ["EVENTS_JSONL_ENABLED"] = "false"
	with tempfile.TemporaryDirectory(prefix="scenario-trace-errors-") as td:
		report_path = Path(td) / "workflow_log_report.json"
		output_dir = Path(td) / "traces"

		_write_json(report_path, [])
		with _capture_std() as (out, err):
			written_paths = renderer.render(None, report_path, output_dir)
		assert written_paths == []
		assert err.getvalue() == ""
		assert (
			"collector_report_load_failed:collector report root is not a JSON object" in out.getvalue()
		)

		_write_json(report_path, {"schema_version": renderer.COLLECTOR_SCHEMA_VERSION, "runs": {}})
		with _capture_std() as (out, err):
			written_paths = renderer.render(None, report_path, output_dir)
		assert written_paths == []
		assert err.getvalue() == ""
		assert "reason=collector_report_missing_runs_array" in out.getvalue()

		report = {
			"schema_version": renderer.COLLECTOR_SCHEMA_VERSION,
			"runs": [
				"skip non-dict run",
				{"run_id": 10, "workflow_family": "plan", "log_excerpts": "not-a-list"},
				{
					"workflow_family": "plan",
					"log_excerpts": [
						{
							"step_name": "Agent",
							"excerpt": "2026-07-15T00:01:00Z >>> [model]\n2026-07-15T00:01:01Z Missing run id.",
						}
					],
				},
				{
					"run_id": 12,
					"log_excerpts": [
						{
							"step_name": "Agent",
							"excerpt": "2026-07-15T00:02:00Z >>> [model]\n2026-07-15T00:02:01Z Missing phase.",
						}
					],
				},
				{
					"run_id": 13,
					"workflow_family": "implement",
					"log_excerpts": [
						{
							"step_name": "Agent",
							"excerpt": "2026-07-15T00:03:00Z <<< [model]\n2026-07-15T00:03:01Z Write me.",
						}
					],
				},
			],
		}
		_write_json(report_path, report)

		original_write_trace = renderer._write_trace

		def _failing_write_trace(path: Path, payload: dict[str, object]) -> None:
			raise OSError("disk full")

		renderer._write_trace = _failing_write_trace
		try:
			with _capture_std() as (out, err):
				written_paths = renderer.render(None, report_path, output_dir)
		finally:
			renderer._write_trace = original_write_trace

		assert written_paths == []
		assert err.getvalue() == ""
		stdout = out.getvalue()
		assert (
			"WORKFLOW_SCENARIO_TRACE_PARSE_FAIL: run_id=unknown phase=plan reason=missing_run_id_or_workflow_family"
			in stdout
		)
		assert (
			"WORKFLOW_SCENARIO_TRACE_PARSE_FAIL: run_id=12 phase=unknown reason=missing_run_id_or_workflow_family"
			in stdout
		)
		assert (
			"WORKFLOW_SCENARIO_TRACE_PARSE_FAIL: run_id=13 phase=implement reason=trace_write_failed:disk full"
			in stdout
		)


def main() -> int:
	test_render_writes_trace_with_all_step_types()
	test_main_fail_open_on_malformed_run_and_keeps_parseable_run()
	test_render_honors_run_id_filter_and_schema_version_stamp()
	test_render_keeps_marker_block_open_across_excerpt_boundaries()
	test_helper_parsers_cover_edge_branches()
	test_build_tool_result_step_covers_fallback_shapes()
	test_render_fail_open_for_report_shape_and_write_failures()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
