#!/usr/bin/env python3
"""Tests for fingerprint quarantine helpers in ai_memory_lib."""

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


def _valid_payload() -> dict:
	return {
		"schema_version": "v1",
		"entries": [
			{
				"fp_key": ["scripts/example.py", "EXPECTED_TWO"],
				"issue_key": "2000",
				"first_seen_run_id": "2002",
				"last_seen_run_id": "2004",
				"consecutive_unchanged_runs": 3,
			},
			{
				"fp_key": ["scripts/example.py", "EXPECTED_ONE"],
				"issue_key": "1000",
				"first_seen_run_id": "1001",
				"last_seen_run_id": "1003",
				"consecutive_unchanged_runs": 2,
			},
		],
	}


@contextmanager
def _memory_root():
	with tempfile.TemporaryDirectory(prefix="fingerprint-quarantine-") as td:
		memory_root = Path(td) / "ai-memory"
		ai_memory_lib.ensure_memory_layout(memory_root)
		ai_memory_lib._sync_memory_reference_files(REPO_ROOT / "ai-memory", memory_root)
		yield memory_root


def test_fingerprint_quarantine_round_trip() -> None:
	with _memory_root() as memory_root:
		payload = _valid_payload()
		written = ai_memory_lib.put_fingerprint_quarantine(memory_root, payload)
		loaded = ai_memory_lib.get_fingerprint_quarantine(memory_root)
		assert written == loaded
		assert loaded["entries"] == [
			{
				"fp_key": ["scripts/example.py", "EXPECTED_ONE"],
				"issue_key": "1000",
				"first_seen_run_id": "1001",
				"last_seen_run_id": "1003",
				"consecutive_unchanged_runs": 2,
			},
			{
				"fp_key": ["scripts/example.py", "EXPECTED_TWO"],
				"issue_key": "2000",
				"first_seen_run_id": "2002",
				"last_seen_run_id": "2004",
				"consecutive_unchanged_runs": 3,
			},
		]
		assert (memory_root / "orchestrator" / "fingerprint_quarantine.v1.json").exists()


def test_fingerprint_quarantine_missing_file_returns_empty_payload() -> None:
	with _memory_root() as memory_root:
		loaded = ai_memory_lib.get_fingerprint_quarantine(memory_root)
		assert loaded == {"schema_version": "v1", "entries": []}


def test_fingerprint_quarantine_rejects_schema_mismatch() -> None:
	with _memory_root() as memory_root:
		quarantine_path = memory_root / "orchestrator" / "fingerprint_quarantine.v1.json"
		quarantine_path.parent.mkdir(parents=True, exist_ok=True)
		quarantine_path.write_text(json.dumps({"schema_version": "v0", "entries": []}), encoding="utf-8")
		try:
			ai_memory_lib.get_fingerprint_quarantine(memory_root)
		except ai_memory_lib.MemoryValidationError as exc:
			assert "schema_version" in str(exc)
			return
		assert False, "Expected quarantine payload schema validation failure"


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
