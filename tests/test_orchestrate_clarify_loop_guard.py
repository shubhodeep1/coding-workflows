#!/usr/bin/env python3
"""Unit tests for clarify loop guard behavior in ai_memory_lib."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "ai_memory_lib.py"

spec = importlib.util.spec_from_file_location("ai_memory_lib", MODULE_PATH)
assert spec is not None and spec.loader is not None
ai_memory_lib = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ai_memory_lib
spec.loader.exec_module(ai_memory_lib)


def _entry(comment_id: int, clarify_hash: str, cycle: int, *, answer_comment_id: int) -> dict:
	return {
		"entry_id": f"processed_command_10_{comment_id}_answer",
		"schema_version": "processed_command_entry.v1",
		"issue_number": 10,
		"comment_id": comment_id,
		"command": "answer",
		"workflow": "orchestrate_clarify_respond",
		"status": "answered",
		"actor": "github-actions[bot]",
		"run_id": "111",
		"run_attempt": 1,
		"timestamp": f"2026-04-15T08:0{cycle}:00Z",
		"metadata": {
			"clarify_hash": clarify_hash,
			"answer_hash": f"answer-{cycle}",
			"clarify_comment_id": comment_id,
			"answer_comment_id": answer_comment_id,
			"cycle": cycle,
			"loop_blocked": False,
			"loop_block_reason": "none",
		},
	}


def test_identical_clarify_hash_blocks_on_second_occurrence() -> None:
	clarify_hash = ai_memory_lib.compute_normalized_sha256("Clarification required: Provide region")
	entries = [_entry(101, clarify_hash, 1, answer_comment_id=102)]

	result = ai_memory_lib.evaluate_clarify_loop_guard(
		entries,
		clarify_hash=clarify_hash,
		max_cycles=3,
		current_comment_id=103,
	)

	assert result["blocked"] is True
	assert result["reason"] == "repeat_clarify_hash"
	assert result["cycle"] == 2
	assert result["previous_clarify_comment_id"] == 101
	assert result["previous_answer_comment_id"] == 102


def test_cycle_budget_blocks_n_plus_one_even_with_distinct_hashes() -> None:
	entries = [
		_entry(201, ai_memory_lib.compute_normalized_sha256("Question A"), 1, answer_comment_id=202),
		_entry(203, ai_memory_lib.compute_normalized_sha256("Question B"), 2, answer_comment_id=204),
	]
	incoming_hash = ai_memory_lib.compute_normalized_sha256("Question C")

	result = ai_memory_lib.evaluate_clarify_loop_guard(
		entries,
		clarify_hash=incoming_hash,
		max_cycles=2,
		current_comment_id=205,
	)

	assert result["blocked"] is True
	assert result["reason"] == "max_cycles_exceeded"
	assert result["cycle"] == 3
	assert result["max_cycles"] == 2
	assert result["previous_clarify_comment_id"] == 203
	assert result["previous_answer_comment_id"] == 204


def test_normalization_is_stable_for_whitespace_and_markdown_drift() -> None:
	base = "Clarification Required: **Target Region**  is  `US-East`"
	drifted = "clarification required target region is us-east"

	assert ai_memory_lib.compute_normalized_sha256(base) == ai_memory_lib.compute_normalized_sha256(drifted)
