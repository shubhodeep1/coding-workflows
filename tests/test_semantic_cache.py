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


def test_lookup_miss_when_embedding_model_changes() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp = Path(tmpdir)
		sqlite_path = tmp / "semantic.sqlite3"
		issue = tmp / "issue.txt"
		thread = tmp / "thread.txt"
		resp = tmp / "response.txt"
		out = tmp / "cached.txt"

		_write(issue, "Issue body D")
		_write(thread, "comment history D")
		_write(resp, "cached response D")

		previous = _set_env(
			{
				"SEMANTIC_CACHE_BACKEND": "sqlite-vec",
				"SEMANTIC_CACHE_SQLITE_PATH": str(sqlite_path),
				"SEMANTIC_CACHE_SIMILARITY_THRESHOLD": "0.9",
				"SEMANTIC_CACHE_TTL_DAYS": "14",
				"OPENROUTER_API_KEY": "test-key",
				"SEMANTIC_CACHE_EMBEDDING_MODEL": "model-A",
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
					"issue_number": "77",
					"issue_body_file": str(issue),
					"thread_history_file": str(thread),
					"response_file": str(resp),
				},
			)()
			store_result = semantic_cache.run_store(store_args)
			assert store_result["ok"] is True
			assert store_result["stored"] is True

			os.environ["SEMANTIC_CACHE_EMBEDDING_MODEL"] = "model-B"
			lookup_args = type(
				"Args",
				(),
				{
					"phase": "clarify",
					"issue_number": "78",
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


def test_truncated_input_skips_store_and_lookup() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp = Path(tmpdir)
		sqlite_path = tmp / "semantic.sqlite3"
		issue = tmp / "issue.txt"
		thread = tmp / "thread.txt"
		resp = tmp / "response.txt"
		out = tmp / "cached.txt"

		_write(issue, "I" * 200)
		_write(thread, "T" * 200)
		_write(resp, "cached response E")

		previous = _set_env(
			{
				"SEMANTIC_CACHE_BACKEND": "sqlite-vec",
				"SEMANTIC_CACHE_SQLITE_PATH": str(sqlite_path),
				"SEMANTIC_CACHE_SIMILARITY_THRESHOLD": "0.9",
				"SEMANTIC_CACHE_TTL_DAYS": "14",
				"OPENROUTER_API_KEY": "test-key",
				"SEMANTIC_CACHE_MAX_CANONICAL_CHARS": "50",
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
					"issue_number": "88",
					"issue_body_file": str(issue),
					"thread_history_file": str(thread),
					"response_file": str(resp),
				},
			)()
			store_result = semantic_cache.run_store(store_args)
			assert store_result["ok"] is True
			assert store_result["stored"] is False
			assert store_result["reason"] == "input_truncated"

			lookup_args = type(
				"Args",
				(),
				{
					"phase": "clarify",
					"issue_number": "89",
					"issue_body_file": str(issue),
					"thread_history_file": str(thread),
					"output_file": str(out),
				},
			)()
			lookup_result = semantic_cache.run_lookup(lookup_args)
			assert lookup_result["ok"] is True
			assert lookup_result["hit"] is False
			assert lookup_result["reason"] == "input_truncated"
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


# ---------------------------------------------------------------------------
# Helpers for the additional coverage tests below
# ---------------------------------------------------------------------------


def _make_cfg(**overrides: object) -> "semantic_cache.RuntimeConfig":
	defaults: dict[str, object] = {
		"backend": "sqlite-vec",
		"ttl_days": 14,
		"similarity_threshold": 0.9,
		"embedding_model": "test-model",
		"embedding_base_url": "https://example.test/v1",
		"redis_url": "",
		"sqlite_path": "/tmp/semantic_cache_test.sqlite3",
		"openrouter_api_key": "test-key",
		"redis_key_namespace": "",
	}
	defaults.update(overrides)
	return semantic_cache.RuntimeConfig(**defaults)  # type: ignore[arg-type]


class _FakeUrlopenResponse:
	def __init__(self, payload: bytes) -> None:
		self._payload = payload

	def __enter__(self) -> "_FakeUrlopenResponse":
		return self

	def __exit__(self, *args: object) -> None:
		return None

	def read(self) -> bytes:
		return self._payload


def _patch_urlopen(handler):
	"""Replace ``semantic_cache.urllib.request.urlopen`` with ``handler`` and
	return the original so the caller can restore it in a finally block."""
	original = semantic_cache.urllib.request.urlopen
	semantic_cache.urllib.request.urlopen = handler  # type: ignore[assignment]
	return original


def _restore_urlopen(original) -> None:
	semantic_cache.urllib.request.urlopen = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# _create_embedding: HTTP path (urllib mocked)
# ---------------------------------------------------------------------------


def test_create_embedding_returns_floats_and_sends_expected_request() -> None:
	captured: dict[str, object] = {}
	payload = json.dumps({"data": [{"embedding": [0.1, 0.2, 0.3]}]}).encode("utf-8")

	def fake_urlopen(request, timeout):  # noqa: ARG001 — match urlopen signature
		captured["url"] = request.full_url
		captured["method"] = request.get_method()
		captured["headers"] = {k.lower(): v for k, v in request.header_items()}
		captured["body"] = request.data
		return _FakeUrlopenResponse(payload)

	cfg = _make_cfg()
	original = _patch_urlopen(fake_urlopen)
	try:
		embedding = semantic_cache._create_embedding("hello", cfg)
	finally:
		_restore_urlopen(original)

	assert embedding == [0.1, 0.2, 0.3]
	assert captured["url"] == "https://example.test/v1/embeddings"
	assert captured["method"] == "POST"
	assert captured["headers"]["authorization"] == "Bearer test-key"
	assert captured["headers"]["content-type"] == "application/json"
	body_obj = json.loads(captured["body"])  # type: ignore[arg-type]
	assert body_obj == {"model": "test-model", "input": "hello"}


def test_create_embedding_missing_api_key_raises() -> None:
	cfg = _make_cfg(openrouter_api_key="")
	try:
		semantic_cache._create_embedding("hello", cfg)
	except RuntimeError as exc:
		assert "OPENROUTER_API_KEY" in str(exc)
	else:
		raise AssertionError("expected RuntimeError when API key missing")


def test_create_embedding_http_error_is_wrapped() -> None:
	import io
	import urllib.error

	def fake_urlopen(request, timeout):  # noqa: ARG001
		raise urllib.error.HTTPError(
			url=request.full_url,
			code=503,
			msg="Service Unavailable",
			hdrs=None,  # type: ignore[arg-type]
			fp=io.BytesIO(b"upstream overloaded"),
		)

	cfg = _make_cfg()
	original = _patch_urlopen(fake_urlopen)
	try:
		try:
			semantic_cache._create_embedding("hello", cfg)
		except RuntimeError as exc:
			message = str(exc)
			assert "HTTP 503" in message
			assert "upstream overloaded" in message
		else:
			raise AssertionError("expected RuntimeError on HTTPError")
	finally:
		_restore_urlopen(original)


def test_create_embedding_url_error_is_wrapped() -> None:
	import urllib.error

	def fake_urlopen(request, timeout):  # noqa: ARG001
		raise urllib.error.URLError("no route to host")

	cfg = _make_cfg()
	original = _patch_urlopen(fake_urlopen)
	try:
		try:
			semantic_cache._create_embedding("hello", cfg)
		except RuntimeError as exc:
			assert "connection error" in str(exc)
		else:
			raise AssertionError("expected RuntimeError on URLError")
	finally:
		_restore_urlopen(original)


def test_create_embedding_parse_error_when_response_missing_embedding() -> None:
	payload = json.dumps({"data": [{}]}).encode("utf-8")

	def fake_urlopen(request, timeout):  # noqa: ARG001
		return _FakeUrlopenResponse(payload)

	cfg = _make_cfg()
	original = _patch_urlopen(fake_urlopen)
	try:
		try:
			semantic_cache._create_embedding("hello", cfg)
		except RuntimeError as exc:
			assert "parse error" in str(exc)
		else:
			raise AssertionError("expected RuntimeError on parse failure")
	finally:
		_restore_urlopen(original)


# ---------------------------------------------------------------------------
# _resolve_backend / _load_runtime_config edge cases
# ---------------------------------------------------------------------------


def test_resolve_backend_raises_on_unsupported_value() -> None:
	cfg = _make_cfg(backend="unknown")
	try:
		semantic_cache._resolve_backend(cfg)
	except RuntimeError as exc:
		assert "Unsupported backend" in str(exc)
	else:
		raise AssertionError("expected RuntimeError for unsupported backend")


def test_load_runtime_config_invalid_backend_raises() -> None:
	previous = _set_env({"SEMANTIC_CACHE_BACKEND": "elasticsearch"})
	try:
		try:
			semantic_cache._load_runtime_config()
		except ValueError as exc:
			assert "Invalid SEMANTIC_CACHE_BACKEND" in str(exc)
		else:
			raise AssertionError("expected ValueError for unknown backend")
	finally:
		_restore_env(previous)


def test_load_runtime_config_clamps_invalid_ttl_and_threshold_and_namespace_sanitizes() -> None:
	previous = _set_env(
		{
			"SEMANTIC_CACHE_BACKEND": "sqlite-vec",
			"SEMANTIC_CACHE_TTL_DAYS": "-3",
			"SEMANTIC_CACHE_SIMILARITY_THRESHOLD": "5.0",
			"GITHUB_REPOSITORY": "Owner/Some.Repo",
			"OPENROUTER_API_KEY": "k",
		}
	)
	# Ensure no explicit namespace overrides the derived one.
	prev_ns = os.environ.pop("SEMANTIC_CACHE_REDIS_KEY_NAMESPACE", None)
	try:
		cfg = semantic_cache._load_runtime_config()
		assert cfg.ttl_days == semantic_cache.DEFAULT_TTL_DAYS
		assert cfg.similarity_threshold == semantic_cache.DEFAULT_SIMILARITY_THRESHOLD
		# Namespace must be lower-cased, illegal chars (".") replaced with "_",
		# and a stable 8-char repo hash suffix appended.
		assert cfg.redis_key_namespace.startswith("owner_some_repo_")
		assert len(cfg.redis_key_namespace.split("_")[-1]) == 8
	finally:
		if prev_ns is not None:
			os.environ["SEMANTIC_CACHE_REDIS_KEY_NAMESPACE"] = prev_ns
		_restore_env(previous)


# ---------------------------------------------------------------------------
# run_store / run_lookup early-return branches
# ---------------------------------------------------------------------------


def _make_store_args(tmp: Path, *, phase: str = "clarify", response_text: str = "x") -> object:
	issue = tmp / "issue.txt"
	thread = tmp / "thread.txt"
	resp = tmp / "response.txt"
	_write(issue, "issue")
	_write(thread, "thread")
	_write(resp, response_text)
	return type(
		"Args",
		(),
		{
			"phase": phase,
			"issue_number": "1",
			"issue_body_file": str(issue),
			"thread_history_file": str(thread),
			"response_file": str(resp),
		},
	)()


def _make_lookup_args(tmp: Path, *, phase: str = "clarify") -> object:
	issue = tmp / "issue.txt"
	thread = tmp / "thread.txt"
	out = tmp / "out.txt"
	_write(issue, "issue")
	_write(thread, "thread")
	return type(
		"Args",
		(),
		{
			"phase": phase,
			"issue_number": "1",
			"issue_body_file": str(issue),
			"thread_history_file": str(thread),
			"output_file": str(out),
		},
	)()


def test_run_store_passthrough_when_backend_none() -> None:
	previous = _set_env({"SEMANTIC_CACHE_BACKEND": "none"})
	try:
		with tempfile.TemporaryDirectory() as tmpdir:
			result = semantic_cache.run_store(_make_store_args(Path(tmpdir)))
			assert result == {
				"ok": True,
				"backend": "none",
				"phase": "clarify",
				"stored": False,
				"reason": "passthrough",
			}
	finally:
		_restore_env(previous)


def test_run_store_phase_not_allowed_short_circuits() -> None:
	previous = _set_env(
		{
			"SEMANTIC_CACHE_BACKEND": "sqlite-vec",
			"SEMANTIC_CACHE_SQLITE_PATH": "/tmp/sc_unused.sqlite3",
			"OPENROUTER_API_KEY": "k",
		}
	)
	try:
		with tempfile.TemporaryDirectory() as tmpdir:
			result = semantic_cache.run_store(
				_make_store_args(Path(tmpdir), phase="implement")
			)
			assert result["ok"] is True
			assert result["stored"] is False
			assert result["reason"] == "phase_not_allowed"
	finally:
		_restore_env(previous)


def test_run_store_skips_empty_response() -> None:
	previous = _set_env(
		{
			"SEMANTIC_CACHE_BACKEND": "sqlite-vec",
			"SEMANTIC_CACHE_SQLITE_PATH": "/tmp/sc_unused.sqlite3",
			"OPENROUTER_API_KEY": "k",
		}
	)
	try:
		with tempfile.TemporaryDirectory() as tmpdir:
			result = semantic_cache.run_store(
				_make_store_args(Path(tmpdir), response_text="   \n\t  ")
			)
			assert result["ok"] is True
			assert result["stored"] is False
			assert result["reason"] == "empty_response"
	finally:
		_restore_env(previous)


def test_run_lookup_phase_not_allowed_short_circuits() -> None:
	previous = _set_env(
		{
			"SEMANTIC_CACHE_BACKEND": "sqlite-vec",
			"SEMANTIC_CACHE_SQLITE_PATH": "/tmp/sc_unused.sqlite3",
			"OPENROUTER_API_KEY": "k",
		}
	)
	try:
		with tempfile.TemporaryDirectory() as tmpdir:
			result = semantic_cache.run_lookup(
				_make_lookup_args(Path(tmpdir), phase="implement")
			)
			assert result["ok"] is True
			assert result["hit"] is False
			assert result["reason"] == "phase_not_allowed"
	finally:
		_restore_env(previous)


def test_run_lookup_returns_error_on_unexpected_exception() -> None:
	# Force _read_text_file to raise so the broad except in run_lookup runs.
	previous = _set_env(
		{
			"SEMANTIC_CACHE_BACKEND": "sqlite-vec",
			"SEMANTIC_CACHE_SQLITE_PATH": "/tmp/sc_unused.sqlite3",
			"OPENROUTER_API_KEY": "k",
		}
	)
	orig_read = semantic_cache._read_text_file

	def boom(path: str) -> str:
		raise OSError("disk gone")

	semantic_cache._read_text_file = boom  # type: ignore[assignment]
	try:
		with tempfile.TemporaryDirectory() as tmpdir:
			result = semantic_cache.run_lookup(_make_lookup_args(Path(tmpdir)))
			assert result["ok"] is False
			assert result["hit"] is False
			assert "disk gone" in result["error"]
	finally:
		semantic_cache._read_text_file = orig_read  # type: ignore[assignment]
		_restore_env(previous)


# ---------------------------------------------------------------------------
# RedisBackend (with an in-memory fake `redis` module)
# ---------------------------------------------------------------------------


class _FakeRedisClient:
	"""Minimal stand-in for ``redis.Redis`` covering the methods the
	semantic_cache RedisBackend touches: ``scan_iter``, ``get``, ``set``,
	``delete``."""

	def __init__(self) -> None:
		self.store: dict[str, str] = {}

	def scan_iter(self, match: str):  # type: ignore[no-untyped-def]
		import fnmatch

		# fnmatch only handles glob-style; the production code uses ``*``
		# wildcards which are compatible.
		for key in list(self.store.keys()):
			if fnmatch.fnmatchcase(key, match):
				yield key

	def get(self, key: str):  # type: ignore[no-untyped-def]
		return self.store.get(key)

	def set(self, key: str, value: str, ex: int | None = None) -> None:  # noqa: ARG002
		self.store[key] = value

	def delete(self, key: str) -> None:
		self.store.pop(key, None)


_FAKE_REDIS_LAST_KWARGS: dict[str, object] = {}
_FAKE_REDIS_SHARED_CLIENT: _FakeRedisClient | None = None


def _install_fake_redis_module() -> None:
	"""Inject a fake top-level ``redis`` module into ``sys.modules`` so that
	semantic_cache's ``import redis`` succeeds without the real package."""
	import types

	module = types.ModuleType("redis")

	class _Redis:
		@staticmethod
		def from_url(url: str, **kwargs: object) -> _FakeRedisClient:  # noqa: ARG004
			global _FAKE_REDIS_SHARED_CLIENT
			_FAKE_REDIS_LAST_KWARGS.clear()
			_FAKE_REDIS_LAST_KWARGS.update(kwargs)
			if _FAKE_REDIS_SHARED_CLIENT is None:
				_FAKE_REDIS_SHARED_CLIENT = _FakeRedisClient()
			return _FAKE_REDIS_SHARED_CLIENT

	module.Redis = _Redis  # type: ignore[attr-defined]
	sys.modules["redis"] = module


def _uninstall_fake_redis_module() -> None:
	global _FAKE_REDIS_SHARED_CLIENT
	_FAKE_REDIS_SHARED_CLIENT = None
	_FAKE_REDIS_LAST_KWARGS.clear()
	sys.modules.pop("redis", None)


def test_redis_backend_init_requires_url() -> None:
	try:
		semantic_cache.RedisBackend("", namespace="ns")
	except RuntimeError as exc:
		assert "REDIS_URL" in str(exc)
	else:
		raise AssertionError("expected RuntimeError when redis_url is empty")


def test_redis_backend_init_raises_when_redis_module_missing() -> None:
	# Make sure no fake redis is installed so the import really fails.
	had_real = sys.modules.pop("redis", None)
	# Block future imports too by inserting a sentinel that raises ImportError.
	import builtins

	original_import = builtins.__import__

	def blocking_import(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
		if name == "redis":
			raise ImportError("simulated missing dependency")
		return original_import(name, *args, **kwargs)

	builtins.__import__ = blocking_import  # type: ignore[assignment]
	try:
		try:
			semantic_cache.RedisBackend("redis://localhost:6379/0")
		except RuntimeError as exc:
			assert "redis backend selected" in str(exc)
		else:
			raise AssertionError("expected RuntimeError when redis pkg missing")
	finally:
		builtins.__import__ = original_import  # type: ignore[assignment]
		if had_real is not None:
			sys.modules["redis"] = had_real


def test_redis_backend_full_roundtrip_uses_fake_client() -> None:
	_install_fake_redis_module()
	try:
		backend = semantic_cache.RedisBackend(
			"rediss://example.test:6380/0", namespace="ns"
		)
		entry = semantic_cache.CacheEntry(
			phase="clarify",
			original_issue_id="42",
			cached_at=semantic_cache._utc_now_iso(),
			expires_at_epoch=int(__import__("time").time()) + 3600,
			embedding_model="test-model",
			embedding=[1.0, 0.0, 0.0],
			response_text="cached body",
			similarity_threshold=0.9,
			source_fingerprint="fp",
		)
		backend.store(entry)
		# ``rediss://`` URLs must propagate ``ssl_cert_reqs="required"``
		# through to ``Redis.from_url``.
		assert _FAKE_REDIS_LAST_KWARGS.get("ssl_cert_reqs") == "required"
		assert _FAKE_REDIS_SHARED_CLIENT is not None
		assert any(
			k.startswith("ns:semantic_cache:clarify:")
			for k in _FAKE_REDIS_SHARED_CLIENT.store
		)

		hit_entry, hit_similarity = backend.lookup(
			"clarify", [1.0, 0.0, 0.0], 0.5, "test-model"
		)
		assert hit_entry is not None
		assert hit_entry.original_issue_id == "42"
		assert hit_similarity > 0.99

		# Mismatched embedding model is filtered out.
		miss_entry, _ = backend.lookup(
			"clarify", [1.0, 0.0, 0.0], 0.5, "other-model"
		)
		assert miss_entry is None
	finally:
		_uninstall_fake_redis_module()


def test_redis_backend_lookup_skips_expired_entries_and_returns_miss_below_threshold() -> None:
	_install_fake_redis_module()
	try:
		backend = semantic_cache.RedisBackend("redis://example.test:6379/0")
		import time as _time

		# Seed the fake client directly with an already-expired entry; the
		# backend should drop it on the next lookup.
		assert _FAKE_REDIS_SHARED_CLIENT is None
		# Force client creation by performing a no-op lookup first.
		entry, _ = backend.lookup("clarify", [1.0, 0.0], 0.9, "test-model")
		assert entry is None
		assert _FAKE_REDIS_SHARED_CLIENT is not None
		expired_payload = json.dumps(
			{
				"phase": "clarify",
				"original_issue_id": "1",
				"cached_at": "2026-01-01T00:00:00Z",
				"expires_at_epoch": int(_time.time()) - 60,
				"embedding_model": "test-model",
				"embedding": [1.0, 0.0],
				"response_text": "stale",
				"similarity_threshold": 0.9,
				"source_fingerprint": "fp",
			}
		)
		_FAKE_REDIS_SHARED_CLIENT.store["semantic_cache:clarify:expired"] = expired_payload

		# Also seed a fresh entry whose similarity is BELOW the high threshold
		# so the lookup returns a miss but exercises the cosine/best-similarity
		# branch.
		fresh_payload = json.dumps(
			{
				"phase": "clarify",
				"original_issue_id": "2",
				"cached_at": "2026-04-15T00:00:00Z",
				"expires_at_epoch": int(_time.time()) + 3600,
				"embedding_model": "test-model",
				"embedding": [0.0, 1.0],
				"response_text": "fresh",
				"similarity_threshold": 0.99,
				"source_fingerprint": "fp",
			}
		)
		_FAKE_REDIS_SHARED_CLIENT.store["semantic_cache:clarify:fresh"] = fresh_payload

		miss_entry, miss_similarity = backend.lookup(
			"clarify", [1.0, 0.0], 0.99, "test-model"
		)
		assert miss_entry is None
		assert miss_similarity < 0.99
		# Expired key should have been deleted by the backend.
		assert "semantic_cache:clarify:expired" not in _FAKE_REDIS_SHARED_CLIENT.store
	finally:
		_uninstall_fake_redis_module()


# ---------------------------------------------------------------------------
# CLI surface: _build_parser + main()
# ---------------------------------------------------------------------------


def test_main_lookup_passthrough_writes_json_to_stdout(capsys=None) -> None:  # noqa: ARG001
	previous = _set_env({"SEMANTIC_CACHE_BACKEND": "none"})
	try:
		with tempfile.TemporaryDirectory() as tmpdir:
			tmp = Path(tmpdir)
			issue = tmp / "issue.txt"
			thread = tmp / "thread.txt"
			out = tmp / "out.txt"
			_write(issue, "issue")
			_write(thread, "thread")

			import io

			stdout_buf = io.StringIO()
			orig_stdout = sys.stdout
			sys.stdout = stdout_buf
			try:
				rc = semantic_cache.main(
					[
						"lookup",
						"--phase",
						"clarify",
						"--issue-number",
						"7",
						"--issue-body-file",
						str(issue),
						"--thread-history-file",
						str(thread),
						"--output-file",
						str(out),
					]
				)
			finally:
				sys.stdout = orig_stdout

			assert rc == 0
			payload = json.loads(stdout_buf.getvalue())
			assert payload["ok"] is True
			assert payload["hit"] is False
			assert payload["backend"] == "none"
	finally:
		_restore_env(previous)


def test_main_store_passthrough_writes_json_to_stdout() -> None:
	previous = _set_env({"SEMANTIC_CACHE_BACKEND": "none"})
	try:
		with tempfile.TemporaryDirectory() as tmpdir:
			tmp = Path(tmpdir)
			issue = tmp / "issue.txt"
			thread = tmp / "thread.txt"
			resp = tmp / "resp.txt"
			_write(issue, "issue")
			_write(thread, "thread")
			_write(resp, "resp body")

			import io

			stdout_buf = io.StringIO()
			orig_stdout = sys.stdout
			sys.stdout = stdout_buf
			try:
				rc = semantic_cache.main(
					[
						"store",
						"--phase",
						"clarify",
						"--issue-number",
						"7",
						"--issue-body-file",
						str(issue),
						"--thread-history-file",
						str(thread),
						"--response-file",
						str(resp),
					]
				)
			finally:
				sys.stdout = orig_stdout

			assert rc == 0
			payload = json.loads(stdout_buf.getvalue())
			assert payload["ok"] is True
			assert payload["stored"] is False
			assert payload["backend"] == "none"
	finally:
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
