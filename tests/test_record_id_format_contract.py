#!/usr/bin/env python3
"""Contract tests for ai-memory record IDs."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "ai_memory_lib.py"
CONTRACTUAL_RECORD_ID_PATTERN = re.compile(
	r"^(?P<prefix>[A-Za-z0-9:-](?:[A-Za-z0-9_.:-]*"
	r"[A-Za-z0-9:-])?)_(?P<timestamp>\d{14})_(?P<suffix>[0-9a-f]{10})$"
)
HEX_SUFFIX_PATTERN = re.compile(r"^[0-9a-f]{10}$")

if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("ai_memory_lib_record_id_contract", MODULE_PATH)
assert spec is not None and spec.loader is not None
ai_memory_lib = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ai_memory_lib
spec.loader.exec_module(ai_memory_lib)


def _split_record_id(record_id: str) -> tuple[str, str, str]:
	prefix_part, timestamp_part, suffix_part = record_id.rsplit("_", 2)
	return prefix_part, timestamp_part, suffix_part


def _assert_record_id_tail(record_id: str) -> tuple[str, str, str]:
	assert CONTRACTUAL_RECORD_ID_PATTERN.fullmatch(record_id) is not None
	prefix_part, timestamp_part, suffix_part = _split_record_id(record_id)
	assert len(timestamp_part) == 14
	assert timestamp_part.isdigit()
	assert HEX_SUFFIX_PATTERN.fullmatch(suffix_part) is not None
	return prefix_part, timestamp_part, suffix_part


def test_make_record_id_matches_contractual_regex_for_documented_prefixes() -> None:
	for current_prefix in ("mem", "run_event"):
		record_id_value = ai_memory_lib.make_record_id(current_prefix)
		prefix_part, _, _ = _assert_record_id_tail(record_id_value)
		assert prefix_part == current_prefix, f"{current_prefix=} produced {prefix_part=}"


def test_make_record_id_falls_back_to_mem_for_empty_whitespace_and_invalid_only_prefixes() -> None:
	for fallback_prefix_input in ("", "   ", "!!!"):
		record_id_value = ai_memory_lib.make_record_id(fallback_prefix_input)
		prefix_part, _, _ = _assert_record_id_tail(record_id_value)
		assert prefix_part == "mem", f"{fallback_prefix_input!r} produced {prefix_part=}"


def test_make_record_id_preserves_current_sanitization_behavior() -> None:
	for prefix_input, expected_prefix in (
		(" Run Event ", "Run_Event"),
		(" .Run.Event:part-1_ ", "Run.Event:part-1"),
	):
		record_id_value = ai_memory_lib.make_record_id(prefix_input)
		prefix_part, _, _ = _assert_record_id_tail(record_id_value)
		assert prefix_part == expected_prefix, f"{prefix_input!r} produced {prefix_part=}"


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
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
