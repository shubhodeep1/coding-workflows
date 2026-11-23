#!/usr/bin/env python3
"""Tests for _sync_memory_reference_files in ai_memory_lib.

Covers the SUPPORT_AI_MEMORY_DIR fallback path added so consumer repos
that do not vendor ai-memory/{schemas,config}/ still get the reference
files installed via review_autofix's support staging step.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "ai_memory_lib.py"

if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("ai_memory_lib", MODULE_PATH)
assert spec is not None and spec.loader is not None
ai_memory_lib = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ai_memory_lib
spec.loader.exec_module(ai_memory_lib)


@contextmanager
def _support_dir_env(value: str | None) -> Iterator[None]:
	original = os.environ.get("SUPPORT_AI_MEMORY_DIR")
	if value is None:
		os.environ.pop("SUPPORT_AI_MEMORY_DIR", None)
	else:
		os.environ["SUPPORT_AI_MEMORY_DIR"] = value
	try:
		yield
	finally:
		if original is None:
			os.environ.pop("SUPPORT_AI_MEMORY_DIR", None)
		else:
			os.environ["SUPPORT_AI_MEMORY_DIR"] = original


def _write_json(path: Path, payload: object) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(payload), encoding="utf-8")


def test_retrieval_profiles_prefers_consumer_tree_when_present() -> None:
	with tempfile.TemporaryDirectory(prefix="memory-sync-") as td:
		root = Path(td)
		consumer_root = root / "consumer"
		support_root = root / "support"
		destination_root = root / "dest"
		destination_root.mkdir()

		_write_json(consumer_root / "config" / "retrieval_profiles.v1.json", {"from": "consumer"})
		_write_json(support_root / "config" / "retrieval_profiles.v1.json", {"from": "support"})

		with _support_dir_env(str(support_root)):
			ai_memory_lib._sync_memory_reference_files(consumer_root, destination_root)

		installed = json.loads((destination_root / "config" / "retrieval_profiles.v1.json").read_text(encoding="utf-8"))
		assert installed == {"from": "consumer"}


def test_retrieval_profiles_falls_back_to_support_dir() -> None:
	with tempfile.TemporaryDirectory(prefix="memory-sync-") as td:
		root = Path(td)
		consumer_root = root / "consumer"
		consumer_root.mkdir()
		support_root = root / "support"
		destination_root = root / "dest"
		destination_root.mkdir()

		_write_json(support_root / "config" / "retrieval_profiles.v1.json", {"from": "support"})

		with _support_dir_env(str(support_root)):
			ai_memory_lib._sync_memory_reference_files(consumer_root, destination_root)

		installed = json.loads((destination_root / "config" / "retrieval_profiles.v1.json").read_text(encoding="utf-8"))
		assert installed == {"from": "support"}


def test_retrieval_profiles_no_copy_when_neither_source_has_file() -> None:
	with tempfile.TemporaryDirectory(prefix="memory-sync-") as td:
		root = Path(td)
		consumer_root = root / "consumer"
		consumer_root.mkdir()
		destination_root = root / "dest"
		destination_root.mkdir()

		with _support_dir_env(None):
			ai_memory_lib._sync_memory_reference_files(consumer_root, destination_root)

		assert not (destination_root / "config" / "retrieval_profiles.v1.json").exists()


def test_schemas_prefers_consumer_tree_when_present() -> None:
	with tempfile.TemporaryDirectory(prefix="memory-sync-") as td:
		root = Path(td)
		consumer_root = root / "consumer"
		support_root = root / "support"
		destination_root = root / "dest"
		destination_root.mkdir()

		_write_json(consumer_root / "schemas" / "memory_record.v1.json", {"from": "consumer"})
		_write_json(support_root / "schemas" / "memory_record.v1.json", {"from": "support"})

		with _support_dir_env(str(support_root)):
			ai_memory_lib._sync_memory_reference_files(consumer_root, destination_root)

		installed = json.loads((destination_root / "schemas" / "memory_record.v1.json").read_text(encoding="utf-8"))
		assert installed == {"from": "consumer"}


def test_schemas_falls_back_to_support_dir() -> None:
	with tempfile.TemporaryDirectory(prefix="memory-sync-") as td:
		root = Path(td)
		consumer_root = root / "consumer"
		consumer_root.mkdir()
		support_root = root / "support"
		destination_root = root / "dest"
		destination_root.mkdir()

		_write_json(support_root / "schemas" / "memory_record.v1.json", {"from": "support"})
		_write_json(support_root / "schemas" / "task_lineage.v1.json", {"from": "support"})

		with _support_dir_env(str(support_root)):
			ai_memory_lib._sync_memory_reference_files(consumer_root, destination_root)

		installed_record = json.loads((destination_root / "schemas" / "memory_record.v1.json").read_text(encoding="utf-8"))
		installed_lineage = json.loads((destination_root / "schemas" / "task_lineage.v1.json").read_text(encoding="utf-8"))
		assert installed_record == {"from": "support"}
		assert installed_lineage == {"from": "support"}


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
