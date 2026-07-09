#!/usr/bin/env python3
"""Contract tests for ai-memory record IDs."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest


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


@pytest.mark.parametrize("current_prefix", ("mem", "run_event"))
def test_make_record_id_matches_contractual_regex_for_documented_prefixes(current_prefix: str) -> None:
	record_id_value = ai_memory_lib.make_record_id(current_prefix)
	prefix_part, _, _ = _assert_record_id_tail(record_id_value)
	assert prefix_part == current_prefix


@pytest.mark.parametrize("fallback_prefix_input", ("", "   ", "!!!"))
def test_make_record_id_falls_back_to_mem_for_empty_whitespace_and_invalid_only_prefixes(fallback_prefix_input: str) -> None:
	record_id_value = ai_memory_lib.make_record_id(fallback_prefix_input)
	prefix_part, _, _ = _assert_record_id_tail(record_id_value)
	assert prefix_part == "mem"


@pytest.mark.parametrize(
	("prefix_input", "expected_prefix"),
	(
		(" Run Event ", "Run_Event"),
		(" .Run.Event:part-1_ ", "Run.Event:part-1"),
	),
)
def test_make_record_id_preserves_current_sanitization_behavior(prefix_input: str, expected_prefix: str) -> None:
	record_id_value = ai_memory_lib.make_record_id(prefix_input)
	prefix_part, _, _ = _assert_record_id_tail(record_id_value)
	assert prefix_part == expected_prefix
