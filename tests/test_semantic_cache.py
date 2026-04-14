#!/usr/bin/env python3
"""Tests for scripts/semantic_cache.py."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "semantic_cache.py"

spec = importlib.util.spec_from_file_location("semantic_cache", MODULE_PATH)
assert spec is not None and spec.loader is not None
semantic_cache = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = semantic_cache
spec.loader.exec_module(semantic_cache)


def _write(path: Path, content: str) -> None:
	path.write_text(content, encoding="utf-8")


def _set_env(overrides: dict[str, str]) -> dict[str, str | None]:
	previous: dict[str, str | None] = {}
	for key, value in overrides.items():
		previous[key] = os.environ.get(key)
		os.environ[key] = value
	return previous


def _restore_env(previous: dict[str, str | None]) -> None:
	for key, value in previous.items():
		if value is None:
			os.environ.pop(key, None)
		else:
			os.environ[key] = value


def test_lookup_passthrough_backend_none() -> None:
	previous = _set_env({"SEMANTIC_CACHE_BACKEND": "none"})
	try:
		with tempfile.TemporaryDirectory() as tmpdir:
			tmp = Path(tmpdir)
			issue = tmp / "issue.txt"
			thread = tmp / "thread.txt"
			out = tmp / "out.txt"
			_write(issue, "issue")
			_write(thread, "thread")

			args = type(
				"Args",
				(),
				{
					"phase": "clarify",
					"issue_number": "1",
					"issue_body_file": str(issue),
					"thread_history_file": str(thread),
					"output_file": str(out),
				},
			)()
			result = semantic_cache.run_lookup(args)
			assert result["ok"] is True
			assert result["hit"] is False
			assert result["backend"] == "none"
	finally:
		_restore_env(previous)


def test_sqlite_store_and_lookup_hit() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp = Path(tmpdir)
		sqlite_path = tmp / "semantic.sqlite3"
		issue = tmp / "issue.txt"
		thread = tmp / "thread.txt"
		resp = tmp / "response.txt"
		out = tmp / "cached.txt"

		_write(issue, "Issue body A")
		_write(thread, "[2026-04-14] @alice: hello")
		_write(resp, "STATUS: CLEAR\nQUESTIONS:\n- None\n")

		previous = _set_env(
			{
				"SEMANTIC_CACHE_BACKEND": "sqlite-vec",
				"SEMANTIC_CACHE_SQLITE_PATH": str(sqlite_path),
				"SEMANTIC_CACHE_SIMILARITY_THRESHOLD": "0.9",
				"SEMANTIC_CACHE_TTL_DAYS": "14",
				"OPENROUTER_API_KEY": "test-key",
			}
		)
		orig_embed = semantic_cache._create_embedding
		semantic_cache._create_embedding = lambda text, cfg: [1.0, 0.0, 0.0]
		try:
			store_args = type(
				"Args",
				(),
				{
					"phase": "clarify",
					"issue_number": "42",
					"issue_body_file": str(issue),
					"thread_history_file": str(thread),
					"response_file": str(resp),
				},
			)()
			store_result = semantic_cache.run_store(store_args)
			assert store_result["ok"] is True
			assert store_result["stored"] is True

			lookup_args = type(
				"Args",
				(),
				{
					"phase": "clarify",
					"issue_number": "99",
					"issue_body_file": str(issue),
					"thread_history_file": str(thread),
					"output_file": str(out),
				},
			)()
			lookup_result = semantic_cache.run_lookup(lookup_args)
			assert lookup_result["ok"] is True
			assert lookup_result["hit"] is True
			assert lookup_result["original_issue_id"] == "42"
			assert out.read_text(encoding="utf-8") == "STATUS: CLEAR\nQUESTIONS:\n- None\n"
		finally:
			semantic_cache._create_embedding = orig_embed
			_restore_env(previous)


def test_sqlite_lookup_miss_on_high_threshold() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp = Path(tmpdir)
		sqlite_path = tmp / "semantic.sqlite3"
		issue = tmp / "issue.txt"
		thread = tmp / "thread.txt"
		resp = tmp / "response.txt"
		out = tmp / "cached.txt"

		_write(issue, "Issue body B")
		_write(thread, "comment history")
		_write(resp, "cached result")

		previous = _set_env(
			{
				"SEMANTIC_CACHE_BACKEND": "sqlite-vec",
				"SEMANTIC_CACHE_SQLITE_PATH": str(sqlite_path),
				"SEMANTIC_CACHE_SIMILARITY_THRESHOLD": "0.95",
				"SEMANTIC_CACHE_TTL_DAYS": "14",
				"OPENROUTER_API_KEY": "test-key",
			}
		)
		orig_embed = semantic_cache._create_embedding
		try:
			semantic_cache._create_embedding = lambda text, cfg: [1.0, 0.0]
			store_args = type(
				"Args",
				(),
				{
					"phase": "clarify",
					"issue_number": "10",
					"issue_body_file": str(issue),
					"thread_history_file": str(thread),
					"response_file": str(resp),
				},
			)()
			store_result = semantic_cache.run_store(store_args)
			assert store_result["ok"] is True

			semantic_cache._create_embedding = lambda text, cfg: [0.1, 0.99]
			lookup_args = type(
				"Args",
				(),
				{
					"phase": "clarify",
					"issue_number": "11",
					"issue_body_file": str(issue),
					"thread_history_file": str(thread),
					"output_file": str(out),
				},
			)()
			lookup_result = semantic_cache.run_lookup(lookup_args)
			assert lookup_result["ok"] is True
			assert lookup_result["hit"] is False
		finally:
			semantic_cache._create_embedding = orig_embed
			_restore_env(previous)


def test_store_fail_open_on_embedding_error() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp = Path(tmpdir)
		issue = tmp / "issue.txt"
		thread = tmp / "thread.txt"
		resp = tmp / "response.txt"
		_write(issue, "Issue body C")
		_write(thread, "Thread C")
		_write(resp, "Response C")

		previous = _set_env(
			{
				"SEMANTIC_CACHE_BACKEND": "sqlite-vec",
				"SEMANTIC_CACHE_SQLITE_PATH": str(tmp / "semantic.sqlite3"),
				"OPENROUTER_API_KEY": "test-key",
			}
		)
		orig_embed = semantic_cache._create_embedding
		semantic_cache._create_embedding = lambda text, cfg: (_ for _ in ()).throw(RuntimeError("embed down"))
		try:
			store_args = type(
				"Args",
				(),
				{
					"phase": "clarify",
					"issue_number": "12",
					"issue_body_file": str(issue),
					"thread_history_file": str(thread),
					"response_file": str(resp),
				},
			)()
			result = semantic_cache.run_store(store_args)
			assert result["ok"] is False
			assert result["stored"] is False
			assert "embed down" in result["error"]
		finally:
			semantic_cache._create_embedding = orig_embed
			_restore_env(previous)


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
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
