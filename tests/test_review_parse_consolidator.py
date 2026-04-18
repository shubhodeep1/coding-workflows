#!/usr/bin/env python3
"""Parser tests for scripts/review_parse_consolidator.sh."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PARSER_SCRIPT = REPO_ROOT / "scripts" / "review_parse_consolidator.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "review_pipeline"


def _run_parser(
	workspace_dir: Path,
	consolidator_fixture: str,
	failopen: str = "1",
) -> tuple[subprocess.CompletedProcess[str], Path]:
	workspace_dir.mkdir(parents=True, exist_ok=True)
	runtime_dir = workspace_dir / "runtime"
	runtime_dir.mkdir(parents=True, exist_ok=True)
	(workspace_dir / "src").mkdir(parents=True, exist_ok=True)
	(workspace_dir / "src" / "module.py").write_text(
		"line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\n",
		encoding="utf-8",
	)

	subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace_dir, check=True)
	for key, value in (
		("user.email", "test@local"),
		("user.name", "test"),
		("commit.gpgsign", "false"),
	):
		subprocess.run(["git", "config", key, value], cwd=workspace_dir, check=True)
	subprocess.run(["git", "add", "src/module.py"], cwd=workspace_dir, check=True)
	subprocess.run(
		["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
		cwd=workspace_dir,
		check=True,
	)

	shutil.copy2(FIXTURES / consolidator_fixture, runtime_dir / "consolidator_raw.txt")
	shutil.copy2(FIXTURES / "reviewer_bundle.txt", runtime_dir / "reviewer_bundle.txt")

	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["REVIEW_PARSER_FAILOPEN"] = failopen

	result = subprocess.run(
		["bash", str(PARSER_SCRIPT)],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)
	return result, runtime_dir


def _load_kv_file(path: Path) -> dict[str, str]:
	pairs: dict[str, str] = {}
	for raw in path.read_text(encoding="utf-8").splitlines():
		if "=" not in raw:
			continue
		k, v = raw.split("=", 1)
		pairs[k] = v
	return pairs


def test_well_formed_block_parses_and_deterministic() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp_root = Path(td)
		first, runtime = _run_parser(tmp_root / "first", "consolidator_well_formed.txt")
		assert first.returncode == 0, first.stderr
		issues_first = (runtime / "review_issues.txt").read_text(encoding="utf-8")
		stats_first = (runtime / "parser_stats.txt").read_text(encoding="utf-8")
		stats_map = _load_kv_file(runtime / "parser_stats.txt")
		assert "=== ISSUE 001 ===" in issues_first
		assert "=== ISSUE PASSTHROUGH 001 ===" in issues_first
		assert stats_map["parsed_blocks"] == "1"
		assert stats_map["passthrough_blocks"] == "2"
		assert stats_map["anchors_total"] == "4"
		assert stats_map["anchors_covered"] == "2"
		assert stats_map["line_unverified"] == "0"
		assert stats_map["parse_failed"] == "0"

		second, runtime_second = _run_parser(tmp_root / "second", "consolidator_well_formed.txt")
		assert second.returncode == 0, second.stderr
		assert issues_first == (runtime_second / "review_issues.txt").read_text(encoding="utf-8")
		assert stats_first == (runtime_second / "parser_stats.txt").read_text(encoding="utf-8")


def test_malformed_missing_file_degrades_to_passthrough() -> None:
	with tempfile.TemporaryDirectory() as td:
		result, runtime = _run_parser(Path(td), "consolidator_malformed_missing_file.txt")
		assert result.returncode == 0
		stats = _load_kv_file(runtime / "parser_stats.txt")
		issues = (runtime / "review_issues.txt").read_text(encoding="utf-8")
		assert stats["parsed_blocks"] == "0"
		assert stats["dropped_invalid_file"] == "1"
		assert stats["passthrough_blocks"] == "4"
		assert "=== ISSUE PASSTHROUGH 004 ===" in issues


def test_path_traversal_block_is_dropped() -> None:
	with tempfile.TemporaryDirectory() as td:
		result, runtime = _run_parser(Path(td), "consolidator_path_traversal.txt")
		assert result.returncode == 0
		stats = _load_kv_file(runtime / "parser_stats.txt")
		issues = (runtime / "review_issues.txt").read_text(encoding="utf-8")
		assert stats["parsed_blocks"] == "0"
		assert stats["dropped_invalid_file"] == "1"
		assert "../secrets.txt" not in issues


def test_anchor_mismatch_tags_line_unverified_and_preserves_passthrough() -> None:
	with tempfile.TemporaryDirectory() as td:
		result, runtime = _run_parser(Path(td), "consolidator_anchor_mismatch.txt")
		assert result.returncode == 0
		stats = _load_kv_file(runtime / "parser_stats.txt")
		issues = (runtime / "review_issues.txt").read_text(encoding="utf-8")
		assert stats["parsed_blocks"] == "1"
		assert stats["line_unverified"] == "1"
		assert stats["anchors_covered"] == "0"
		assert stats["passthrough_blocks"] == "4"
		assert "PARSER_TAGS: LINE_UNVERIFIED" in issues
		assert "=== ISSUE PASSTHROUGH 004 ===" in issues


def test_fail_open_when_no_markers_emits_passthrough_anchors() -> None:
	with tempfile.TemporaryDirectory() as td:
		result, runtime = _run_parser(Path(td), "consolidator_garbled.txt", failopen="1")
		assert result.returncode == 0
		stats = _load_kv_file(runtime / "parser_stats.txt")
		issues = (runtime / "review_issues.txt").read_text(encoding="utf-8")
		assert stats["parse_failed"] == "1"
		assert stats["parse_error"] == "no_issue_markers"
		assert stats["parsed_blocks"] == "0"
		assert stats["passthrough_blocks"] == "4"
		assert stats["anchors_total"] == "4"
		assert stats["anchors_covered"] == "0"
		assert "=== ISSUE PASSTHROUGH 004 ===" in issues


def test_fail_closed_when_no_markers_exits_nonzero() -> None:
	with tempfile.TemporaryDirectory() as td:
		result, runtime = _run_parser(Path(td), "consolidator_garbled.txt", failopen="0")
		assert result.returncode == 3
		stats = _load_kv_file(runtime / "parser_stats.txt")
		issues = (runtime / "review_issues.txt").read_text(encoding="utf-8")
		assert issues == ""
		assert stats["parse_failed"] == "1"
		assert stats["parse_error"] == "no_issue_markers"


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
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
