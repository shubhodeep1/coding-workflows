#!/usr/bin/env python3
"""Tests for scripts/analyze_workflow_logs.py.

The analyzer now does context preparation only — the legacy OpenRouter
sync/batch pathway has been removed. These tests cover:
  * argparse surface (including the deprecated --codex-mode no-op alias)
  * load_input_data validation
  * prepare_analysis_context: caps lifted to 100/250 and the
    `deep_dive_logs` field intentionally excluded
  * resolve_dated_output_path collision handling
  * main() writes analysis_context.json and prints its path

The file is runnable two ways:
  * via pytest (for local dev): `python3 -m pytest tests/test_analyze_workflow_logs.py`
  * as a plain script (for CI's coverage gate at `.github/workflows/ci.yml:199`):
    `python3 -m coverage run tests/test_analyze_workflow_logs.py`

CI installs `yamllint coverage pyyaml jsonschema jinja2` only — no pytest —
so the script-mode entry point at the bottom must NOT depend on pytest.
That's why the test bodies use plain `try/except`/`io.StringIO` instead
of `pytest.raises`/`capsys` fixtures.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from datetime import date
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_PATH = REPO_ROOT / "scripts" / "analyze_workflow_logs.py"
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))


spec = importlib.util.spec_from_file_location("analyze_workflow_logs", ANALYZER_PATH)
assert spec is not None and spec.loader is not None
analyzer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyzer)


def _write_json(path: Path, payload: object) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(payload), encoding="utf-8")


@contextlib.contextmanager
def _capture_std():
	"""Replacement for pytest's capsys fixture; works without pytest installed."""
	out = io.StringIO()
	err = io.StringIO()
	with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
		yield out, err


def _assert_raises(exc_type: type[BaseException], func, *args, **kwargs) -> BaseException:
	"""Replacement for pytest.raises; returns the raised exception for further checks."""
	try:
		func(*args, **kwargs)
	except exc_type as exc:
		return exc
	raise AssertionError(f"expected {exc_type.__name__} but no exception was raised")


# ---------- argparse surface ------------------------------------------------


def test_build_parser_codex_mode_default_false():
	args = analyzer.build_parser().parse_args([])
	assert args.codex_mode is False


def test_build_parser_accepts_codex_mode_flag():
	args = analyzer.build_parser().parse_args(["--codex-mode"])
	assert args.codex_mode is True


def test_build_parser_accepts_input_and_output():
	args = analyzer.build_parser().parse_args(
		["--input", "in.json", "--output", "out.md"]
	)
	assert args.input == "in.json"
	assert args.output == "out.md"


def test_build_parser_rejects_legacy_batch_flags():
	parser = analyzer.build_parser()
	# argparse calls sys.exit(2) with stderr noise on unknown args; suppress
	# the noise so the script-mode test runner output stays clean.
	with _capture_std():
		_assert_raises(SystemExit, parser.parse_args, ["--prompt-token-budget", "24000"])
		_assert_raises(SystemExit, parser.parse_args, ["--batch-mode", "auto"])


# ---------- load_input_data ------------------------------------------------


def test_load_input_data_rejects_malformed_json_input():
	with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
		tmp.write("{not-json")
		path = tmp.name
	try:
		args = analyzer.build_parser().parse_args(["--input", path])
		_assert_raises(ValueError, analyzer.load_input_data, args)
	finally:
		Path(path).unlink(missing_ok=True)


def test_load_input_data_collector_report_round_trips_runs():
	report = {
		"runs": [
			{"repository": "o/r", "run_id": 1, "conclusion": "failure", "duration_seconds": 90},
			{"repository": "o/r", "run_id": 2, "conclusion": "success", "duration_seconds": 30},
		],
		"summary": {"total_runs": 2},
		"errors": [],
		"scope": {"repositories": ["o/r"]},
	}
	with tempfile.TemporaryDirectory(prefix="analyze-input-") as td:
		path = Path(td) / "workflow_log_report.json"
		_write_json(path, report)
		args = analyzer.build_parser().parse_args(["--input", str(path)])
		loaded = analyzer.load_input_data(args)
	assert loaded["collector_report"] == report
	assert "deep_dive_logs" not in loaded


def test_load_input_data_errors_when_no_data_present():
	with tempfile.TemporaryDirectory(prefix="analyze-empty-") as td:
		args = analyzer.build_parser().parse_args(["--data-dir", td])
		_assert_raises(ValueError, analyzer.load_input_data, args)


# ---------- prepare_analysis_context ---------------------------------------


def _make_runs(count: int, *, conclusion: str, base_id: int = 0) -> list[dict]:
	return [
		{
			"repository": "o/r",
			"run_id": base_id + i,
			"workflow_name": "ci",
			"workflow_family": "ci",
			"conclusion": conclusion,
			"duration_seconds": 10 * (i + 1),
			"run_attempt": 1,
			"retries": 0,
			"created_at": f"2026-04-{(i % 28) + 1:02d}T00:00:00Z",
		}
		for i in range(count)
	]


def test_prepare_analysis_context_lifts_caps_to_100_and_drops_deep_dive_logs():
	# 150 failing + 200 successful, plus excerpts attached to inputs to confirm
	# they are NOT propagated into the output context (Q8: B).
	failing = _make_runs(150, conclusion="failure", base_id=0)
	for run in failing:
		run["log_excerpts"] = [{"step_name": "step1", "excerpt": "boom"}]
	successful = _make_runs(200, conclusion="success", base_id=1000)
	report = {
		"runs": failing + successful,
		"errors": [{"repository": "o/r", "scope": "logs", "message": f"err-{i}"} for i in range(400)],
	}
	input_data = {
		"source_files": ["x.json"],
		"collector_report": report,
		"run_metrics": None,
		"summary_stats": None,
	}

	ctx = analyzer.prepare_analysis_context(input_data)

	assert "deep_dive_logs" not in ctx
	assert len(ctx["failing_runs"]) == analyzer.RUN_LIST_CAP == 100
	assert len(ctx["slow_runs"]) == analyzer.RUN_LIST_CAP == 100
	assert len(ctx["recent_runs"]) == analyzer.RUN_LIST_CAP == 100
	assert len(ctx["errors"]) == analyzer.ERRORS_CAP == 250
	# Each row in the lists is a normalized view, never the raw row, so
	# log_excerpts must not have leaked through.
	assert all("log_excerpts" not in run for run in ctx["failing_runs"])


def test_prepare_analysis_context_marks_insufficient_data_when_empty():
	ctx = analyzer.prepare_analysis_context(
		{
			"source_files": [],
			"collector_report": {"runs": [], "summary": {"total_runs": 0}, "errors": []},
			"run_metrics": None,
			"summary_stats": None,
		}
	)
	assert ctx["insufficient_data"] is True
	assert "insufficient" in ctx["analysis_guidance"].lower()


def test_prepare_analysis_context_failing_runs_sorted_by_duration():
	runs = [
		{"repository": "o/r", "run_id": 1, "conclusion": "failure", "duration_seconds": 10},
		{"repository": "o/r", "run_id": 2, "conclusion": "failure", "duration_seconds": 500},
		{"repository": "o/r", "run_id": 3, "conclusion": "success", "duration_seconds": 600},
	]
	ctx = analyzer.prepare_analysis_context(
		{
			"source_files": [],
			"collector_report": {"runs": runs, "summary": {}, "errors": []},
			"run_metrics": None,
			"summary_stats": None,
		}
	)
	assert [r["run_id"] for r in ctx["failing_runs"]] == [2, 1]
	assert [r["run_id"] for r in ctx["slow_runs"][:1]] == [3]


def test_prepare_analysis_context_carries_cost_telemetry_through_rollups():
	runs = [
		{
			"repository": "o/r",
			"run_id": 1,
			"workflow_name": "AI Review",
			"workflow_family": "review_autofix",
			"conclusion": "failure",
			"duration_seconds": 10,
			"created_at": "2026-04-01T00:00:00Z",
			"cost_telemetry": {
				"log_parsed": True,
				"or_prompt_tokens": 100,
				"or_completion_tokens": 25,
				"or_total_tokens": 125,
				"or_cache_write_tokens": 30,
				"or_cache_read_tokens": 40,
				"or_calls": 1,
				"break_glass_count": 1,
				"context_budget_warn_count": 0,
				"wall_clock_p50_ms": 1000,
				"wall_clock_p99_ms": 1000,
			},
		},
		{
			"repository": "o/r",
			"run_id": 2,
			"workflow_name": "AI Review",
			"workflow_family": "review_autofix",
			"conclusion": "success",
			"duration_seconds": 20,
			"created_at": "2026-04-02T00:00:00Z",
			"cost_telemetry": {
				"log_parsed": True,
				"or_prompt_tokens": 50,
				"or_completion_tokens": 10,
				"or_total_tokens": 60,
				"or_cache_write_tokens": 0,
				"or_cache_read_tokens": 20,
				"or_calls": 1,
				"break_glass_count": 0,
				"context_budget_warn_count": 1,
				"wall_clock_p50_ms": 2000,
				"wall_clock_p99_ms": 2000,
			},
		},
	]
	ctx = analyzer.prepare_analysis_context(
		{
			"source_files": [],
			"collector_report": {"runs": runs, "summary": {"total_runs": 2}, "errors": []},
			"run_metrics": None,
			"summary_stats": None,
		}
	)

	assert ctx["summary"]["cost_telemetry"]["runs_with_log_telemetry"] == 2
	assert ctx["summary"]["cost_telemetry"]["or_total_tokens"] == 185
	assert ctx["summary"]["cost_telemetry"]["context_budget_warn_count"] == 1
	assert ctx["per_repo"]["o/r"]["cost_telemetry"]["break_glass_count"] == 1
	assert ctx["per_workflow_family"]["review_autofix"]["cost_telemetry"]["context_budget_warn_count"] == 1
	assert ctx["failing_runs"][0]["cost_telemetry"]["break_glass_count"] == 1


# ---------- resolve_dated_output_path --------------------------------------


def test_resolve_dated_output_path_with_suffix_collision():
	with tempfile.TemporaryDirectory(prefix="analyze-output-") as td:
		output_dir = Path(td)
		(output_dir / "workflow-optimization-2026-04-11.md").write_text("x", encoding="utf-8")
		(output_dir / "workflow-optimization-2026-04-11-2.md").write_text("x", encoding="utf-8")
		resolved = analyzer.resolve_dated_output_path(
			None,
			str(output_dir),
			today=date(2026, 4, 11),
		)
		assert resolved == output_dir / "workflow-optimization-2026-04-11-3.md"


def test_resolve_dated_output_path_uses_explicit_output_when_provided():
	resolved = analyzer.resolve_dated_output_path(
		"/tmp/explicit.md",
		"/ignored",
		today=date(2026, 4, 11),
	)
	assert resolved == Path("/tmp/explicit.md")


# ---------- main() ---------------------------------------------------------


def test_main_writes_analysis_context_and_prints_path():
	report = {
		"runs": _make_runs(2, conclusion="failure"),
		"summary": {"total_runs": 2},
		"errors": [],
		"scope": {"repositories": ["o/r"]},
	}
	with tempfile.TemporaryDirectory(prefix="analyze-main-") as td:
		td_path = Path(td)
		input_path = td_path / "workflow_log_report.json"
		_write_json(input_path, report)
		output_md = td_path / "analysis" / "workflow-optimization-2099-12-31.md"

		with _capture_std() as (out, _err):
			exit_code = analyzer.main(
				[
					"--input",
					str(input_path),
					"--output",
					str(output_md),
					"--codex-mode",
				]
			)
		assert exit_code == 0
		printed_path = Path(out.getvalue().strip())
		assert printed_path == output_md.parent / "analysis_context.json"
		assert printed_path.exists()
		payload = json.loads(printed_path.read_text(encoding="utf-8"))
		assert "deep_dive_logs" not in payload
		assert len(payload["failing_runs"]) == 2


def test_main_returns_2_when_input_missing():
	with _capture_std() as (_out, err):
		exit_code = analyzer.main(["--input", "/nonexistent/does-not-exist.json"])
	assert exit_code == 2
	assert "ERROR" in err.getvalue()


# ---------- aggregations ---------------------------------------------------


def test_aggregate_runs_by_key_computes_failure_rate_and_durations():
	runs = [
		{"repository": "a", "conclusion": "failure", "duration_seconds": 100},
		{"repository": "a", "conclusion": "success", "duration_seconds": 50},
		{"repository": "b", "conclusion": "failure", "duration_seconds": 200},
	]
	out = analyzer._aggregate_runs_by_key(runs, "repository")
	assert out["a"]["total_runs"] == 2
	assert out["a"]["failure_count"] == 1
	assert abs(out["a"]["failure_rate"] - 0.5) < 1e-9
	assert out["a"]["avg_duration_seconds"] == 75.0
	assert out["b"]["failure_rate"] == 1.0


# ---------- script-mode entry point ---------------------------------------
# CI's "Workflow log analyzer coverage gate" invokes this file directly:
#   python3 -m coverage run tests/test_analyze_workflow_logs.py
# CI does NOT install pytest, so this entry point must run all tests
# in-process without any pytest dependency. It walks `test_*` callables
# in module globals and calls each one; assertion failures abort with a
# non-zero exit so the coverage gate fails loudly on regressions.


def main() -> int:
	test_funcs = sorted(
		(name, obj)
		for name, obj in globals().items()
		if name.startswith("test_") and callable(obj)
	)
	failures: list[tuple[str, BaseException]] = []
	for name, func in test_funcs:
		try:
			func()
		except BaseException as exc:  # noqa: BLE001 — script runner aggregates everything
			failures.append((name, exc))
			print(f"FAIL: {name}: {exc!r}", file=sys.stderr)
		else:
			print(f"PASS: {name}")
	if failures:
		print(f"\n{len(failures)} of {len(test_funcs)} tests failed.", file=sys.stderr)
		return 1
	print(f"\n{len(test_funcs)} tests passed.")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
