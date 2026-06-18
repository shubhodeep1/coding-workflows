#!/usr/bin/env python3
"""Tests for the opt-in prompt compaction helper."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "openrouter_prompt_cache.py"

if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("openrouter_prompt_cache", MODULE_PATH)
assert spec is not None and spec.loader is not None
openrouter_prompt_cache = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = openrouter_prompt_cache
spec.loader.exec_module(openrouter_prompt_cache)


def test_compact_if_over_budget_empty_input() -> None:
	assert openrouter_prompt_cache.compact_if_over_budget([], 5) == ""


def test_compact_if_over_budget_under_budget_passthrough() -> None:
	sections = [
		(1, "system", "A" * 4),
		(3, "appendix", "B" * 4),
	]
	original = list(sections)
	compacted = openrouter_prompt_cache.compact_if_over_budget(sections, 3)
	assert compacted == "AAAA\n\nBBBB"
	assert sections == original


def test_compact_if_over_budget_none_uses_env_budget() -> None:
	sections = [
		(1, "system", "A" * 8),
		(3, "low-priority", "B" * 8),
		(2, "higher-priority", "C" * 8),
	]
	previous_budget_tokens = os.environ.get("OPENROUTER_PROMPT_BUDGET_TOKENS")
	try:
		os.environ["OPENROUTER_PROMPT_BUDGET_TOKENS"] = "5"
		assert openrouter_prompt_cache.compact_if_over_budget(sections, None) == (
			"AAAAAAAA\n\nCCCCCCCC"
		)
	finally:
		if previous_budget_tokens is None:
			os.environ.pop("OPENROUTER_PROMPT_BUDGET_TOKENS", None)
		else:
			os.environ["OPENROUTER_PROMPT_BUDGET_TOKENS"] = previous_budget_tokens


def test_compact_if_over_budget_drops_tier_three_before_tier_two() -> None:
	sections = [
		(1, "system", "A" * 8),
		(3, "low-priority", "B" * 8),
		(2, "higher-priority", "C" * 8),
	]
	assert openrouter_prompt_cache.compact_if_over_budget(sections, 5) == "AAAAAAAA\n\nCCCCCCCC"


def test_compact_if_over_budget_extreme_case_keeps_only_tier_one() -> None:
	sections = [
		(1, "system", "A" * 8),
		(2, "issue", "B" * 8),
		(3, "supplement", "C" * 8),
	]
	assert openrouter_prompt_cache.compact_if_over_budget(sections, 1) == "AAAAAAAA"


def test_compact_if_over_budget_negative_budget_clamps_to_zero() -> None:
	sections = [
		(1, "system", "A" * 8),
		(2, "issue", "B" * 8),
		(3, "supplement", "C" * 8),
	]
	assert openrouter_prompt_cache.compact_if_over_budget(sections, -1) == "AAAAAAAA"


def main() -> int:
	test_compact_if_over_budget_empty_input()
	test_compact_if_over_budget_under_budget_passthrough()
	test_compact_if_over_budget_none_uses_env_budget()
	test_compact_if_over_budget_drops_tier_three_before_tier_two()
	test_compact_if_over_budget_extreme_case_keeps_only_tier_one()
	test_compact_if_over_budget_negative_budget_clamps_to_zero()
	print("PASS")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
