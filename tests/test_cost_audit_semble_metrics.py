#!/usr/bin/env python3
"""Focused parser coverage for Semble telemetry in scripts/cost_audit.py."""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
PHASE_H_CONTEXT_BUDGET_OVERFLOW_FIXTURE = (
	REPO_ROOT / "scripts" / "fixtures" / "cloudflare-learnings" / "phase-h-context-budget-overflow.txt"
)

from cost_audit import (  # noqa: E402
	aggregate_run_cost_telemetry,
	build_context_budget_warning,
	build_context_budget_warn_line_for_file,
	build_run_cost_telemetry,
	parse_log,
)
from openrouter_prompt_cache import format_openrouter_usage_line  # noqa: E402
import analyze_soft_errors  # noqa: E402
import summarize_unselected_runs  # noqa: E402


class _TelemetryFakeOpenRouterResponse:
	def __init__(self, payload: dict) -> None:
		self.payload = payload

	def __enter__(self):
		return self

	def __exit__(self, _exc_type, _exc, _traceback) -> bool:
		return False

	def read(self) -> bytes:
		return json.dumps(self.payload).encode("utf-8")


def test_format_openrouter_usage_line_preserves_complete_usage_contract() -> None:
	line = format_openrouter_usage_line(
		{
			"prompt_tokens": 100,
			"completion_tokens": 25,
			"total_tokens": 125,
			"cache_creation_input_tokens": 30,
			"cache_read_input_tokens": 40,
		},
		model="openai/gpt-5.6-luna",
		phase="workflow-log-analysis",
		call_label="summarize-unselected-run",
		cache_enabled=True,
		cache_breakpoint_enabled=None,
		cache_breakpoint_fallback_retry=None,
	)

	assert line == (
		"INFO: openrouter usage phase=workflow-log-analysis "
		"call=summarize-unselected-run model=openai/gpt-5.6-luna "
		"cache_enabled=true cache_breakpoint_enabled=na "
		"cache_breakpoint_fallback_retry=na prompt_tokens=100 "
		"completion_tokens=25 total_tokens=125 "
		"cache_creation_input_tokens=30 cache_read_input_tokens=40"
	)
	parsed = parse_log(line)
	assert parsed["or_calls"] == 1
	assert parsed["or_prompt_tokens"] == 100
	assert parsed["or_completion_tokens"] == 25
	assert parsed["or_total_tokens"] == 125
	assert parsed["or_cache_write_tokens"] == 30
	assert parsed["or_cache_read_tokens"] == 40


def test_format_openrouter_usage_line_normalizes_nested_cache_usage() -> None:
	line = format_openrouter_usage_line(
		{
			"prompt_tokens": 80,
			"completion_tokens": 20,
			"total_tokens": 100,
			"prompt_tokens_details": {"cache_write_tokens": 31},
			"input_token_details": {"cache_read": 41},
		},
		model="openai/gpt-5.6-luna",
		phase="release-gate",
		call_label="soft-error-analyzer",
		cache_enabled=False,
		cache_breakpoint_enabled=None,
		cache_breakpoint_fallback_retry=None,
	)

	parsed = parse_log(line)
	assert parsed["or_calls"] == 1
	assert parsed["or_cache_write_tokens"] == 31
	assert parsed["or_cache_read_tokens"] == 41


def test_format_openrouter_usage_line_uses_na_for_missing_usage() -> None:
	line = format_openrouter_usage_line(
		None,
		model="openai/gpt-5.6-luna",
		phase="release-gate",
		call_label="soft-error-analyzer",
		cache_enabled=True,
		cache_breakpoint_enabled=None,
		cache_breakpoint_fallback_retry=None,
	)

	assert line.endswith(
		"prompt_tokens=na completion_tokens=na total_tokens=na "
		"cache_creation_input_tokens=na cache_read_input_tokens=na"
	)
	assert parse_log(line)["or_calls"] == 1


def test_direct_openrouter_callers_preserve_output_and_emit_safe_usage() -> None:
	summary_payload = {
		"model": "provider/summary-model",
		"choices": [{"message": {"content": "  summary response  "}}],
		"usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
	}
	summary_stderr = io.StringIO()
	summary_client = summarize_unselected_runs.OpenRouterSummarizer(
		"secret-summary-key",
		model="requested/summary-model",
		base_url="https://example.invalid",
		timeout_seconds=1,
		max_output_tokens=50,
	)
	with patch.object(
		summarize_unselected_runs.urllib.request,
		"urlopen",
		return_value=_TelemetryFakeOpenRouterResponse(summary_payload),
	), contextlib.redirect_stderr(summary_stderr):
		summary_result = summary_client.summarize(
			{"repository": "owner/repo", "run_id": 7}, "sensitive prompt text"
		)

	assert summary_result == ("summary response", 14)
	summary_line = summary_stderr.getvalue().strip()
	assert summary_line.count("INFO: openrouter usage ") == 1
	assert "model=provider/summary-model" in summary_line
	assert "secret-summary-key" not in summary_line
	assert "sensitive prompt text" not in summary_line
	assert "summary response" not in summary_line

	analyzer_payload = {
		"choices": [{"message": {"content": "analyzer response"}}],
		"usage": {
			"prompt_tokens": 21,
			"completion_tokens": 4,
			"total_tokens": 25,
			"prompt_tokens_details": {"cached_tokens": 8},
		},
	}
	analyzer_stderr = io.StringIO()
	with patch.object(
		analyze_soft_errors.urllib.request,
		"urlopen",
		return_value=_TelemetryFakeOpenRouterResponse(analyzer_payload),
	), contextlib.redirect_stderr(analyzer_stderr):
		analyzer_result = analyze_soft_errors.call_openrouter(
			[{"role": "user", "content": "private analyzer prompt"}],
			model="requested/analyzer-model",
			reasoning="medium",
			api_key="secret-analyzer-key",
		)

	assert analyzer_result == "analyzer response"
	analyzer_line = analyzer_stderr.getvalue().strip()
	assert analyzer_line.count("INFO: openrouter usage ") == 1
	assert "model=requested/analyzer-model" in analyzer_line
	assert "cache_read_input_tokens=8" in analyzer_line
	assert "secret-analyzer-key" not in analyzer_line
	assert "private analyzer prompt" not in analyzer_line
	assert "analyzer response" not in analyzer_line


def test_direct_openrouter_callers_emit_usage_for_empty_content() -> None:
	empty_payload = {
		"choices": [{"message": {"content": ""}}],
		"usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
	}
	summary_stderr = io.StringIO()
	summary_client = summarize_unselected_runs.OpenRouterSummarizer(
		"unused-key",
		model="requested/summary-model",
		base_url="https://example.invalid",
		timeout_seconds=1,
		max_output_tokens=50,
	)
	with patch.object(
		summarize_unselected_runs.urllib.request,
		"urlopen",
		return_value=_TelemetryFakeOpenRouterResponse(empty_payload),
	), contextlib.redirect_stderr(summary_stderr):
		try:
			summary_client.summarize({}, "logs")
		except RuntimeError as exc:
			assert str(exc) == "chat/completions empty content"
		else:
			raise AssertionError("empty summary content must retain its existing error")
	assert summary_stderr.getvalue().count("INFO: openrouter usage ") == 1
	summary_usage = parse_log(summary_stderr.getvalue())
	assert summary_usage["or_calls"] == 1
	assert summary_usage["or_prompt_tokens"] == 1
	assert summary_usage["or_total_tokens"] == 1

	analyzer_stderr = io.StringIO()
	with patch.object(
		analyze_soft_errors.urllib.request,
		"urlopen",
		return_value=_TelemetryFakeOpenRouterResponse(empty_payload),
	), contextlib.redirect_stderr(analyzer_stderr):
		result = analyze_soft_errors.call_openrouter(
			[],
			model="requested/analyzer-model",
			reasoning="medium",
			api_key="unused-key",
		)
	assert result == ""
	assert analyzer_stderr.getvalue().count("INFO: openrouter usage ") == 1
	analyzer_usage = parse_log(analyzer_stderr.getvalue())
	assert analyzer_usage["or_calls"] == 1
	assert analyzer_usage["or_prompt_tokens"] == 1
	assert analyzer_usage["or_total_tokens"] == 1


def test_parse_log_counts_semble_query_bytes_and_fallbacks_by_target() -> None:
	log = """
SEMBLE_QUERY target=overflow file=src/big.py chunks=20 bytes=1200 ms=5
SEMBLE_QUERY chunks=6 bytes=321 target=reviewer-context ms=7
SEMBLE_FALLBACK reason=timeout target=reviewer-context ms=5000
SEMBLE_FALLBACK target=overflow reason=exit=7 raw failure from semble ms=11
"""

	parsed = parse_log(log)

	assert parsed["semble_query_calls"] == 2
	assert parsed["semble_query_bytes"] == 1521
	assert parsed["semble_fallbacks"] == 2
	assert parsed["semble_contract_test_fallbacks"] == 0
	assert parsed["semble_runtime_fallbacks"] == 2
	assert parsed["semble_targets"] == {
		"overflow": {"query_calls": 1, "bytes": 1200, "fallbacks": 1, "runtime_fallbacks": 1},
		"reviewer-context": {"query_calls": 1, "bytes": 321, "fallbacks": 1, "runtime_fallbacks": 1},
	}


def test_parse_log_preserves_codex_and_openrouter_counts_with_semble_present() -> None:
	log = """
tokens used
12,345
INFO: openrouter usage phase=review call=pass1 model=openai/gpt-5.4 cache_enabled=true cache_breakpoint_enabled=false cache_breakpoint_fallback_retry=false prompt_tokens=100 completion_tokens=25 total_tokens=125 cache_creation_input_tokens=30 cache_read_input_tokens=40
SEMBLE_QUERY target=judge chunks=4 bytes=88 ms=3
SEMBLE_FALLBACK target=judge reason=index-unavailable ms=1
"""

	parsed = parse_log(log)

	assert parsed["codex_tokens_used"] == 12345
	assert parsed["codex_calls"] == 1
	assert parsed["or_prompt_tokens"] == 100
	assert parsed["or_completion_tokens"] == 25
	assert parsed["or_total_tokens"] == 125
	assert parsed["or_cache_write_tokens"] == 30
	assert parsed["or_cache_read_tokens"] == 40
	assert parsed["or_calls"] == 1
	assert parsed["or_phases"] == {
		"review": {
			"prompt_tokens": 100,
			"completion_tokens": 25,
			"total_tokens": 125,
			"calls": 1,
		}
	}
	assert parsed["semble_targets"] == {
		"judge": {"query_calls": 1, "bytes": 88, "fallbacks": 1, "runtime_fallbacks": 1}
	}


def test_parse_log_accepts_runtime_openrouter_usage_placeholders() -> None:
	log = """
INFO: openrouter usage phase=review call=review model=openai/gpt-5.4 cache_enabled=true cache_breakpoint_enabled=na cache_breakpoint_fallback_retry=na prompt_tokens=na completion_tokens=na total_tokens=na cache_creation_input_tokens=na cache_read_input_tokens=na
"""

	parsed = parse_log(log)

	assert parsed["or_calls"] == 1
	assert parsed["or_total_tokens"] == 0
	assert parsed["or_phases"] == {
		"review": {
			"prompt_tokens": 0,
			"completion_tokens": 0,
			"total_tokens": 0,
			"calls": 1,
		}
	}


def test_parse_log_accepts_comma_formatted_openrouter_usage_counts() -> None:
	log = """
INFO: openrouter usage phase=review call=review model=openai/gpt-5.4 cache_enabled=true cache_breakpoint_enabled=na cache_breakpoint_fallback_retry=na prompt_tokens=100,000 completion_tokens=25,000 total_tokens=125,000 cache_creation_input_tokens=30,000 cache_read_input_tokens=40,000
"""

	parsed = parse_log(log)

	assert parsed["or_prompt_tokens"] == 100000
	assert parsed["or_completion_tokens"] == 25000
	assert parsed["or_total_tokens"] == 125000
	assert parsed["or_cache_write_tokens"] == 30000
	assert parsed["or_cache_read_tokens"] == 40000
	assert parsed["or_phases"] == {
		"review": {
			"prompt_tokens": 100000,
			"completion_tokens": 25000,
			"total_tokens": 125000,
			"calls": 1,
		}
	}


def test_parse_log_splits_contract_test_semble_fallbacks_from_runtime() -> None:
	log = """
SEMBLE_FALLBACK target=reviewer-context reason=timeout context=contract-test ms=5000
SEMBLE_FALLBACK target=overflow reason=exit=7 raw failure from semble ms=11
"""

	parsed = parse_log(log)

	assert parsed["semble_fallbacks"] == 2
	assert parsed["semble_contract_test_fallbacks"] == 1
	assert parsed["semble_runtime_fallbacks"] == 1
	assert parsed["semble_targets"] == {
		"overflow": {"fallbacks": 1, "runtime_fallbacks": 1},
		"reviewer-context": {"fallbacks": 1, "contract_test_fallbacks": 1},
	}


def test_parse_log_fails_open_on_partial_or_malformed_semble_lines() -> None:
	log = """
SEMBLE_QUERY target=overflow chunks=6 ms=7
SEMBLE_QUERY bytes=17 ms=1
SEMBLE_FALLBACK reason=timeout ms=5000
SEMBLE_FALLBACK target=conflict-resolver reason=exit=9 stderr tail with spaces ms=19
"""

	parsed = parse_log(log)

	assert parsed["semble_query_calls"] == 2
	assert parsed["semble_query_bytes"] == 17
	assert parsed["semble_fallbacks"] == 2
	assert parsed["semble_contract_test_fallbacks"] == 0
	assert parsed["semble_runtime_fallbacks"] == 2
	assert parsed["semble_targets"] == {
		"overflow": {"query_calls": 1, "bytes": 0},
		"unknown": {"query_calls": 1, "bytes": 17, "fallbacks": 1, "runtime_fallbacks": 1},
		"conflict-resolver": {"fallbacks": 1, "runtime_fallbacks": 1},
	}


def test_context_budget_warn_fixture_generates_and_parses_review_telemetry() -> None:
	assert PHASE_H_CONTEXT_BUDGET_OVERFLOW_FIXTURE.exists()

	warn_line = build_context_budget_warn_line_for_file(
		phase="consolidator",
		prompt_path=PHASE_H_CONTEXT_BUDGET_OVERFLOW_FIXTURE,
		model="openai/gpt-5.4",
	)

	assert warn_line is not None
	assert warn_line.startswith("CONTEXT_BUDGET_WARN: phase=consolidator ")
	assert "model_context_window=272000" in warn_line
	assert "threshold=190400" in warn_line

	log = "\n".join(
		[
			"tokens used\n12,345",
			"INFO: openrouter usage phase=review call=pass1 model=openai/gpt-5.4 cache_enabled=true cache_breakpoint_enabled=false cache_breakpoint_fallback_retry=false prompt_tokens=100 completion_tokens=25 total_tokens=125 cache_creation_input_tokens=30 cache_read_input_tokens=40",
			"BREAK_GLASS: phase=editor reason=manual-override",
			warn_line,
		]
	)

	parsed = parse_log(log, fallback_wall_clock_ms=3210)
	assert parsed["codex_tokens_used"] == 12345
	assert parsed["break_glass_count"] == 1
	assert parsed["context_budget_warn_count"] == 1
	assert parsed["cache_hit_rate"] == 0.235294
	assert parsed["wall_clock_p50_ms"] == 3210
	assert parsed["wall_clock_p99_ms"] == 3210

	telemetry = build_run_cost_telemetry(log, fallback_wall_clock_ms=3210)
	assert telemetry["log_parsed"] is True
	assert telemetry["break_glass_count"] == 1
	assert telemetry["context_budget_warn_count"] == 1

	aggregate = aggregate_run_cost_telemetry([{"cost_telemetry": telemetry}])
	assert aggregate["runs_with_log_telemetry"] == 1
	assert aggregate["break_glass_count"] == 1
	assert aggregate["context_budget_warn_count"] == 1
	assert aggregate["wall_clock_p50_ms"] == 3210
	assert aggregate["wall_clock_p99_ms"] == 3210

	override = build_context_budget_warning(
		phase="review",
		prompt_tokens=200,
		model="openai/gpt-5.4",
		env={"MAX_PROMPT_TOKENS_FOR_PHASE": "128"},
	)
	assert override is not None
	assert override["threshold"] == 128


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
		except Exception as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
