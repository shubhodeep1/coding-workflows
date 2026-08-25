#!/usr/bin/env python3
"""Focused parser coverage for Serena and generic MCP telemetry in cost_audit.py."""
from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from cost_audit import parse_log  # noqa: E402


def test_parse_log_aggregates_serena_queries_by_target_and_tool() -> None:
	log = """
SERENA_QUERY target=implement tool=find_symbol calls=2 response_bytes=300 ms=10
SERENA_QUERY target=implement tool=search_for_pattern calls=1 response_bytes=50 ms=5
SERENA_QUERY target=validate tool=find_symbol calls=4 response_bytes=700 ms=20
"""

	parsed = parse_log(log)

	assert parsed["serena_query_calls"] == 3
	assert parsed["serena_query_response_bytes"] == 1050
	assert parsed["serena_query_tool_calls"] == 7
	assert parsed["serena_query_ms"] == 35
	assert parsed["serena_targets"] == {
		"implement": {
			"query_calls": 2,
			"response_bytes": 350,
			"tool_calls": 3,
			"ms": 15,
		},
		"validate": {
			"query_calls": 1,
			"response_bytes": 700,
			"tool_calls": 4,
			"ms": 20,
		},
	}
	assert parsed["serena_tools"] == {
		"find_symbol": {"calls": 6, "response_bytes": 1000, "ms": 30},
		"search_for_pattern": {"calls": 1, "response_bytes": 50, "ms": 5},
	}


def test_parse_log_aggregates_serena_fallbacks_by_target() -> None:
	log = """
SERENA_FALLBACK target=implement reason=setup-failure
SERENA_FALLBACK target=validate phase=discover reason=disabled
SERENA_FALLBACK phase=diagnose target=validate reason=setup-failure
"""

	parsed = parse_log(log)

	assert parsed["serena_fallbacks"] == 3
	assert parsed["serena_targets"] == {
		"implement": {"fallbacks": 1},
		"validate": {"fallbacks": 2},
	}


def test_parse_log_buckets_serena_probe_results() -> None:
	log = """
SERENA_PROBE target=setup result=ok
SERENA_PROBE target=setup result=failed reason=spawn-failed
SERENA_PROBE target=validate result=skipped reason=disabled
SERENA_PROBE target=review result=garbage reason=unexpected
"""

	parsed = parse_log(log)

	assert parsed["serena_probe_ok"] == 1
	assert parsed["serena_probe_failed"] == 1
	assert parsed["serena_probe_skipped"] == 1
	assert parsed["serena_targets"] == {
		"setup": {"probe_ok": 1, "probe_failed": 1},
		"validate": {"probe_skipped": 1},
	}


def test_parse_log_preserves_other_signal_types_when_serena_is_present() -> None:
	log = """
tokens used
12,345
INFO: openrouter usage phase=review call=pass1 model=openai/gpt-5.4 cache_enabled=true cache_breakpoint_enabled=false cache_breakpoint_fallback_retry=false prompt_tokens=100 completion_tokens=25 total_tokens=125 cache_creation_input_tokens=30 cache_read_input_tokens=40
SEMBLE_QUERY target=judge chunks=4 bytes=88 ms=3
SEMBLE_FALLBACK target=judge reason=index-unavailable ms=1
SERENA_QUERY target=implement tool=find_symbol calls=3 response_bytes=210 ms=9
SERENA_FALLBACK target=implement reason=disabled
SERENA_PROBE target=implement result=failed reason=spawn-failed
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
	assert parsed["semble_query_calls"] == 1
	assert parsed["semble_query_bytes"] == 88
	assert parsed["semble_fallbacks"] == 1
	assert parsed["semble_targets"] == {
		"judge": {
			"query_calls": 1,
			"bytes": 88,
			"fallbacks": 1,
			"runtime_fallbacks": 1,
		}
	}
	assert parsed["serena_query_calls"] == 1
	assert parsed["serena_query_response_bytes"] == 210
	assert parsed["serena_query_tool_calls"] == 3
	assert parsed["serena_query_ms"] == 9
	assert parsed["serena_fallbacks"] == 1
	assert parsed["serena_probe_ok"] == 0
	assert parsed["serena_probe_failed"] == 1
	assert parsed["serena_probe_skipped"] == 0
	assert parsed["serena_targets"] == {
		"implement": {
			"query_calls": 1,
			"response_bytes": 210,
			"tool_calls": 3,
			"ms": 9,
			"fallbacks": 1,
			"probe_failed": 1,
		}
	}
	assert parsed["serena_tools"] == {
		"find_symbol": {"calls": 3, "response_bytes": 210, "ms": 9}
	}
	assert parsed["other_mcp"] == {}


def test_parse_log_routes_unknown_mcp_servers_to_other_mcp() -> None:
	log = """
FOO_QUERY target=alpha bytes=100
FOO_FALLBACK target=alpha reason=timeout
FOO_PROBE target=alpha result=failed
BAR_QUERY target=beta response_bytes=250
BAR_PROBE target=beta result=ok
"""

	parsed = parse_log(log)

	assert parsed["other_mcp"] == {
		"FOO": {
			"query_calls": 1,
			"query_bytes": 100,
			"fallbacks": 1,
			"probe_failed": 1,
		},
		"BAR": {
			"query_calls": 1,
			"query_response_bytes": 250,
			"probe_ok": 1,
		},
	}


def test_parse_log_does_not_route_known_servers_to_other_mcp() -> None:
	log = """
SEMBLE_QUERY target=overflow chunks=2 bytes=12 ms=1
SERENA_QUERY target=implement tool=find_symbol calls=1 response_bytes=34 ms=5
"""

	parsed = parse_log(log)

	assert parsed["semble_query_calls"] == 1
	assert parsed["serena_query_calls"] == 1
	assert parsed["other_mcp"] == {}


def test_parse_log_ignores_partial_malformed_and_echoed_serena_lines() -> None:
	log = """
SERENA_QUERY tool=find_symbol
SERENA_QUERY target=validate response_bytes=17
SERENA_QUERY calls=5 ms=11
SERENA_QUERY target=validate tool=find_symbol calls=five response_bytes=17 ms=11
SERENA_QUERY 0
report says SERENA_QUERY traffic increased
SERENA_FALLBACK reason=timeout
SERENA_FALLBACK target=validate
SERENA_FALLBACK 0
SERENA_PROBE result=failed
SERENA_PROBE target=validate result=garbage
SERENA_PROBE 0
"""

	parsed = parse_log(log)

	assert parsed["serena_query_calls"] == 0
	assert parsed["serena_query_response_bytes"] == 0
	assert parsed["serena_query_tool_calls"] == 0
	assert parsed["serena_query_ms"] == 0
	assert parsed["serena_fallbacks"] == 0
	assert parsed["serena_probe_failed"] == 0
	assert parsed["serena_probe_skipped"] == 0
	assert parsed["serena_targets"] == {}
	assert parsed["serena_tools"] == {}


def test_parse_log_ignores_partial_malformed_and_echoed_generic_mcp_lines() -> None:
	log = """
FOO_QUERY target=alpha
FOO_QUERY target=alpha bytes=many
FOO_QUERY 0
report says FOO_QUERY traffic increased
FOO_FALLBACK reason=timeout
FOO_FALLBACK target=alpha
FOO_FALLBACK 0
FOO_PROBE result=failed
FOO_PROBE target=alpha result=garbage
FOO_PROBE 0
"""

	parsed = parse_log(log)

	assert parsed["other_mcp"] == {}


def test_parse_log_bounds_single_line_with_multiple_query_prefixes() -> None:
	log = """
SERENA_QUERY target=alpha tool=find_symbol calls=2 response_bytes=50 ms=3 FOO_QUERY target=beta bytes=99
"""

	parsed = parse_log(log)

	assert parsed["serena_query_calls"] == 1
	assert parsed["serena_targets"] == {
		"alpha": {
			"query_calls": 1,
			"response_bytes": 50,
			"tool_calls": 2,
			"ms": 3,
		}
	}
	assert parsed["serena_tools"] == {
		"find_symbol": {"calls": 2, "response_bytes": 50, "ms": 3}
	}
	assert parsed["other_mcp"] == {}


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
