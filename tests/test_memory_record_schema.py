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


def test_build_scope_treats_blank_override_as_unset() -> None:
	scope = ai_memory_lib._build_scope(42, None, None, "   ")
	assert scope == {"level": "task", "issue_number": 42, "pr_number": None, "run_id": None}


def main() -> int:
	test_memory_record_schema_accepts_repo_learnings_without_version_bump()
	test_build_scope_treats_blank_override_as_unset()
	print("OK: memory record schema accepts repo_learnings additively")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
