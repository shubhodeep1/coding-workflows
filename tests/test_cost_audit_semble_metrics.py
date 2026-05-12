#!/usr/bin/env python3
"""Focused parser coverage for Semble/Serena telemetry in scripts/cost_audit.py."""
from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from cost_audit import _finalize_serena_summary, parse_log  # noqa: E402


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
	assert parsed["semble_targets"] == {
		"overflow": {"query_calls": 1, "bytes": 1200, "fallbacks": 1},
		"reviewer-context": {"query_calls": 1, "bytes": 321, "fallbacks": 1},
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
		"judge": {"query_calls": 1, "bytes": 88, "fallbacks": 1}
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
	assert parsed["semble_targets"] == {
		"overflow": {"query_calls": 1, "bytes": 0},
		"unknown": {"query_calls": 1, "bytes": 17, "fallbacks": 1},
		"conflict-resolver": {"fallbacks": 1},
	}


def test_parse_log_counts_serena_rollups_by_target_and_query_calls() -> None:
	log = """
SERENA_QUERY target=implement tool=find_symbol calls=3 response_bytes=1500 ms=20
SERENA_QUERY target=implement tool=find_referencing_symbols calls=2 response_bytes=9000 ms=40
SERENA_QUERY target=validate tool=search_for_pattern calls=4 response_bytes=12000 ms=9
SERENA_FALLBACK target=validate phase=diagnose reason=setup-failure
SERENA_FALLBACK target=implement reason=probe-failure
"""

	parsed = parse_log(log)

	assert parsed["serena_query_rollups"] == 3
	assert parsed["serena_query_calls"] == 9
	assert parsed["serena_query_bytes"] == 22500
	assert parsed["serena_fallbacks"] == 2
	assert parsed["serena_fallback_ratio"] == 0.4
	assert parsed["serena_legacy_prompt_bytes_estimate"] == 57936
	assert parsed["serena_legacy_prompt_tokens_estimate"] == 14484
	assert parsed["serena_observed_prompt_tokens"] is None
	assert parsed["serena_observed_prompt_tokens_source"] is None
	assert parsed["serena_targets"] == {
		"implement": {
			"query_rollups": 2,
			"query_calls": 5,
			"response_bytes": 10500,
			"fallbacks": 1,
			"legacy_prompt_bytes_estimate": 33360,
			"legacy_prompt_tokens_estimate": 8340,
			"fallback_ratio": 0.3333,
		},
		"validate": {
			"query_rollups": 1,
			"query_calls": 4,
			"response_bytes": 12000,
			"fallbacks": 1,
			"legacy_prompt_bytes_estimate": 24576,
			"legacy_prompt_tokens_estimate": 6144,
			"fallback_ratio": 0.5,
		},
	}


def test_parse_log_compares_serena_legacy_estimate_to_observed_prompt_tokens() -> None:
	log = """
INFO: openrouter usage phase=review call=pass1 model=openai/gpt-5.4 cache_enabled=true cache_breakpoint_enabled=false cache_breakpoint_fallback_retry=false prompt_tokens=200 completion_tokens=25 total_tokens=225 cache_creation_input_tokens=0 cache_read_input_tokens=0
SERENA_QUERY target=review_autofix tool=find_symbol calls=2 response_bytes=6000 ms=12
SERENA_FALLBACK target=review-autofix-editor reason=probe-failure
"""

	parsed = parse_log(log)

	assert parsed["serena_query_rollups"] == 1
	assert parsed["serena_query_calls"] == 2
	assert parsed["serena_fallback_ratio"] == 0.5
	assert parsed["serena_legacy_prompt_bytes_estimate"] == 10240
	assert parsed["serena_legacy_prompt_tokens_estimate"] == 2560
	assert parsed["serena_observed_prompt_tokens"] == 200
	assert parsed["serena_observed_prompt_tokens_source"] == "openrouter_prompt_tokens"
	assert parsed["serena_legacy_prompt_tokens_delta_vs_observed"] == 2360
	assert parsed["serena_legacy_prompt_tokens_ratio_vs_observed"] == 12.8
	assert parsed["serena_targets"] == {
		"review_autofix": {
			"query_rollups": 1,
			"query_calls": 2,
			"response_bytes": 6000,
			"fallbacks": 1,
			"legacy_prompt_bytes_estimate": 10240,
			"legacy_prompt_tokens_estimate": 2560,
			"fallback_ratio": 0.5,
		}
	}


def test_parse_log_fails_open_on_partial_or_malformed_serena_lines() -> None:
	log = """
SERENA_QUERY target=implement tool=find_symbol calls=3 ms=20
SERENA_QUERY target=review-autofix-editor tool=find_referencing_symbols response_bytes=9000 ms=40
SERENA_QUERY response_bytes=15 ms=1
SERENA_FALLBACK phase=diagnose reason=setup-failure
SERENA_FALLBACK target=validate phase=self-heal reason=disabled
"""

	parsed = parse_log(log)

	assert parsed["serena_query_rollups"] == 1
	assert parsed["serena_query_calls"] == 3
	assert parsed["serena_query_bytes"] == 9015
	assert parsed["serena_fallbacks"] == 2
	assert parsed["serena_fallback_ratio"] == 0.6667
	assert parsed["serena_legacy_prompt_bytes_estimate"] == 15360
	assert parsed["serena_legacy_prompt_tokens_estimate"] == 3840
	assert parsed["serena_targets"] == {
		"implement": {
			"query_rollups": 1,
			"query_calls": 3,
			"response_bytes": 0,
			"legacy_prompt_bytes_estimate": 15360,
			"legacy_prompt_tokens_estimate": 3840,
			"fallback_ratio": 0.0,
		},
		"review_autofix": {
			"query_calls": 0,
			"response_bytes": 9000,
			"legacy_prompt_bytes_estimate": 0,
			"legacy_prompt_tokens_estimate": 0,
			"fallback_ratio": None,
		},
		"unknown": {
			"query_calls": 0,
			"response_bytes": 15,
			"fallbacks": 1,
			"legacy_prompt_bytes_estimate": 0,
			"legacy_prompt_tokens_estimate": 0,
			"fallback_ratio": 1.0,
		},
		"validate": {
			"fallbacks": 1,
			"fallback_ratio": 1.0,
		},
	}


def test_finalize_serena_summary_requires_same_run_prompt_token_evidence() -> None:
	summary = {
		"or_prompt_tokens": 999,
		"serena_query_rollups": 1,
		"serena_fallbacks": 0,
		"serena_legacy_prompt_tokens_estimate": 2048,
		"serena_observed_prompt_tokens": None,
		"serena_observed_prompt_tokens_source": None,
		"serena_targets": {},
	}

	_finalize_serena_summary(summary)

	assert summary["serena_fallback_ratio"] == 0.0
	assert summary["serena_observed_prompt_tokens"] is None
	assert summary["serena_observed_prompt_tokens_source"] is None
	assert summary["serena_legacy_prompt_tokens_delta_vs_observed"] is None
	assert summary["serena_legacy_prompt_tokens_ratio_vs_observed"] is None


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
