#!/usr/bin/env python3
"""Tests for scripts/summarize_unselected_runs.py.

Coverage:
  * select_targets: filters runs already covered by deep-dive log_excerpts or a
    prior log_summary, requires repo+run_id, sorts newest-first, and respects
    the cap.
  * build_summary_input: per-step head/tail truncation and run-level fallback
    truncation when the assembled text still exceeds the cap.
  * _normalized_run_view (in analyze_workflow_logs): carries log_summary
    through to analysis_context.json when present.
  * main(): fail-open paths — missing report, missing creds, missing 'runs'
    array all return 0 and emit telemetry instead of raising.

Run via the CI coverage gate pattern (no pytest dependency):
  python3 -m coverage run tests/test_summarize_unselected_runs.py
"""

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
SCRIPT_PATH = REPO_ROOT / "scripts" / "summarize_unselected_runs.py"
ANALYZER_PATH = REPO_ROOT / "scripts" / "analyze_workflow_logs.py"
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))


def _load_module(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


summarizer = _load_module("summarize_unselected_runs", SCRIPT_PATH)
analyzer = _load_module("analyze_workflow_logs", ANALYZER_PATH)


@contextlib.contextmanager
def _capture_std():
	out, err = io.StringIO(), io.StringIO()
	with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
		yield out, err


@contextlib.contextmanager
def _env(**overrides):
	original = {key: os.environ.get(key) for key in overrides}
	try:
		for key, value in overrides.items():
			if value is None:
				os.environ.pop(key, None)
			else:
				os.environ[key] = value
		yield
	finally:
		for key, value in original.items():
			if value is None:
				os.environ.pop(key, None)
			else:
				os.environ[key] = value


# ---------- select_targets ------------------------------------------------


def test_select_targets_skips_runs_with_excerpts_or_existing_summary():
	runs = [
		{"repository": "a/b", "run_id": 1, "created_at": "2026-01-01T00:00:00Z"},
		{
			"repository": "a/b",
			"run_id": 2,
			"created_at": "2026-01-02T00:00:00Z",
			"log_excerpts": [{"step_name": "x", "excerpt": "..."}],
		},
		{
			"repository": "a/b",
			"run_id": 3,
			"created_at": "2026-01-03T00:00:00Z",
			"log_summary": "already summarized",
		},
		{"repository": "a/b", "run_id": 4, "created_at": "2026-01-04T00:00:00Z"},
	]
	picked = summarizer.select_targets(runs, max_summaries=10)
	assert [r["run_id"] for r in picked] == [4, 1], picked


def test_select_targets_requires_repo_and_run_id():
	runs = [
		{"run_id": 5, "created_at": "2026-01-01T00:00:00Z"},
		{"repository": "a/b", "run_id": 0, "created_at": "2026-01-01T00:00:00Z"},
		{"repository": "a/b", "run_id": 7, "created_at": "2026-01-01T00:00:00Z"},
	]
	picked = summarizer.select_targets(runs, max_summaries=10)
	assert [r["run_id"] for r in picked] == [7]


def test_select_targets_caps_and_sorts_newest_first():
	runs = [
		{"repository": "a/b", "run_id": i, "created_at": f"2026-01-{i:02d}T00:00:00Z"}
		for i in range(1, 8)
	]
	picked = summarizer.select_targets(runs, max_summaries=3)
	assert [r["run_id"] for r in picked] == [7, 6, 5]


def test_select_targets_zero_cap_returns_empty():
	runs = [{"repository": "a/b", "run_id": 1, "created_at": "2026-01-01T00:00:00Z"}]
	assert summarizer.select_targets(runs, max_summaries=0) == []


# ---------- build_summary_input ------------------------------------------


def test_build_summary_input_per_step_head_tail_truncation():
	long_content = "A" * 2000 + "MIDDLE" + "Z" * 4000
	logs = [{"step_name": "build", "content": long_content}]
	text = summarizer.build_summary_input(
		logs, char_cap=20_000, per_step_head=500, per_step_tail=1_000
	)
	assert text.startswith("=== STEP: build ===\n")
	assert "A" * 500 in text
	assert "Z" * 1_000 in text
	assert "MIDDLE" not in text  # middle dropped
	assert "[truncated" in text


def test_build_summary_input_run_level_truncation_when_assembled_exceeds_cap():
	logs = [{"step_name": f"s{i}", "content": "X" * 6_000} for i in range(10)]
	text = summarizer.build_summary_input(
		logs, char_cap=5_000, per_step_head=1_000, per_step_tail=2_000
	)
	assert len(text) <= 5_000 + len("\n... [run-level truncation] ...\n")
	assert "[run-level truncation]" in text


def test_build_summary_input_skips_empty_steps():
	logs = [
		{"step_name": "empty", "content": ""},
		{"step_name": "real", "content": "hello world"},
	]
	text = summarizer.build_summary_input(logs, char_cap=10_000)
	assert "STEP: real" in text
	assert "STEP: empty" not in text


# ---------- _format_user_message + truncation helper ---------------------


def test_truncate_step_returns_short_content_unchanged():
	assert summarizer._truncate_step("short", 100, 100) == "short"


def test_format_user_message_includes_failure_point_when_present():
	run = {
		"repository": "owner/repo",
		"run_id": 42,
		"workflow_name": "Plan",
		"workflow_family": "plan",
		"conclusion": "failure",
		"duration_seconds": 120,
		"run_attempt": 2,
		"retries": 1,
		"failure_point": {"job_name": "build", "step_name": "compile"},
	}
	msg = summarizer._format_user_message(run, "logs go here")
	assert "owner/repo #42" in msg
	assert "build / compile" in msg
	assert "logs go here" in msg


# ---------- analyzer carries log_summary through ------------------------


def test_normalized_run_view_carries_log_summary():
	run = {
		"repository": "a/b",
		"run_id": 7,
		"workflow_name": "Plan",
		"log_summary": "- failure on step X\n- 429 from openrouter",
	}
	view = analyzer._normalized_run_view(run)
	assert view["log_summary"].startswith("- failure")


def test_normalized_run_view_omits_log_summary_when_blank():
	view = analyzer._normalized_run_view({"repository": "a/b", "run_id": 1, "log_summary": "  "})
	assert "log_summary" not in view


# ---------- main() fail-open paths --------------------------------------


def _write_report(path: Path, payload: dict) -> None:
	path.write_text(json.dumps(payload), encoding="utf-8")


def test_main_returns_zero_when_report_missing():
	with tempfile.TemporaryDirectory() as tmp:
		report = Path(tmp) / "absent.json"
		with _capture_std() as (_out, err):
			rc = summarizer.main(["--report", str(report)])
	assert rc == 0
	assert "not found" in err.getvalue()


def test_main_returns_zero_when_openrouter_key_missing():
	with tempfile.TemporaryDirectory() as tmp:
		report = Path(tmp) / "report.json"
		_write_report(report, {"runs": [{"repository": "a/b", "run_id": 1}]})
		with _env(OPENROUTER_API_KEY="", GH_TOKEN="ghs_test"), _capture_std() as (_out, err):
			rc = summarizer.main(["--report", str(report)])
	assert rc == 0
	assert "OPENROUTER_API_KEY" in err.getvalue()
	assert "AI_MEMORY_TELEMETRY" in err.getvalue()


def test_main_returns_zero_when_gh_token_missing():
	with tempfile.TemporaryDirectory() as tmp:
		report = Path(tmp) / "report.json"
		_write_report(report, {"runs": []})
		with _env(OPENROUTER_API_KEY="orkey", GH_TOKEN=""), _capture_std() as (_out, err):
			rc = summarizer.main(["--report", str(report)])
	assert rc == 0
	assert "GH_TOKEN" in err.getvalue()


def test_main_returns_zero_when_runs_field_missing():
	with tempfile.TemporaryDirectory() as tmp:
		report = Path(tmp) / "report.json"
		_write_report(report, {"summary": {}})
		with _env(OPENROUTER_API_KEY="orkey", GH_TOKEN="ghs_test"), _capture_std() as (_out, err):
			rc = summarizer.main(["--report", str(report)])
	assert rc == 0
	assert "missing 'runs' array" in err.getvalue()


def test_main_returns_zero_with_zero_max_summaries():
	with tempfile.TemporaryDirectory() as tmp:
		report = Path(tmp) / "report.json"
		_write_report(report, {"runs": [{"repository": "a/b", "run_id": 1}]})
		with _env(OPENROUTER_API_KEY="orkey", GH_TOKEN="ghs_test"), _capture_std() as (_out, err):
			rc = summarizer.main(["--report", str(report), "--max-summaries", "0"])
	assert rc == 0


def test_main_writes_summary_when_summarizer_succeeds(monkeypatch=None):
	"""End-to-end happy path with both collector + summarizer monkey-patched."""
	with tempfile.TemporaryDirectory() as tmp:
		report = Path(tmp) / "report.json"
		_write_report(
			report,
			{
				"runs": [
					{
						"repository": "a/b",
						"run_id": 9,
						"created_at": "2026-04-30T00:00:00Z",
						"workflow_name": "Plan",
						"conclusion": "success",
					}
				]
			},
		)

		# Stub out the collector loader so we don't need ai_memory_lib.
		class _FakeCollector:
			@staticmethod
			def _fetch_run_log_archive(repo, run_id, *, token, cache=None):
				return b"<archive bytes>"

			@staticmethod
			def extract_full_logs(_archive):
				return [{"step_name": "build", "content": "ok"}]

		original_loader = summarizer._load_collector_module
		summarizer._load_collector_module = lambda: _FakeCollector

		# Stub the OpenRouter client so we don't hit the network.
		class _FakeSummarizer:
			def __init__(self, *_a, **_k):
				pass

			def summarize(self, _run, _text):
				return ("- success in 1s", 42)

		original_klass = summarizer.OpenRouterSummarizer
		summarizer.OpenRouterSummarizer = _FakeSummarizer
		try:
			with _env(OPENROUTER_API_KEY="orkey", GH_TOKEN="ghs_test"), _capture_std():
				rc = summarizer.main(["--report", str(report)])
			assert rc == 0
			written = json.loads(report.read_text(encoding="utf-8"))
		finally:
			summarizer._load_collector_module = original_loader
			summarizer.OpenRouterSummarizer = original_klass

	row = written["runs"][0]
	assert row["log_summary"].startswith("- success")
	assert row["log_summary_meta"]["tokens_used"] == 42
	assert row["log_summary_meta"]["model"]


def test_main_fail_open_when_summarizer_raises():
	with tempfile.TemporaryDirectory() as tmp:
		report = Path(tmp) / "report.json"
		_write_report(
			report,
			{
				"runs": [
					{
						"repository": "a/b",
						"run_id": 11,
						"created_at": "2026-04-30T00:00:00Z",
					}
				]
			},
		)

		class _FakeCollector:
			@staticmethod
			def _fetch_run_log_archive(repo, run_id, *, token, cache=None):
				return b"<archive bytes>"

			@staticmethod
			def extract_full_logs(_archive):
				return [{"step_name": "build", "content": "ok"}]

		class _FlakySummarizer:
			def __init__(self, *_a, **_k):
				pass

			def summarize(self, *_a, **_k):
				raise RuntimeError("simulated upstream 503")

		original_loader = summarizer._load_collector_module
		original_klass = summarizer.OpenRouterSummarizer
		summarizer._load_collector_module = lambda: _FakeCollector
		summarizer.OpenRouterSummarizer = _FlakySummarizer
		try:
			with _env(OPENROUTER_API_KEY="orkey", GH_TOKEN="ghs_test"), _capture_std() as (_out, err):
				rc = summarizer.main(["--report", str(report)])
			err_text = err.getvalue()
			written = json.loads(report.read_text(encoding="utf-8"))
		finally:
			summarizer._load_collector_module = original_loader
			summarizer.OpenRouterSummarizer = original_klass

	assert rc == 0
	assert "simulated upstream 503" in err_text
	# Run row must remain unchanged (no log_summary written) under fail-open.
	assert "log_summary" not in written["runs"][0]


# ---------- script-mode entry point --------------------------------------


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
		except BaseException as exc:  # noqa: BLE001
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
