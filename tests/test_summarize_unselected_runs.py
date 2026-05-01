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


def test_select_targets_rejects_whitespace_or_non_string_repository():
	runs = [
		{"repository": "   ", "run_id": 1, "created_at": "2026-01-01T00:00:00Z"},
		{"repository": "", "run_id": 2, "created_at": "2026-01-02T00:00:00Z"},
		{"repository": 42, "run_id": 3, "created_at": "2026-01-03T00:00:00Z"},
		{"repository": "a/b", "run_id": 4, "created_at": "2026-01-04T00:00:00Z"},
	]
	picked = summarizer.select_targets(runs, max_summaries=10)
	# Only the last row has a usable repository string.
	assert [r["run_id"] for r in picked] == [4]


def test_select_targets_tolerates_non_string_created_at_in_sort_key():
	"""A malformed `created_at` (int / dict / None) must not raise — it just
	sorts as 'no timestamp' instead of aborting the whole window."""
	runs = [
		{"repository": "a/b", "run_id": 1, "created_at": 12345},
		{"repository": "a/b", "run_id": 2, "created_at": {"not": "a string"}},
		{"repository": "a/b", "run_id": 3, "created_at": None},
		{"repository": "a/b", "run_id": 4, "created_at": "2026-04-01T00:00:00Z"},
	]
	# Must not raise; the well-formed row should sort newest among them.
	picked = summarizer.select_targets(runs, max_summaries=10)
	assert {r["run_id"] for r in picked} == {1, 2, 3, 4}
	assert picked[0]["run_id"] == 4


def test_parse_iso8601_returns_none_for_non_string_inputs():
	assert summarizer._parse_iso8601(None) is None
	assert summarizer._parse_iso8601(12345) is None
	assert summarizer._parse_iso8601({"not": "a string"}) is None
	assert summarizer._parse_iso8601(["2026-01-01"]) is None


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


def test_select_targets_treats_whitespace_log_summary_as_missing():
	runs = [
		{"repository": "a/b", "run_id": 1, "created_at": "2026-01-01T00:00:00Z", "log_summary": "   "},
		{"repository": "a/b", "run_id": 2, "created_at": "2026-01-02T00:00:00Z", "log_summary": ""},
		{"repository": "a/b", "run_id": 3, "created_at": "2026-01-03T00:00:00Z", "log_summary": "real"},
	]
	picked = summarizer.select_targets(runs, max_summaries=10)
	# Whitespace-only and empty are treated as missing → eligible. Real text
	# is treated as already summarized → skipped.
	assert sorted(r["run_id"] for r in picked) == [1, 2]


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
	# Strict cap: total length INCLUDING the truncation marker must fit
	# within `char_cap`. This matches the --per-run-char-cap CLI contract.
	assert len(text) <= 5_000
	assert "[run-level truncation]" in text


def test_build_summary_input_preserves_late_step_signal_in_tail():
	"""Regression: previously an early `break` at total>=char_cap*2 dropped
	late steps entirely. The run-level truncation now keeps both ends, so
	the failing-step content at the bottom of the archive must survive."""
	logs = [
		{"step_name": f"step{i:03d}", "content": f"step{i:03d}-body " * 200}
		for i in range(40)
	]
	# Append a sentinel as the final step (mirrors the failing step / final
	# warnings that the analyzer needs).
	logs.append({"step_name": "final", "content": "FAILING_STEP_SENTINEL " * 50})
	text = summarizer.build_summary_input(
		logs, char_cap=8_000, per_step_head=200, per_step_tail=400
	)
	assert "FAILING_STEP_SENTINEL" in text
	assert len(text) <= 8_000


def test_build_summary_input_when_cap_smaller_than_marker_returns_marker_prefix():
	# Pathological tiny cap (< marker length): we still must not exceed it.
	logs = [{"step_name": "s", "content": "X" * 100}]
	text = summarizer.build_summary_input(logs, char_cap=10, per_step_head=5, per_step_tail=5)
	assert len(text) <= 10


def test_build_summary_input_strict_cap_at_marker_plus_one():
	# Regression: when budget==1 (char_cap == len(marker)+1), the previous
	# `max(budget - head_size, 1)` clamp produced char_cap+1 chars.
	marker = "\n... [run-level truncation] ...\n"
	char_cap = len(marker) + 1
	logs = [{"step_name": "s", "content": "X" * 1_000}]
	text = summarizer.build_summary_input(
		logs, char_cap=char_cap, per_step_head=10, per_step_tail=10
	)
	assert len(text) <= char_cap


def test_build_summary_input_strict_cap_at_marker_plus_two():
	marker = "\n... [run-level truncation] ...\n"
	char_cap = len(marker) + 2
	logs = [{"step_name": "s", "content": "X" * 1_000}]
	text = summarizer.build_summary_input(
		logs, char_cap=char_cap, per_step_head=10, per_step_tail=10
	)
	assert len(text) <= char_cap


def test_build_summary_input_skips_empty_steps():
	logs = [
		{"step_name": "empty", "content": ""},
		{"step_name": "real", "content": "hello world"},
	]
	text = summarizer.build_summary_input(logs, char_cap=10_000)
	assert "STEP: real" in text
	assert "STEP: empty" not in text


def test_build_summary_input_clamps_non_positive_char_cap():
	logs = [{"step_name": "build", "content": "X" * 5_000}]
	# A misconfigured char_cap=0 is clamped to 1, and the strict cap means
	# the returned text fits in 1 character (marker truncated to that).
	text = summarizer.build_summary_input(logs, char_cap=0, per_step_head=10, per_step_tail=10)
	assert len(text) <= 1


def test_extract_tokens_used_prefers_normalized_total():
	got = summarizer._extract_tokens_used(
		{"total_tokens": 123, "prompt_tokens": 100, "completion_tokens": 23},
		input_chars=4_000,
		max_output_tokens=500,
	)
	assert got == 123


def test_extract_tokens_used_sums_prompt_and_completion_when_total_missing():
	got = summarizer._extract_tokens_used(
		{"prompt_tokens": 80, "completion_tokens": 20},
		input_chars=4_000,
		max_output_tokens=500,
	)
	assert got == 100


def test_extract_tokens_used_falls_back_when_usage_missing():
	# When the provider omits `usage` entirely, the fallback estimate must
	# still return a positive number so --token-budget keeps advancing.
	got = summarizer._extract_tokens_used(None, input_chars=4_000, max_output_tokens=500)
	assert got >= 1
	# Conservative estimate: input_chars/4 + max_output_tokens.
	assert got == 4_000 // 4 + 500


def test_extract_tokens_used_falls_back_on_empty_usage_dict():
	got = summarizer._extract_tokens_used({}, input_chars=2_000, max_output_tokens=400)
	assert got == 2_000 // 4 + 400


# ---------- _format_user_message + truncation helper ---------------------


def test_truncate_step_returns_short_content_unchanged():
	assert summarizer._truncate_step("short", 100, 100) == "short"


def test_truncate_step_handles_tail_chars_zero_without_falling_back_to_full_string():
	"""Python `content[-0:]` evaluates to `content[0:]` (the full string).
	The truncator must guard against that so per_step_tail=0 actually drops
	the tail instead of silently returning the entire content."""
	content = "X" * 1_000
	out = summarizer._truncate_step(content, head_chars=10, tail_chars=0)
	# Should be head + marker only — never the full 1000-char content.
	assert content not in out
	assert out.startswith("X" * 10)
	assert "[truncated 990 chars]" in out


def test_truncate_step_handles_head_chars_zero():
	content = "Y" * 500
	out = summarizer._truncate_step(content, head_chars=0, tail_chars=20)
	assert content not in out
	assert out.endswith("Y" * 20)


def test_truncate_step_returns_marker_only_when_both_bounds_zero():
	out = summarizer._truncate_step("Z" * 100, head_chars=0, tail_chars=0)
	assert "Z" not in out
	assert "[truncated" in out


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


def test_main_counts_empty_archive_as_skipped_empty_logs_not_fetch_error():
	"""When extract_full_logs returns nothing summarizable, the run must be
	counted as skipped_empty_logs (fetch succeeded, archive was empty),
	not skipped_fetch_error (transient/unrecoverable fetch failure)."""
	with tempfile.TemporaryDirectory() as tmp:
		report = Path(tmp) / "report.json"
		_write_report(
			report,
			{
				"runs": [
					{"repository": "a/b", "run_id": 1, "created_at": "2026-04-30T00:00:00Z"}
				]
			},
		)

		class _EmptyArchiveCollector:
			@staticmethod
			def _fetch_run_log_archive(repo, run_id, *, token, cache=None):
				return b""

			@staticmethod
			def extract_full_logs(_archive):
				return []  # empty -> logs_text is blank

		class _ShouldNotBeCalledSummarizer:
			def __init__(self, *_a, **_k):
				pass

			def summarize(self, *_a, **_k):  # pragma: no cover — guarded by empty path
				raise AssertionError("summarize() must not be called for empty archives")

		original_loader = summarizer._load_collector_module
		original_klass = summarizer.OpenRouterSummarizer
		summarizer._load_collector_module = lambda: _EmptyArchiveCollector
		summarizer.OpenRouterSummarizer = _ShouldNotBeCalledSummarizer
		try:
			with _env(OPENROUTER_API_KEY="orkey", GH_TOKEN="ghs_test"), _capture_std() as (_out, err):
				rc = summarizer.main(["--report", str(report)])
			err_text = err.getvalue()
		finally:
			summarizer._load_collector_module = original_loader
			summarizer.OpenRouterSummarizer = original_klass

	assert rc == 0
	# Telemetry line includes skipped_empty_logs=1 and skipped_fetch_error=0.
	assert '"skipped_empty_logs": 1' in err_text
	assert '"skipped_fetch_error": 0' in err_text


def test_main_does_not_pass_archive_cache_so_payload_bytes_arent_retained():
	"""The collector caches payload bytes by (repo, run_id) when given a
	cache dict; for a script that fetches each run exactly once that just
	leaks log archives. Verify cache=None is passed."""
	with tempfile.TemporaryDirectory() as tmp:
		report = Path(tmp) / "report.json"
		_write_report(
			report,
			{
				"runs": [
					{"repository": "a/b", "run_id": 1, "created_at": "2026-04-01T00:00:00Z"},
					{"repository": "a/b", "run_id": 2, "created_at": "2026-04-02T00:00:00Z"},
				]
			},
		)
		seen_cache_args: list[object] = []

		class _CacheSpyingCollector:
			@staticmethod
			def _fetch_run_log_archive(repo, run_id, *, token, cache=None):
				seen_cache_args.append(cache)
				return b"<archive>"

			@staticmethod
			def extract_full_logs(_archive):
				return [{"step_name": "build", "content": "ok"}]

		class _NoopSummarizer:
			def __init__(self, *_a, **_k):
				pass

			def summarize(self, *_a, **_k):
				return ("- ok", 10)

		original_loader = summarizer._load_collector_module
		original_klass = summarizer.OpenRouterSummarizer
		summarizer._load_collector_module = lambda: _CacheSpyingCollector
		summarizer.OpenRouterSummarizer = _NoopSummarizer
		try:
			with _env(OPENROUTER_API_KEY="orkey", GH_TOKEN="ghs_test"), _capture_std():
				rc = summarizer.main(["--report", str(report)])
		finally:
			summarizer._load_collector_module = original_loader
			summarizer.OpenRouterSummarizer = original_klass

	assert rc == 0
	assert len(seen_cache_args) == 2
	assert all(arg is None for arg in seen_cache_args), (
		f"expected cache=None for every fetch to avoid retaining log-archive bytes, "
		f"got: {seen_cache_args!r}"
	)


def test_main_breaks_loop_when_token_budget_exhausted():
	"""After a single mini call exceeds the budget, remaining runs must be
	accounted for in `skipped_budget_exhausted` in one shot — not iterated."""
	with tempfile.TemporaryDirectory() as tmp:
		report = Path(tmp) / "report.json"
		_write_report(
			report,
			{
				"runs": [
					{
						"repository": "a/b",
						"run_id": i,
						"created_at": f"2026-04-{i:02d}T00:00:00Z",
					}
					for i in range(1, 6)  # 5 eligible runs
				]
			},
		)
		fetch_calls = {"n": 0}
		summarize_calls = {"n": 0}

		class _FakeCollector:
			@staticmethod
			def _fetch_run_log_archive(repo, run_id, *, token, cache=None):
				fetch_calls["n"] += 1
				return b"<archive bytes>"

			@staticmethod
			def extract_full_logs(_archive):
				return [{"step_name": "build", "content": "ok"}]

		class _BudgetEatingSummarizer:
			def __init__(self, *_a, **_k):
				pass

			def summarize(self, *_a, **_k):
				summarize_calls["n"] += 1
				# First call alone consumes the entire budget.
				return ("- summary", 10_000)

		original_loader = summarizer._load_collector_module
		original_klass = summarizer.OpenRouterSummarizer
		summarizer._load_collector_module = lambda: _FakeCollector
		summarizer.OpenRouterSummarizer = _BudgetEatingSummarizer
		try:
			with _env(OPENROUTER_API_KEY="orkey", GH_TOKEN="ghs_test"), _capture_std():
				rc = summarizer.main(
					["--report", str(report), "--token-budget", "5000"]
				)
			written = json.loads(report.read_text(encoding="utf-8"))
		finally:
			summarizer._load_collector_module = original_loader
			summarizer.OpenRouterSummarizer = original_klass

	assert rc == 0
	# Exactly one summary written; loop must not continue calling the
	# summarizer or fetching archives once budget exceeded.
	assert summarize_calls["n"] == 1
	assert fetch_calls["n"] == 1
	summarized_count = sum(1 for r in written["runs"] if r.get("log_summary"))
	assert summarized_count == 1


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
