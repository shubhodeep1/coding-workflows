#!/usr/bin/env python3
"""Direct-run schema checks for additive AI-memory record categories."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "ai_memory_lib.py"
SCHEMA_PATH = REPO_ROOT / "ai-memory" / "schemas" / "memory_record.v1.json"
LESSONS_SCHEMA_PATH = REPO_ROOT / "ai-memory" / "schemas" / "lessons_learned_record.v1.json"

if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("ai_memory_lib", MODULE_PATH)
assert spec is not None and spec.loader is not None
ai_memory_lib = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ai_memory_lib
spec.loader.exec_module(ai_memory_lib)


def test_memory_record_schema_accepts_repo_learnings_without_version_bump() -> None:
	with tempfile.TemporaryDirectory(prefix="memory-record-schema-") as td:
		memory_root = Path(td) / "ai-memory"
		ai_memory_lib.ensure_memory_layout(memory_root)
		shutil.copytree(REPO_ROOT / "ai-memory" / "schemas", memory_root / "schemas", dirs_exist_ok=True)

		record = {
			"record_id": "mem-test-repo-learning",
			"schema_version": ai_memory_lib.MEMORY_RECORD_SCHEMA_VERSION,
			"category": "repo_learnings",
			"status": "candidate",
			"scope": {
				"level": "global",
				"issue_number": None,
				"pr_number": None,
				"run_id": None,
			},
			"summary": "Merged runs can promote stable repo learnings",
			"details": "Repository learnings are stored as additive global AI-memory records.",
			"confidence": 0.72,
			"sensitive": False,
			"fingerprint": "0" * 64,
			"provenance": {
				"workflow": "memory_maintenance",
				"run_id": "run-123",
				"run_attempt": 1,
				"actor": "github-actions[bot]",
				"source_refs": [],
			},
			"lineage": {
				"issue_number": None,
				"pr_number": None,
				"run_id": "run-123",
				"parent_ids": [],
				"supersedes": None,
				"superseded_by": None,
			},
			"timestamps": {
				"created_at": "2026-06-21T00:00:00Z",
				"promoted_at": None,
				"superseded_at": None,
			},
		}

		ai_memory_lib.validate_memory_record(record, memory_root)

		schema_payload = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
		assert schema_payload["properties"]["schema_version"]["const"] == "memory_record.v1"
		assert "repo_learnings" in schema_payload["properties"]["category"]["enum"]
		assert "prune_marked_at" in schema_payload["properties"]["timestamps"]["properties"]


def test_memory_record_schema_accepts_optional_prune_marked_at_timestamp() -> None:
	with tempfile.TemporaryDirectory(prefix="memory-record-prune-schema-") as td:
		memory_root = Path(td) / "ai-memory"
		ai_memory_lib.ensure_memory_layout(memory_root)
		shutil.copytree(REPO_ROOT / "ai-memory" / "schemas", memory_root / "schemas", dirs_exist_ok=True)

		record = {
			"record_id": "mem-test-prune-marker",
			"schema_version": ai_memory_lib.MEMORY_RECORD_SCHEMA_VERSION,
			"category": "task_summaries",
			"status": "candidate",
			"scope": {
				"level": "task",
				"issue_number": 42,
				"pr_number": None,
				"run_id": None,
			},
			"summary": "Prune-marked candidates stay schema-valid until maintenance archives them",
			"details": "The optional prune_marked_at timestamp lets compact() archive old records in the current month.",
			"confidence": 0.8,
			"sensitive": False,
			"fingerprint": "1" * 64,
			"provenance": {
				"workflow": "ai-implement",
				"run_id": "run-456",
				"run_attempt": 1,
				"actor": "codex-bot",
				"source_refs": [],
			},
			"lineage": {
				"issue_number": 42,
				"pr_number": None,
				"run_id": None,
				"parent_ids": [],
				"supersedes": None,
				"superseded_by": None,
			},
			"timestamps": {
				"created_at": "2026-06-21T00:00:00Z",
				"promoted_at": None,
				"prune_marked_at": "2026-06-26T00:00:00Z",
				"superseded_at": None,
			},
		}

		ai_memory_lib.validate_memory_record(record, memory_root)


def test_build_scope_treats_blank_override_as_unset() -> None:
	scope = ai_memory_lib._build_scope(42, None, None, "   ")
	assert scope == {"level": "task", "issue_number": 42, "pr_number": None, "run_id": None}


def test_lessons_learned_writer_persists_issue_scoped_schema_valid_record() -> None:
	with tempfile.TemporaryDirectory(prefix="lessons-learned-record-") as td:
		memory_root = Path(td) / "ai-memory"
		ai_memory_lib.ensure_memory_layout(memory_root)
		shutil.copytree(REPO_ROOT / "ai-memory" / "schemas", memory_root / "schemas", dirs_exist_ok=True)

		long_tag = "nested/" + ("segment-" * 40)
		long_lesson_text = "Validated autofix touched files outside the original change set. " + ("x" * 13000)

		records = ai_memory_lib.record_lessons_learned(
			memory_root,
			issue_number=42,
			pr_number=7,
			phase="review_autofix",
			lessons=[
				{
					"lesson_kind": "out_of_plan_fix",
					"lesson_text": long_lesson_text,
					"tags": [long_tag, "tests/test_memory_record_schema.py"],
				}
			],
		)

		assert len(records) == 1
		record = records[0]
		persisted_path = memory_root / "tasks" / "issue-42" / "lessons_learned" / f"{record['record_id']}.json"
		assert persisted_path.is_file()

		persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
		ai_memory_lib.validate_lessons_learned_record(persisted, memory_root)

		schema_payload = json.loads(LESSONS_SCHEMA_PATH.read_text(encoding="utf-8"))
		assert schema_payload["properties"]["schema_version"]["const"] == "lessons_learned_record.v1"
		assert persisted["issue_number"] == 42
		assert persisted["pr_number"] == 7
		assert persisted["phase"] == "review_autofix"
		assert persisted["lesson_kind"] == "out_of_plan_fix"
		assert persisted["lesson_text"] == long_lesson_text[:12000]
		assert persisted["tags"] == [long_tag[:256], "tests/test_memory_record_schema.py"]


def main() -> int:
	test_memory_record_schema_accepts_repo_learnings_without_version_bump()
	test_memory_record_schema_accepts_optional_prune_marked_at_timestamp()
	test_build_scope_treats_blank_override_as_unset()
	test_lessons_learned_writer_persists_issue_scoped_schema_valid_record()
	print("OK: memory record schema and lessons-learned writer stay additive")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
