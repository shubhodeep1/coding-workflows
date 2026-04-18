#!/usr/bin/env python3
"""Ledger tests for scripts/review_issue_ledger.sh."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_SCRIPT = REPO_ROOT / "scripts" / "review_issue_ledger.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "review_pipeline"


def _seed_repo(workspace_dir: Path, module_lines: list[str]) -> None:
	workspace_dir.mkdir(parents=True, exist_ok=True)
	(workspace_dir / "src").mkdir(parents=True, exist_ok=True)
	(workspace_dir / "src" / "module.py").write_text("\n".join(module_lines) + "\n", encoding="utf-8")

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


def _run_ledger(
	workspace_dir: Path,
	review_issues_fixture: str,
	iteration: int,
	*,
	persist_limit: int = 2,
	ledger_path: str = ".ai/review_issue_ledger.txt",
	floor_tags_fixture: str | None = None,
	review_ledger_enabled: str = "1",
) -> subprocess.CompletedProcess[str]:
	runtime_dir = workspace_dir / "runtime"
	runtime_dir.mkdir(parents=True, exist_ok=True)

	shutil.copy2(FIXTURES / review_issues_fixture, runtime_dir / "review_issues.txt")
	if floor_tags_fixture is not None:
		shutil.copy2(FIXTURES / floor_tags_fixture, runtime_dir / "floor_tags.txt")

	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["PR_NUMBER"] = "4242"
	env["AUTOFIX_ITERATION"] = str(iteration)
	env["REVIEW_LEDGER_PERSIST_LIMIT"] = str(persist_limit)
	env["REVIEW_LEDGER_PATH"] = ledger_path
	env["REVIEW_LEDGER_ENABLED"] = review_ledger_enabled
	env["REVIEW_ISSUES_FILE"] = str(runtime_dir / "review_issues.txt")
	env["LEDGER_STATUS_FILE"] = str(runtime_dir / "ledger_status.txt")
	env["FLOOR_TAGS_FILE"] = str(runtime_dir / "floor_tags.txt")

	return subprocess.run(
		["bash", str(LEDGER_SCRIPT)],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)


def _parse_ledger_entries(ledger_path: Path) -> dict[str, dict[str, str]]:
	entries: dict[str, dict[str, str]] = {}
	if not ledger_path.exists():
		return entries
	current_id = ""
	for raw in ledger_path.read_text(encoding="utf-8").splitlines():
		line = raw.strip("\n")
		if line.startswith("=== ENTRY ") and line.endswith(" ==="):
			current_id = line[len("=== ENTRY ") : -len(" ===")]
			entries[current_id] = {}
			continue
		if line == "=== END ENTRY ===":
			current_id = ""
			continue
		if current_id and ":" in line:
			k, v = line.split(":", 1)
			entries[current_id][k.strip()] = v.strip()
	return entries


def _parse_status_rows(path: Path) -> list[list[str]]:
	if not path.exists():
		return []
	rows: list[list[str]] = []
	for raw in path.read_text(encoding="utf-8").splitlines():
		if not raw.strip():
			continue
		rows.append(raw.split("\t"))
	return rows


def _first_issue_id(entries: dict[str, dict[str, str]]) -> str:
	ids = sorted(entries)
	assert ids, "ledger has no entries"
	return ids[0]


def test_lifecycle_and_persist_limit_transition() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		_seed_repo(
			workspace,
			[
				"def sample(a, b):",
				"    total = a + b",
				"    return total",
			],
		)

		first = _run_ledger(workspace, "review_issues_single_security.txt", 1, persist_limit=2)
		assert first.returncode == 0, first.stderr
		ledger_entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		issue_id = _first_issue_id(ledger_entries)
		assert ledger_entries[issue_id]["STATUS"] == "NEW"
		assert ledger_entries[issue_id]["PERSIST_COUNT"] == "1"

		second = _run_ledger(workspace, "review_issues_single_security.txt", 2, persist_limit=2)
		assert second.returncode == 0, second.stderr
		ledger_entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		assert ledger_entries[issue_id]["STATUS"] == "accepted-residual"
		assert ledger_entries[issue_id]["PERSIST_COUNT"] == "2"
		status_rows = _parse_status_rows(workspace / "runtime" / "ledger_status.txt")
		assert status_rows[0][1] == "accepted-residual"
		assert status_rows[0][2] == "2"
		filtered_review_issues = (workspace / "runtime" / "review_issues.txt").read_text(encoding="utf-8")
		assert "=== ISSUE 001 ===" not in filtered_review_issues



def test_present_absent_fixed_then_resurgent() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		_seed_repo(
			workspace,
			[
				"def sample(a, b):",
				"    total = a + b",
				"    return total",
			],
		)

		first = _run_ledger(workspace, "review_issues_single_security.txt", 1, persist_limit=5)
		assert first.returncode == 0, first.stderr
		ledger_entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		issue_id = _first_issue_id(ledger_entries)

		second = _run_ledger(workspace, "review_issues_empty.txt", 2, persist_limit=5)
		assert second.returncode == 0, second.stderr
		ledger_entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		assert ledger_entries[issue_id]["STATUS"] == "FIXED"
		assert ledger_entries[issue_id]["LAST_SEEN_ITERATION"] == "2"

		third = _run_ledger(workspace, "review_issues_single_security.txt", 3, persist_limit=5)
		assert third.returncode == 0, third.stderr
		ledger_entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		assert ledger_entries[issue_id]["STATUS"] == "RESURGENT"
		assert ledger_entries[issue_id]["PERSIST_COUNT"] == "1"



def test_hash_stable_on_whitespace_comment_churn_and_diverges_on_lens() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		_seed_repo(
			workspace,
			[
				"def sample(a, b):",
				"    total = a + b",
				"    return total",
			],
		)

		base = _run_ledger(workspace, "review_issues_single_security.txt", 1, persist_limit=5)
		assert base.returncode == 0, base.stderr
		entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		base_id = _first_issue_id(entries)

		(workspace / "src" / "module.py").write_text(
			"\n".join(
				[
					"def sample(a, b):",
					"    total    =    a + b     # cosmetic comment",
					"    return total",
				]
			)
			+ "\n",
			encoding="utf-8",
		)
		stable = _run_ledger(workspace, "review_issues_single_security.txt", 2, persist_limit=5)
		assert stable.returncode == 0, stable.stderr
		entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		assert base_id in entries

		lens_change = _run_ledger(workspace, "review_issues_single_correctness.txt", 3, persist_limit=5)
		assert lens_change.returncode == 0, lens_change.stderr
		entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		ids = sorted(entries)
		assert len(ids) == 2
		assert base_id in ids
		other = [x for x in ids if x != base_id][0]
		assert entries[other]["LENS"] == "CORRECTNESS & LOGIC"
		assert entries[base_id]["STATUS"] == "FIXED"



def test_malformed_prior_ledger_resets_fail_open() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		_seed_repo(
			workspace,
			[
				"def sample(a, b):",
				"    total = a + b",
				"    return total",
			],
		)

		ledger_path = workspace / ".ai" / "review_issue_ledger.txt"
		ledger_path.parent.mkdir(parents=True, exist_ok=True)
		ledger_path.write_text("this is malformed\n", encoding="utf-8")

		result = _run_ledger(workspace, "review_issues_single_security.txt", 2, persist_limit=5)
		assert result.returncode == 0, result.stderr
		assert "ledger_reset=1" in result.stderr
		entries = _parse_ledger_entries(ledger_path)
		issue_id = _first_issue_id(entries)
		assert entries[issue_id]["STATUS"] == "NEW"
		assert entries[issue_id]["FIRST_SEEN_ITERATION"] == "2"



def test_hash_collision_suffix_and_log_signal() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		_seed_repo(
			workspace,
			[
				"def sample(a, b):",
				"    total = a + b",
				"    return total",
			],
		)

		result = _run_ledger(
			workspace,
			"review_issues_two_collision_same_hash.txt",
			1,
			persist_limit=5,
		)
		assert result.returncode == 0, result.stderr
		assert "hash_collision=1" in result.stderr
		entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		ids = sorted(entries)
		assert len(ids) == 2
		assert any(":" in issue_id for issue_id in ids)
		status_rows = _parse_status_rows(workspace / "runtime" / "ledger_status.txt")
		for row in status_rows:
			assert len(row) == 6



def test_ledger_status_schema_and_sorting() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		_seed_repo(
			workspace,
			[
				"def sample(a, b):",
				"    total = a + b",
				"    return total",
			],
		)
		result = _run_ledger(workspace, "review_issues_two_distinct.txt", 1, persist_limit=5)
		assert result.returncode == 0, result.stderr
		rows = _parse_status_rows(workspace / "runtime" / "ledger_status.txt")
		assert len(rows) == 2
		ids = [row[0] for row in rows]
		assert ids == sorted(ids)
		for row in rows:
			assert len(row) == 6
			assert row[1] in {"NEW", "PERSISTING", "FIXED", "RESURGENT", "accepted-residual"}
			assert row[2].isdigit()
			assert ":" in row[3]



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
