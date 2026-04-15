#!/usr/bin/env python3
"""Schema and helper compatibility tests for processed command entries."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "ai_memory_lib.py"

if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("ai_memory_lib", MODULE_PATH)
assert spec is not None and spec.loader is not None
ai_memory_lib = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ai_memory_lib
spec.loader.exec_module(ai_memory_lib)


MEMORY_ROOT = REPO_ROOT / "ai-memory"


def _base_entry() -> dict:
	return {
		"entry_id": "processed_command_42_123456789_answer",
		"schema_version": "processed_command_entry.v1",
		"issue_number": 42,
		"comment_id": 123456789,
		"command": "answer",
		"workflow": "orchestrate_clarify_respond",
		"status": "claimed",
		"actor": "github-actions[bot]",
		"run_id": "987654321",
		"run_attempt": 1,
		"timestamp": "2026-03-22T11:30:00Z",
		"metadata": {
			"source": "issue_comment",
			"author_login": "octocat",
		},
	}


def test_processed_command_entry_legacy_metadata_is_still_valid() -> None:
	entry = _base_entry()
	ai_memory_lib.validate_processed_command_entry(entry, MEMORY_ROOT)


def test_processed_command_entry_accepts_clarify_loop_metadata() -> None:
	entry = _base_entry()
	entry["metadata"].update(
		{
			"clarify_hash": "dcf50f30d5dd8f6ca48e72b745ecdf7e96f5db84abaf5e26077f27de83b56dbf",
			"answer_hash": "2bdbbc90af3bc4e3f0a13c5395f37f3969b683f8362a8c4640f6dd84f1b20544",
			"clarify_comment_id": 123456789,
			"answer_comment_id": 123456790,
			"cycle": 1,
			"loop_blocked": False,
			"loop_block_reason": "none",
		}
	)
	ai_memory_lib.validate_processed_command_entry(entry, MEMORY_ROOT)


def test_processed_command_entry_rejects_malformed_clarify_hash() -> None:
	entry = _base_entry()
	entry["metadata"].update({"clarify_hash": "not-a-sha256", "cycle": 1})
	try:
		ai_memory_lib.validate_processed_command_entry(entry, MEMORY_ROOT)
	except ai_memory_lib.MemoryValidationError as exc:
		assert "clarify_hash" in str(exc)
		return
	assert False, "Expected schema validation failure for malformed clarify_hash"


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
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
