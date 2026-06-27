#!/usr/bin/env python3
"""Direct-run and pytest-compatible tests for the operator-facing AI-memory CLI."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_MODULE_PATH = REPO_ROOT / "scripts" / "ai_memory_lib.py"
CLI_MODULE_PATH = REPO_ROOT / "scripts" / "ai_memory.py"

if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

lib_spec = importlib.util.spec_from_file_location("ai_memory_lib", LIB_MODULE_PATH)
assert lib_spec is not None and lib_spec.loader is not None
ai_memory_lib = importlib.util.module_from_spec(lib_spec)
sys.modules[lib_spec.name] = ai_memory_lib
lib_spec.loader.exec_module(ai_memory_lib)

cli_spec = importlib.util.spec_from_file_location("ai_memory", CLI_MODULE_PATH)
assert cli_spec is not None and cli_spec.loader is not None
ai_memory = importlib.util.module_from_spec(cli_spec)
sys.modules[cli_spec.name] = ai_memory
cli_spec.loader.exec_module(ai_memory)


@contextlib.contextmanager
def _patched_module_attrs(module, **replacements):
	originals = {name: getattr(module, name) for name in replacements}
	try:
		for name, value in replacements.items():
			setattr(module, name, value)
		yield
	finally:
		for name, value in originals.items():
			setattr(module, name, value)


@contextlib.contextmanager
def _temporary_env(**updates: str | None):
	originals = {key: os.environ.get(key) for key in updates}
	try:
		for key, value in updates.items():
			if value is None:
				os.environ.pop(key, None)
			else:
				os.environ[key] = value
		yield
	finally:
		for key, value in originals.items():
			if value is None:
				os.environ.pop(key, None)
			else:
				os.environ[key] = value


def _run_ai_memory_cli(argv: list[str]) -> tuple[int, str, str]:
	stdout = io.StringIO()
	stderr = io.StringIO()
	with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
		exit_code = ai_memory.main(argv)
	return exit_code, stdout.getvalue(), stderr.getvalue()


def _extract_ai_memory_telemetry(stderr: str) -> list[dict]:
	prefix = "AI_MEMORY_TELEMETRY: "
	entries = []
	for line in stderr.splitlines():
		if line.startswith(prefix):
			entries.append(json.loads(line[len(prefix) :]))
	return entries


def _tree_digest(root: Path) -> str:
	hasher = hashlib.sha256()
	if not root.exists():
		return hasher.hexdigest()
	for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
		relative = path.relative_to(root).as_posix().encode("utf-8")
		hasher.update(relative)
		hasher.update(b"\0")
		hasher.update(path.read_bytes())
		hasher.update(b"\0")
	return hasher.hexdigest()


@contextlib.contextmanager
def _stub_ai_memory_cli_branch():
	with tempfile.TemporaryDirectory(prefix="ai-memory-cli-store-") as store_dir:
		store_root = Path(store_dir)
		memory_root = store_root / "ai-memory"
		ai_memory_lib.ensure_memory_layout(memory_root)
		ai_memory_lib._sync_memory_reference_files(REPO_ROOT / "ai-memory", memory_root)
		snapshot_roots: list[Path] = []

		def _fake_read_memory_root_from_branch(_repo_root, *, memory_branch, memory_root_relative):
			assert memory_branch == "ai-memory"
			snapshot_root = Path(tempfile.mkdtemp(prefix="ai-memory-cli-read-"))
			snapshot_roots.append(snapshot_root)
			shutil.copytree(store_root / memory_root_relative, snapshot_root / memory_root_relative, dirs_exist_ok=True)
			return snapshot_root

		def _fake_persist_memory_operation(_repo_root, *, memory_branch, memory_root_relative, push_retries, commit_message, operation):
			assert memory_branch == "ai-memory"
			assert memory_root_relative == "ai-memory"
			assert push_retries >= 1
			assert commit_message.startswith("ai-memory:")
			memory_tree_root = store_root / memory_root_relative
			before_digest = _tree_digest(memory_tree_root)
			result = operation(store_root) or {}
			after_digest = _tree_digest(memory_tree_root)
			did_change = before_digest != after_digest
			return {
				"did_commit": did_change,
				"did_push": did_change,
				"commit_sha": "deadbeefcafebabe" if did_change else None,
				"push_attempts": 1 if did_change else 0,
				"operation_result": result,
			}

		with _patched_module_attrs(
			ai_memory,
			read_memory_root_from_branch=_fake_read_memory_root_from_branch,
			persist_memory_operation=_fake_persist_memory_operation,
		):
			yield store_root

		for snapshot_root in snapshot_roots:
			shutil.rmtree(snapshot_root, ignore_errors=True)


def _write_candidate_record(
	memory_root: Path,
	*,
	issue_number: int,
	pr_number: int | None,
	summary: str,
	created_at: str,
	details: str | None = None,
	prune_marked_at: str | None = None,
) -> dict:
	record = ai_memory_lib.record_candidate(
		memory_root,
		category="task_summaries",
		summary=summary,
		details=details or f"Details for {summary}",
		confidence=0.8,
		workflow="implement",
		run_id=f"run-{issue_number}-{pr_number or 0}",
		run_attempt=1,
		actor="octocat",
		issue_number=issue_number,
		pr_number=pr_number,
		source_refs=[f"issue-{issue_number}"],
	)
	path = ai_memory_lib._record_path_for_candidate(memory_root, record)
	payload = json.loads(path.read_text(encoding="utf-8"))
	payload["timestamps"]["created_at"] = created_at
	if prune_marked_at is not None:
		payload["timestamps"]["prune_marked_at"] = prune_marked_at
	ai_memory_lib.validate_memory_record(payload, memory_root)
	path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	return payload


def test_review_lists_only_stale_unpruned_candidates() -> None:
	now_utc = datetime.now(timezone.utc).replace(microsecond=0)
	with _stub_ai_memory_cli_branch() as store_root:
		memory_root = store_root / "ai-memory"
		old_record = _write_candidate_record(
			memory_root,
			issue_number=42,
			pr_number=7,
			summary="Old candidate for review",
			created_at=(now_utc - timedelta(days=120)).isoformat().replace("+00:00", "Z"),
		)
		_write_candidate_record(
			memory_root,
			issue_number=42,
			pr_number=8,
			summary="Fresh candidate should stay hidden",
			created_at=(now_utc - timedelta(days=10)).isoformat().replace("+00:00", "Z"),
		)
		exit_code, stdout, stderr = _run_ai_memory_cli(
			[
				"review",
				"--memory-branch",
				"ai-memory",
				"--memory-root",
				"ai-memory",
			]
		)

	assert exit_code == 0
	payload = json.loads(stdout)
	assert payload["stale_count"] == 1
	assert [item["record_id"] for item in payload["records"]] == [old_record["record_id"]]
	assert old_record["record_id"] in payload["table"]
	telemetry = _extract_ai_memory_telemetry(stderr)
	assert telemetry[0]["op"] == "review"
	assert telemetry[0]["stale_count"] == 1


def test_prune_is_idempotent_and_blocks_promotion() -> None:
	with _stub_ai_memory_cli_branch() as store_root:
		memory_root = store_root / "ai-memory"
		record = _write_candidate_record(
			memory_root,
			issue_number=77,
			pr_number=None,
			summary="Prunable candidate",
			created_at=(datetime.now(timezone.utc) - timedelta(days=200)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
		)

		exit_code, stdout, stderr = _run_ai_memory_cli(
			[
				"prune",
				"--memory-branch",
				"ai-memory",
				"--memory-root",
				"ai-memory",
				"--record-id",
				record["record_id"],
				"--record-id",
				"missing-record",
			]
		)
		assert exit_code == 0
		payload = json.loads(stdout)
		assert payload["marked"] == [record["record_id"]]
		assert payload["already_marked"] == []
		assert payload["not_found"] == ["missing-record"]
		assert payload["did_commit"] is True
		telemetry = _extract_ai_memory_telemetry(stderr)
		assert telemetry[0]["op"] == "prune"
		assert telemetry[0]["marked"] == 1

		candidate_path = ai_memory_lib._record_path_for_candidate(memory_root, record)
		persisted = json.loads(candidate_path.read_text(encoding="utf-8"))
		assert persisted["timestamps"]["prune_marked_at"]

		exit_code, stdout, _stderr = _run_ai_memory_cli(
			[
				"prune",
				"--memory-branch",
				"ai-memory",
				"--memory-root",
				"ai-memory",
				"--record-id",
				record["record_id"],
			]
		)
		assert exit_code == 0
		payload = json.loads(stdout)
		assert payload["marked"] == []
		assert payload["already_marked"] == [record["record_id"]]
		assert payload["prune_marked_at"] is None
		assert payload["did_commit"] is False

		promotion = ai_memory_lib.promote_candidates(memory_root, issue_number=77, record_id=record["record_id"])
		assert promotion["promoted"] == []
		assert promotion["skipped"] == [record["record_id"]]


def test_compact_archives_prune_marked_candidates_by_prune_month() -> None:
	with _stub_ai_memory_cli_branch() as store_root:
		memory_root = store_root / "ai-memory"
		record = _write_candidate_record(
			memory_root,
			issue_number=88,
			pr_number=None,
			summary="Old pruned candidate",
			created_at=(datetime.now(timezone.utc) - timedelta(days=300)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
		)
		exit_code, stdout, _stderr = _run_ai_memory_cli(
			[
				"prune",
				"--memory-branch",
				"ai-memory",
				"--memory-root",
				"ai-memory",
				"--record-id",
				record["record_id"],
			]
		)
		assert exit_code == 0
		payload = json.loads(stdout)
		prune_month = payload["prune_marked_at"][:7]

		summary = ai_memory_lib.compact_memory(memory_root, month_yyyy_mm=prune_month, prune=True)
		candidate_path = ai_memory_lib._record_path_for_candidate(memory_root, record)
		archive_path = memory_root / "archive" / "monthly" / prune_month / candidate_path.relative_to(memory_root)
		assert summary["archived_candidates"] == 1
		assert summary["removed_candidates"] == 1
		assert not candidate_path.exists()
		assert archive_path.exists()


def test_search_falls_back_to_keyword_when_openrouter_is_unset() -> None:
	with _stub_ai_memory_cli_branch() as store_root, _temporary_env(OPENROUTER_API_KEY=None):
		memory_root = store_root / "ai-memory"
		match = _write_candidate_record(
			memory_root,
			issue_number=91,
			pr_number=None,
			summary="Retry guard for orchestrator deadlock",
			created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
		)
		_write_candidate_record(
			memory_root,
			issue_number=92,
			pr_number=None,
			summary="Completely unrelated memory",
			created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
		)
		exit_code, stdout, stderr = _run_ai_memory_cli(
			[
				"search",
				"--memory-branch",
				"ai-memory",
				"--memory-root",
				"ai-memory",
				"--query",
				"deadlock guard",
			]
		)

	assert exit_code == 0
	payload = json.loads(stdout)
	assert payload["method"] == "keyword"
	assert payload["results"][0]["record"]["record_id"] == match["record_id"]
	telemetry = _extract_ai_memory_telemetry(stderr)
	assert telemetry[0]["op"] == "search"
	assert telemetry[0]["method"] == "keyword"


def test_search_uses_embedding_ranking_when_openrouter_is_available() -> None:
	with _stub_ai_memory_cli_branch() as store_root, _temporary_env(OPENROUTER_API_KEY="test-openrouter-key"):
		memory_root = store_root / "ai-memory"
		target = _write_candidate_record(
			memory_root,
			issue_number=101,
			pr_number=None,
			summary="Target record for semantic search",
			created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
		)
		distant = _write_candidate_record(
			memory_root,
			issue_number=102,
			pr_number=None,
			summary="Distant record for semantic search",
			created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
		)

		def _fake_embeddings(texts: list[str], *, api_key: str, model: str, base_url: str) -> list[list[float]]:
			assert api_key == "test-openrouter-key"
			assert model
			assert base_url
			results = []
			for text in texts:
				lowered = text.lower()
				if "semantic query" in lowered:
					results.append([1.0, 0.0])
				elif target["record_id"] in lowered or "target record" in lowered:
					results.append([0.9, 0.1])
				elif distant["record_id"] in lowered or "distant record" in lowered:
					results.append([0.0, 1.0])
				else:
					results.append([0.1, 0.1])
			return results

		with _patched_module_attrs(ai_memory_lib, _create_memory_search_embeddings=_fake_embeddings):
			exit_code, stdout, stderr = _run_ai_memory_cli(
				[
					"search",
					"--memory-branch",
					"ai-memory",
					"--memory-root",
					"ai-memory",
					"--query",
					"semantic query",
				]
			)

	assert exit_code == 0
	payload = json.loads(stdout)
	assert payload["method"] == "embedding"
	assert payload["results"][0]["record"]["record_id"] == target["record_id"]
	assert payload["results"][1]["record"]["record_id"] == distant["record_id"]
	telemetry = _extract_ai_memory_telemetry(stderr)
	assert telemetry[0]["method"] == "embedding"


def test_search_chunks_embedding_batches_for_large_record_sets() -> None:
	with _stub_ai_memory_cli_branch() as store_root, _temporary_env(OPENROUTER_API_KEY="test-openrouter-key"):
		memory_root = store_root / "ai-memory"
		target = _write_candidate_record(
			memory_root,
			issue_number=151,
			pr_number=None,
			summary="Target record for chunked semantic search",
			created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
		)
		for issue_number in range(152, 185):
			_write_candidate_record(
				memory_root,
				issue_number=issue_number,
				pr_number=None,
				summary=f"Background record {issue_number}",
				created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
			)

		batch_sizes: list[int] = []

		def _fake_embeddings(texts: list[str], *, api_key: str, model: str, base_url: str) -> list[list[float]]:
			assert api_key == "test-openrouter-key"
			assert model
			assert base_url
			batch_sizes.append(len(texts))
			results = []
			for text in texts:
				lowered = text.lower()
				if "chunked semantic query" in lowered:
					results.append([1.0, 0.0])
				elif target["record_id"] in lowered or "target record for chunked semantic search" in lowered:
					results.append([0.9, 0.1])
				else:
					results.append([0.0, 1.0])
			return results

		with _patched_module_attrs(ai_memory_lib, _create_memory_search_embeddings=_fake_embeddings):
			exit_code, stdout, stderr = _run_ai_memory_cli(
				[
					"search",
					"--memory-branch",
					"ai-memory",
					"--memory-root",
					"ai-memory",
					"--query",
					"chunked semantic query",
					"--max",
					"5",
				]
			)

	assert exit_code == 0
	payload = json.loads(stdout)
	assert payload["method"] == "embedding"
	assert payload["results"][0]["record"]["record_id"] == target["record_id"]
	assert batch_sizes == [1, 32, 2]
	telemetry = _extract_ai_memory_telemetry(stderr)
	assert telemetry[0]["method"] == "embedding"


def test_export_filters_by_issue_pr_and_intersection() -> None:
	with _stub_ai_memory_cli_branch() as store_root:
		memory_root = store_root / "ai-memory"
		match_issue_pr = _write_candidate_record(
			memory_root,
			issue_number=201,
			pr_number=11,
			summary="Issue 201 PR 11",
			created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
		)
		match_issue_only = _write_candidate_record(
			memory_root,
			issue_number=201,
			pr_number=12,
			summary="Issue 201 PR 12",
			created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
		)
		match_pr_only = _write_candidate_record(
			memory_root,
			issue_number=202,
			pr_number=11,
			summary="Issue 202 PR 11",
			created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
		)

		exit_code, stdout, stderr = _run_ai_memory_cli(
			[
				"export",
				"--memory-branch",
				"ai-memory",
				"--memory-root",
				"ai-memory",
				"--issue",
				"201",
				"--format",
				"json",
			]
		)
		assert exit_code == 0
		payload = json.loads(stdout)
		assert {entry["record"]["record_id"] for entry in payload["records"]} == {
			match_issue_pr["record_id"],
			match_issue_only["record_id"],
		}

		exit_code, stdout, _stderr = _run_ai_memory_cli(
			[
				"export",
				"--memory-branch",
				"ai-memory",
				"--memory-root",
				"ai-memory",
				"--pr",
				"11",
				"--format",
				"json",
			]
		)
		assert exit_code == 0
		payload = json.loads(stdout)
		assert {entry["record"]["record_id"] for entry in payload["records"]} == {
			match_issue_pr["record_id"],
			match_pr_only["record_id"],
		}

		exit_code, stdout, _stderr = _run_ai_memory_cli(
			[
				"export",
				"--memory-branch",
				"ai-memory",
				"--memory-root",
				"ai-memory",
				"--issue",
				"201",
				"--pr",
				"11",
				"--format",
				"json",
			]
		)
		assert exit_code == 0
		payload = json.loads(stdout)
		assert [entry["record"]["record_id"] for entry in payload["records"]] == [match_issue_pr["record_id"]]
		telemetry = _extract_ai_memory_telemetry(stderr)
		assert telemetry[0]["op"] == "export"
		assert telemetry[0]["record_count"] == 2


def main() -> int:
	for name, value in sorted(globals().items()):
		if name.startswith("test_") and callable(value):
			value()
	print("OK: ai_memory review/prune/search/export CLI tests passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
