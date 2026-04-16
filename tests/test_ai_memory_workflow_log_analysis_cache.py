#!/usr/bin/env python3
"""Tests for workflow log analysis cache helpers in ai_memory_lib."""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
from contextlib import redirect_stderr
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
		"repositories": {
			"owner/repo": {
				"runs_etag": "W/\"abc\"",
				"runs_window_start": "2026-04-01T00:00:00Z",
				"jobs_seen_set": [101, 102],
				"logs_seen_set": [101],
				"last_updated": "2026-04-11T00:00:00Z",
				"runs_snapshot": [],
				"rows_snapshot": [],
			}
		},
	}


def test_workflow_log_analysis_cache_round_trip() -> None:
	with tempfile.TemporaryDirectory(prefix="memory-cache-") as td:
		memory_root = Path(td)
		ai_memory_lib.ensure_memory_layout(memory_root)
		ai_memory_lib._sync_memory_reference_files(REPO_ROOT / "ai-memory", memory_root)

		payload = _valid_payload()
		written = ai_memory_lib.put_workflow_log_analysis_cache(memory_root, payload)
		assert written == payload

		loaded = ai_memory_lib.get_workflow_log_analysis_cache(memory_root)
		assert loaded == payload


def test_workflow_log_analysis_cache_corruption_fails_open() -> None:
	with tempfile.TemporaryDirectory(prefix="memory-cache-") as td:
		memory_root = Path(td)
		ai_memory_lib.ensure_memory_layout(memory_root)
		ai_memory_lib._sync_memory_reference_files(REPO_ROOT / "ai-memory", memory_root)

		cache_path = memory_root / "global" / "cache" / "workflow_log_analysis_cache.v1.json"
		cache_path.parent.mkdir(parents=True, exist_ok=True)
		cache_path.write_text("not-json", encoding="utf-8")

		stderr_buf = io.StringIO()
		with redirect_stderr(stderr_buf):
			loaded = ai_memory_lib.get_workflow_log_analysis_cache(memory_root)

		assert loaded == {"schema_version": "v1", "repositories": {}}
		assert "rate_limit_audit_fallback" in stderr_buf.getvalue()


def test_workflow_log_analysis_cache_schema_mismatch_fails_open_on_write() -> None:
	with tempfile.TemporaryDirectory(prefix="memory-cache-") as td:
		memory_root = Path(td)
		ai_memory_lib.ensure_memory_layout(memory_root)
		ai_memory_lib._sync_memory_reference_files(REPO_ROOT / "ai-memory", memory_root)

		invalid = _valid_payload()
		invalid["schema_version"] = "v2"

		stderr_buf = io.StringIO()
		with redirect_stderr(stderr_buf):
			written = ai_memory_lib.put_workflow_log_analysis_cache(memory_root, invalid)

		assert written is None
		assert "rate_limit_audit_fallback" in stderr_buf.getvalue()


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
