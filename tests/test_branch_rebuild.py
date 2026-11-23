#!/usr/bin/env python3
"""Tests for branch rebuild audit helpers in ai_memory_lib."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from contextlib import contextmanager
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


def _valid_audit() -> dict:
	return {
		"schema_version": "v1",
		"repository": "owner/repo",
		"tracking_issue_number": 192,
		"integration_branch": "orchestrator/project-192",
		"default_branch": "main",
		"last_rebuild_at": "2026-05-21T10:00:00Z",
		"trigger_reason": "resolver_escalated_threshold",
		"resolver_escalated_at": "2026-05-20T00:00:00Z",
		"final_pr_number": 357,
		"final_pr_head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"pre_rebuild_branch_head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"default_branch_head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		"replay_commits": [
			{
				"issue_number": 10,
				"pr_number": 901,
				"merge_commit_sha": "cccccccccccccccccccccccccccccccccccccccc",
				"merged_at": "2026-05-19T12:00:00Z",
			},
		],
		"outcome": "success",
		"branch_protected": False,
		"failure_detail": None,
		"completed_at": "2026-05-21T10:05:00Z",
	}


@contextmanager
def _memory_root():
	with tempfile.TemporaryDirectory(prefix="branch-rebuild-audit-") as td:
		memory_root = Path(td) / "ai-memory"
		ai_memory_lib.ensure_memory_layout(memory_root)
		ai_memory_lib._sync_memory_reference_files(REPO_ROOT / "ai-memory", memory_root)
		yield memory_root


def test_branch_rebuild_audit_round_trip() -> None:
	with _memory_root() as memory_root:
		audit = _valid_audit()
		written = ai_memory_lib.put_branch_rebuild_audit(
			memory_root,
			repository="owner/repo",
			tracking_issue_number=192,
			integration_branch="orchestrator/project-192",
			audit=audit,
		)
		loaded = ai_memory_lib.get_branch_rebuild_audit(
			memory_root,
			repository="owner/repo",
			tracking_issue_number=192,
			integration_branch="orchestrator/project-192",
		)
		assert written == loaded
		assert (memory_root / "orchestrator" / "branch_rebuild_audits" / "owner__repo" / "issue-192__orchestrator_project-192.json").exists()


def test_branch_rebuild_audit_rejects_schema_mismatch() -> None:
	with _memory_root() as memory_root:
		audit_path = memory_root / "orchestrator" / "branch_rebuild_audits" / "owner__repo" / "issue-192__orchestrator_project-192.json"
		audit_path.parent.mkdir(parents=True, exist_ok=True)
		invalid = _valid_audit()
		invalid["schema_version"] = "v0"
		audit_path.write_text(json.dumps(invalid), encoding="utf-8")
		try:
			ai_memory_lib.get_branch_rebuild_audit(
				memory_root,
				repository="owner/repo",
				tracking_issue_number=192,
				integration_branch="orchestrator/project-192",
			)
		except ai_memory_lib.MemoryValidationError as exc:
			assert "schema_version" in str(exc)
			return
		assert False, "Expected branch rebuild audit schema validation failure"


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
		except Exception as exc:  # noqa: BLE001
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
