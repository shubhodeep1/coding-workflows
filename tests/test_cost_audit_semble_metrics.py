#!/usr/bin/env python3
"""Focused parser coverage for Semble telemetry in scripts/cost_audit.py."""
from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from cost_audit import parse_log  # noqa: E402


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
