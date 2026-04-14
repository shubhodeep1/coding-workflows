#!/usr/bin/env python3
"""Semantic cache helper for clarify-phase workflows.

Backends:
- none (passthrough)
- sqlite-vec (SQLite + cosine scan)
- redis (Redis key scan + cosine scan)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ALLOWED_BACKENDS = {"none", "redis", "sqlite-vec"}
DEFAULT_BACKEND = "none"
DEFAULT_TTL_DAYS = 14
DEFAULT_SIMILARITY_THRESHOLD = 0.92
DEFAULT_EMBEDDING_MODEL = "openai/text-embedding-3-small"
DEFAULT_EMBEDDING_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_SQLITE_PATH = "/tmp/semantic_cache.sqlite3"
DEFAULT_MAX_CANONICAL_CHARS = 50000


def _safe_int(value: str | None, default: int) -> int:
	try:
		return int((value or "").strip())
	except (TypeError, ValueError, AttributeError):
		return default


def _safe_float(value: str | None, default: float) -> float:
	try:
		return float((value or "").strip())
	except (TypeError, ValueError, AttributeError):
		return default


def _utc_now_iso() -> str:
	return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_text(text: str) -> str:
	return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_text_file(path: str) -> str:
	with open(path, "r", encoding="utf-8") as handle:
		return _normalize_text(handle.read())


def _build_canonical_input(issue_body: str, thread_history: str) -> tuple[str, bool]:
	combined = (
		"ISSUE_BODY\n"
		f"{issue_body}\n\n"
		"THREAD_HISTORY\n"
		f"{thread_history}\n"
	)
	limit = _safe_int(os.getenv("SEMANTIC_CACHE_MAX_CANONICAL_CHARS"), DEFAULT_MAX_CANONICAL_CHARS)
	if limit <= 0:
		limit = DEFAULT_MAX_CANONICAL_CHARS
	if len(combined) > limit:
		return combined[:limit], True
	return combined, False


def _cosine_similarity(a: list[float], b: list[float]) -> float:
	if len(a) != len(b) or not a:
		return -1.0
	dot = 0.0
	mag_a = 0.0
	mag_b = 0.0
	for av, bv in zip(a, b):
		dot += av * bv
		mag_a += av * av
		mag_b += bv * bv
	if mag_a <= 0.0 or mag_b <= 0.0:
		return -1.0
	return dot / (math.sqrt(mag_a) * math.sqrt(mag_b))


@dataclass
class CacheEntry:
	phase: str
	original_issue_id: str
	cached_at: str
	expires_at_epoch: int
	embedding_model: str
	embedding: list[float]
	response_text: str
	similarity_threshold: float
	source_fingerprint: str


@dataclass
class RuntimeConfig:
	backend: str
	ttl_days: int
	similarity_threshold: float
	embedding_model: str
	embedding_base_url: str
	redis_url: str
	sqlite_path: str
	openrouter_api_key: str


def _load_runtime_config() -> RuntimeConfig:
	backend = (os.getenv("SEMANTIC_CACHE_BACKEND") or DEFAULT_BACKEND).strip().lower()
	if backend not in ALLOWED_BACKENDS:
		raise ValueError(
			"Invalid SEMANTIC_CACHE_BACKEND value: "
			f"{backend!r}. Allowed values: none|redis|sqlite-vec"
		)

	ttl_days = _safe_int(os.getenv("SEMANTIC_CACHE_TTL_DAYS"), DEFAULT_TTL_DAYS)
	if ttl_days <= 0:
		ttl_days = DEFAULT_TTL_DAYS

	similarity_threshold = _safe_float(
		os.getenv("SEMANTIC_CACHE_SIMILARITY_THRESHOLD"), DEFAULT_SIMILARITY_THRESHOLD
	)
	if similarity_threshold <= 0.0 or similarity_threshold > 1.0:
		similarity_threshold = DEFAULT_SIMILARITY_THRESHOLD

	embedding_model = (
		os.getenv("SEMANTIC_CACHE_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
	).strip() or DEFAULT_EMBEDDING_MODEL
	embedding_base_url = (
		os.getenv("SEMANTIC_CACHE_EMBEDDING_BASE_URL") or DEFAULT_EMBEDDING_BASE_URL
	).strip() or DEFAULT_EMBEDDING_BASE_URL
	redis_url = (os.getenv("SEMANTIC_CACHE_REDIS_URL") or "").strip()
	sqlite_path = (os.getenv("SEMANTIC_CACHE_SQLITE_PATH") or DEFAULT_SQLITE_PATH).strip() or DEFAULT_SQLITE_PATH
	openrouter_api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()

	return RuntimeConfig(
		backend=backend,
		ttl_days=ttl_days,
		similarity_threshold=similarity_threshold,
		embedding_model=embedding_model,
		embedding_base_url=embedding_base_url.rstrip("/"),
		redis_url=redis_url,
		sqlite_path=sqlite_path,
		openrouter_api_key=openrouter_api_key,
	)


def _create_embedding(text: str, cfg: RuntimeConfig) -> list[float]:
	if not cfg.openrouter_api_key:
		raise RuntimeError("OPENROUTER_API_KEY is required for semantic cache embedding requests")

	payload = json.dumps({"model": cfg.embedding_model, "input": text}).encode("utf-8")
	request = urllib.request.Request(
		f"{cfg.embedding_base_url}/embeddings",
		data=payload,
		headers={
			"Authorization": f"Bearer {cfg.openrouter_api_key}",
			"Content-Type": "application/json",
		},
		method="POST",
	)

	try:
		with urllib.request.urlopen(request, timeout=30) as response:
			response_body = response.read().decode("utf-8")
	except urllib.error.HTTPError as exc:
		body = ""
		try:
			body = exc.read().decode("utf-8", errors="replace")
		except Exception:
			pass
		raise RuntimeError(f"Embeddings API HTTP {exc.code}: {body[:500]}") from exc
	except urllib.error.URLError as exc:
		raise RuntimeError(f"Embeddings API connection error: {exc}") from exc

	try:
		decoded = json.loads(response_body)
		embedding_raw = decoded.get("data", [])[0].get("embedding")
		if not isinstance(embedding_raw, list) or not embedding_raw:
			raise ValueError("missing embedding array")
		return [float(v) for v in embedding_raw]
	except Exception as exc:
		raise RuntimeError(f"Embeddings API parse error: {exc}") from exc


class SQLiteVecBackend:
	def __init__(self, path: str) -> None:
		self.path = path

	def _connect(self) -> sqlite3.Connection:
		path_obj = Path(self.path)
		path_obj.parent.mkdir(parents=True, exist_ok=True)
		conn = sqlite3.connect(path_obj)
		conn.row_factory = sqlite3.Row
		return conn

	def _ensure_schema(self, conn: sqlite3.Connection) -> None:
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS semantic_cache_entries (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				phase TEXT NOT NULL,
				original_issue_id TEXT NOT NULL,
				cached_at TEXT NOT NULL,
				expires_at INTEGER NOT NULL,
				embedding_model TEXT NOT NULL,
				embedding_json TEXT NOT NULL,
				response_text TEXT NOT NULL,
				similarity_threshold REAL NOT NULL,
				source_fingerprint TEXT NOT NULL
			)
			"""
		)
		columns = {row[1] for row in conn.execute("PRAGMA table_info(semantic_cache_entries)").fetchall()}
		if "embedding_model" not in columns:
			conn.execute(
				"ALTER TABLE semantic_cache_entries ADD COLUMN embedding_model TEXT NOT NULL DEFAULT ''"
			)
		conn.execute(
			"CREATE INDEX IF NOT EXISTS idx_semantic_cache_phase_expires ON semantic_cache_entries (phase, expires_at)"
		)

	def lookup(
		self,
		phase: str,
		query_embedding: list[float],
		threshold: float,
		query_embedding_model: str,
	) -> tuple[CacheEntry | None, float]:
		now_epoch = int(time.time())
		with self._connect() as conn:
			self._ensure_schema(conn)
			conn.execute("DELETE FROM semantic_cache_entries WHERE expires_at <= ?", (now_epoch,))
			rows = conn.execute(
				"""
				SELECT phase, original_issue_id, cached_at, expires_at, embedding_model, embedding_json,
				       response_text, similarity_threshold, source_fingerprint
				FROM semantic_cache_entries
				WHERE phase = ? AND expires_at > ? AND embedding_model = ?
				""",
				(phase, now_epoch, query_embedding_model),
			).fetchall()

		best_entry: CacheEntry | None = None
		best_similarity = -1.0
		for row in rows:
			try:
				embedding = [float(v) for v in json.loads(row["embedding_json"])]
			except Exception:
				continue
			similarity = _cosine_similarity(query_embedding, embedding)
			if similarity > best_similarity:
				best_similarity = similarity
				best_entry = CacheEntry(
					phase=row["phase"],
					original_issue_id=row["original_issue_id"],
					cached_at=row["cached_at"],
					expires_at_epoch=int(row["expires_at"]),
					embedding_model=row["embedding_model"],
					embedding=embedding,
					response_text=row["response_text"],
					similarity_threshold=float(row["similarity_threshold"]),
					source_fingerprint=row["source_fingerprint"],
				)

		if best_entry is None or best_similarity < threshold:
			return None, best_similarity
		return best_entry, best_similarity

	def store(self, entry: CacheEntry) -> None:
		with self._connect() as conn:
			self._ensure_schema(conn)
			conn.execute(
				"""
				INSERT INTO semantic_cache_entries (
					phase, original_issue_id, cached_at, expires_at, embedding_model,
					embedding_json, response_text, similarity_threshold, source_fingerprint
				) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
				""",
				(
					entry.phase,
					entry.original_issue_id,
					entry.cached_at,
					entry.expires_at_epoch,
					entry.embedding_model,
					json.dumps(entry.embedding, ensure_ascii=True, separators=(",", ":")),
					entry.response_text,
					entry.similarity_threshold,
					entry.source_fingerprint,
				),
			)


class RedisBackend:
	def __init__(self, redis_url: str) -> None:
		if not redis_url:
			raise RuntimeError("SEMANTIC_CACHE_REDIS_URL is required when backend is redis")
		self.redis_url = redis_url

	def _client(self) -> Any:
		try:
			import redis  # type: ignore
		except ImportError as exc:
			raise RuntimeError("redis backend selected but 'redis' package is not installed") from exc
		client_kwargs: dict[str, Any] = {
			"decode_responses": True,
			"socket_timeout": 3,
			"socket_connect_timeout": 3,
		}
		if self.redis_url.lower().startswith("rediss://"):
			client_kwargs["ssl_cert_reqs"] = "required"
		return redis.Redis.from_url(self.redis_url, **client_kwargs)

	def lookup(
		self,
		phase: str,
		query_embedding: list[float],
		threshold: float,
		query_embedding_model: str,
	) -> tuple[CacheEntry | None, float]:
		client = self._client()
		now_epoch = int(time.time())
		best_entry: CacheEntry | None = None
		best_similarity = -1.0
		for key in client.scan_iter(match=f"semantic_cache:{phase}:*"):
			raw = client.get(key)
			if not raw:
				continue
			try:
				payload = json.loads(raw)
				expires_at = int(payload.get("expires_at_epoch", 0))
				if expires_at <= now_epoch:
					client.delete(key)
					continue
				embedding_model = str(payload.get("embedding_model", ""))
				if embedding_model != query_embedding_model:
					continue
				embedding = [float(v) for v in payload.get("embedding", [])]
			except Exception:
				continue

			similarity = _cosine_similarity(query_embedding, embedding)
			if similarity > best_similarity:
				best_similarity = similarity
				best_entry = CacheEntry(
					phase=str(payload.get("phase", phase)),
					original_issue_id=str(payload.get("original_issue_id", "")),
					cached_at=str(payload.get("cached_at", "")),
					expires_at_epoch=expires_at,
					embedding_model=embedding_model,
					embedding=embedding,
					response_text=str(payload.get("response_text", "")),
					similarity_threshold=float(payload.get("similarity_threshold", threshold)),
					source_fingerprint=str(payload.get("source_fingerprint", "")),
				)

		if best_entry is None or best_similarity < threshold:
			return None, best_similarity
		return best_entry, best_similarity

	def store(self, entry: CacheEntry) -> None:
		client = self._client()
		ttl_seconds = max(1, entry.expires_at_epoch - int(time.time()))
		key = f"semantic_cache:{entry.phase}:{uuid.uuid4().hex}"
		payload = {
			"phase": entry.phase,
			"original_issue_id": entry.original_issue_id,
			"cached_at": entry.cached_at,
			"expires_at_epoch": entry.expires_at_epoch,
			"embedding_model": entry.embedding_model,
			"embedding": entry.embedding,
			"response_text": entry.response_text,
			"similarity_threshold": entry.similarity_threshold,
			"source_fingerprint": entry.source_fingerprint,
		}
		client.set(key, json.dumps(payload, ensure_ascii=True, separators=(",", ":")), ex=ttl_seconds)


def _resolve_backend(cfg: RuntimeConfig) -> Any:
	if cfg.backend == "sqlite-vec":
		return SQLiteVecBackend(cfg.sqlite_path)
	if cfg.backend == "redis":
		return RedisBackend(cfg.redis_url)
	raise RuntimeError(f"Unsupported backend: {cfg.backend}")


def _build_entry(
	phase: str,
	original_issue_id: str,
	embedding_model: str,
	embedding: list[float],
	response_text: str,
	similarity_threshold: float,
	source_fingerprint: str,
	ttl_days: int,
) -> CacheEntry:
	now_epoch = int(time.time())
	expires_at_epoch = now_epoch + (ttl_days * 24 * 60 * 60)
	return CacheEntry(
		phase=phase,
		original_issue_id=original_issue_id,
		cached_at=_utc_now_iso(),
		expires_at_epoch=expires_at_epoch,
		embedding_model=embedding_model,
		embedding=embedding,
		response_text=response_text,
		similarity_threshold=similarity_threshold,
		source_fingerprint=source_fingerprint,
	)


def run_lookup(args: argparse.Namespace) -> dict[str, Any]:
	try:
		cfg = _load_runtime_config()
		if cfg.backend == "none":
			return {
				"ok": True,
				"backend": cfg.backend,
				"phase": args.phase,
				"hit": False,
				"reason": "passthrough",
			}

		issue_body = _read_text_file(args.issue_body_file)
		thread_history = _read_text_file(args.thread_history_file)
		canonical_input, truncated = _build_canonical_input(issue_body, thread_history)
		source_fingerprint = hashlib.sha256(canonical_input.encode("utf-8")).hexdigest()
		if truncated:
			return {
				"ok": True,
				"backend": cfg.backend,
				"phase": args.phase,
				"hit": False,
				"reason": "input_truncated",
				"truncated": True,
				"source_fingerprint": source_fingerprint,
			}
		query_embedding = _create_embedding(canonical_input, cfg)
		backend = _resolve_backend(cfg)
		entry, similarity = backend.lookup(args.phase, query_embedding, cfg.similarity_threshold, cfg.embedding_model)
		if entry is None:
			return {
				"ok": True,
				"backend": cfg.backend,
				"phase": args.phase,
				"hit": False,
				"truncated": truncated,
				"source_fingerprint": source_fingerprint,
				"similarity": similarity,
			}

		Path(args.output_file).write_text(entry.response_text, encoding="utf-8")
		return {
			"ok": True,
			"backend": cfg.backend,
			"phase": args.phase,
			"hit": True,
			"similarity": similarity,
			"cached_at": entry.cached_at,
			"original_issue_id": entry.original_issue_id,
			"source_fingerprint": entry.source_fingerprint,
			"truncated": truncated,
		}
	except Exception as exc:
		return {
			"ok": False,
			"hit": False,
			"phase": getattr(args, "phase", "unknown"),
			"error": str(exc),
		}


def run_store(args: argparse.Namespace) -> dict[str, Any]:
	try:
		cfg = _load_runtime_config()
		if cfg.backend == "none":
			return {
				"ok": True,
				"backend": cfg.backend,
				"phase": args.phase,
				"stored": False,
				"reason": "passthrough",
			}

		response_text = _read_text_file(args.response_file)
		if not response_text.strip():
			return {
				"ok": True,
				"backend": cfg.backend,
				"phase": args.phase,
				"stored": False,
				"reason": "empty_response",
			}

		issue_body = _read_text_file(args.issue_body_file)
		thread_history = _read_text_file(args.thread_history_file)
		canonical_input, truncated = _build_canonical_input(issue_body, thread_history)
		source_fingerprint = hashlib.sha256(canonical_input.encode("utf-8")).hexdigest()
		if truncated:
			return {
				"ok": True,
				"backend": cfg.backend,
				"phase": args.phase,
				"stored": False,
				"reason": "input_truncated",
				"truncated": True,
				"source_fingerprint": source_fingerprint,
			}
		embedding = _create_embedding(canonical_input, cfg)
		backend = _resolve_backend(cfg)
		entry = _build_entry(
			phase=args.phase,
			original_issue_id=str(args.issue_number),
			embedding_model=cfg.embedding_model,
			embedding=embedding,
			response_text=response_text,
			similarity_threshold=cfg.similarity_threshold,
			source_fingerprint=source_fingerprint,
			ttl_days=cfg.ttl_days,
		)
		backend.store(entry)
		return {
			"ok": True,
			"backend": cfg.backend,
			"phase": args.phase,
			"stored": True,
			"cached_at": entry.cached_at,
			"expires_at_epoch": entry.expires_at_epoch,
			"original_issue_id": entry.original_issue_id,
			"source_fingerprint": entry.source_fingerprint,
			"truncated": truncated,
		}
	except Exception as exc:
		return {
			"ok": False,
			"stored": False,
			"phase": getattr(args, "phase", "unknown"),
			"error": str(exc),
		}


def _build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Semantic cache helper")
	subparsers = parser.add_subparsers(dest="command", required=True)

	lookup_parser = subparsers.add_parser("lookup", help="Lookup semantic cache entry")
	lookup_parser.add_argument("--phase", required=True)
	lookup_parser.add_argument("--issue-number", required=True)
	lookup_parser.add_argument("--issue-body-file", required=True)
	lookup_parser.add_argument("--thread-history-file", required=True)
	lookup_parser.add_argument("--output-file", required=True)

	store_parser = subparsers.add_parser("store", help="Store semantic cache entry")
	store_parser.add_argument("--phase", required=True)
	store_parser.add_argument("--issue-number", required=True)
	store_parser.add_argument("--issue-body-file", required=True)
	store_parser.add_argument("--thread-history-file", required=True)
	store_parser.add_argument("--response-file", required=True)

	return parser


def main(argv: list[str] | None = None) -> int:
	parser = _build_parser()
	args = parser.parse_args(argv)

	if args.command == "lookup":
		result = run_lookup(args)
	elif args.command == "store":
		result = run_store(args)
	else:
		result = {"ok": False, "error": f"unsupported command: {args.command}"}

	json.dump(result, sys.stdout, ensure_ascii=True, sort_keys=True)
	sys.stdout.write("\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
