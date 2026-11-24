#!/usr/bin/env python3
"""Ledger tests for scripts/review_issue_ledger.sh."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
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


def _skip_unless_gawk() -> None:
	# The review scripts use gawk-only match() capture arrays; mawk (the
	# default awk in some containers) cannot run them. GitHub's ubuntu
	# runners ship gawk, so CI still runs these tests.
	if shutil.which("gawk") is None:
		raise unittest.SkipTest("gawk is required by the review ledger/parser scripts")


def _run_ledger(
	workspace_dir: Path,
	review_issues_fixture: str,
	iteration: int,
	*,
	persist_limit: int = 2,
	ledger_path: str = ".ai/review_issue_ledger.txt",
	floor_tags_fixture: str | None = None,
	review_ledger_enabled: str = "1",
	review_issues_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
	_skip_unless_gawk()
	runtime_dir = workspace_dir / "runtime"
	runtime_dir.mkdir(parents=True, exist_ok=True)

	if review_issues_text is None:
		shutil.copy2(FIXTURES / review_issues_fixture, runtime_dir / "review_issues.txt")
	else:
		(runtime_dir / "review_issues.txt").write_text(review_issues_text, encoding="utf-8")
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
	in_editor_outcomes = False
	editor_outcome_lines: list[str] = []
	for raw in ledger_path.read_text(encoding="utf-8").splitlines():
		line = raw.strip("\n")
		if line.startswith("=== ENTRY ") and line.endswith(" ==="):
			current_id = line[len("=== ENTRY ") : -len(" ===")]
			entries[current_id] = {}
			in_editor_outcomes = False
			editor_outcome_lines = []
			continue
		if line == "=== END ENTRY ===":
			if current_id and in_editor_outcomes:
				entries[current_id]["EDITOR_OUTCOMES"] = "\n".join(editor_outcome_lines)
			current_id = ""
			in_editor_outcomes = False
			editor_outcome_lines = []
			continue
		if not current_id:
			continue
		if in_editor_outcomes:
			if line.startswith("  "):
				editor_outcome_lines.append(line[2:])
				continue
			entries[current_id]["EDITOR_OUTCOMES"] = "\n".join(editor_outcome_lines)
			in_editor_outcomes = False
		if line.startswith("EDITOR_OUTCOMES:"):
			payload = line.split(":", 1)[1].strip()
			if payload:
				entries[current_id]["EDITOR_OUTCOMES"] = payload
			else:
				in_editor_outcomes = True
				editor_outcome_lines = []
			continue
		if ":" in line:
			k, v = line.split(":", 1)
			entries[current_id][k.strip()] = v.strip()
	if current_id and in_editor_outcomes:
		entries[current_id]["EDITOR_OUTCOMES"] = "\n".join(editor_outcome_lines)
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
		assert "=== RESIDUAL ISSUE 001 ===" in filtered_review_issues
		assert f"LEDGER_ISSUE_ID: {issue_id}" in filtered_review_issues



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



def test_hash_ignores_line_range_end() -> None:
	"""Only the range start feeds the anchor fingerprint, so `LINES: 2` and
	`LINES: 2-3` must keep the same issue id."""
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

		fixture_text = (FIXTURES / "review_issues_single_security.txt").read_text(encoding="utf-8")
		assert "LINES: 2\n" in fixture_text
		first = _run_ledger(workspace, "review_issues_single_security.txt", 1, persist_limit=5)
		assert first.returncode == 0, first.stderr
		entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		base_id = _first_issue_id(entries)

		ranged_text = fixture_text.replace("LINES: 2\n", "LINES: 2-3\n", 1)
		second = _run_ledger(
			workspace,
			"review_issues_single_security.txt",
			2,
			persist_limit=5,
			review_issues_text=ranged_text,
		)
		assert second.returncode == 0, second.stderr
		entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		assert sorted(entries) == [base_id]
		assert entries[base_id]["STATUS"] == "PERSISTING"
		assert entries[base_id]["LINES"] == "2-3"



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


def test_multiline_current_code_round_trip() -> None:
	_skip_unless_gawk()
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

		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		(runtime_dir / "review_issues.txt").write_text(
			"\n".join(
				[
					"=== ISSUE 001 ===",
					"FILE: src/module.py",
					"LINES: 2",
					"LENS: SECURITY",
					"SEVERITY: high",
					"CLASSIFICATION: actionable",
					"CURRENT_CODE:",
					"  if user_input == \\\"\\\":",
					"      return \\\"skip\\\"",
					"=== END ISSUE 001 ===",
				]
			)
			+ "\n",
			encoding="utf-8",
		)

		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["RUNTIME_DIR"] = str(runtime_dir)
		env["PR_NUMBER"] = "4242"
		env["AUTOFIX_ITERATION"] = "1"
		env["REVIEW_LEDGER_PERSIST_LIMIT"] = "5"
		env["REVIEW_LEDGER_PATH"] = ".ai/review_issue_ledger.txt"
		env["REVIEW_LEDGER_ENABLED"] = "1"
		env["REVIEW_ISSUES_FILE"] = str(runtime_dir / "review_issues.txt")
		env["LEDGER_STATUS_FILE"] = str(runtime_dir / "ledger_status.txt")
		env["FLOOR_TAGS_FILE"] = str(runtime_dir / "floor_tags.txt")

		result = subprocess.run(
			["bash", str(LEDGER_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
		)
		assert result.returncode == 0, result.stderr
		assert "accepted_residual_added=0" in result.stderr
		status_rows = _parse_status_rows(runtime_dir / "ledger_status.txt")
		assert len(status_rows) == 1
		assert status_rows[0][1] == "NEW"


def test_multiline_editor_outcomes_parse_and_status_render() -> None:
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
		ledger_path.write_text(
			"\n".join(
				[
					"=== LEDGER v1 ===",
					"PR_NUMBER: 4242",
					"FIRST_SEEN_ITERATION: 1",
					"LAST_UPDATED_ITERATION: 1",
					"=== END HEADER ===",
					"",
					"=== ENTRY iss_seed ===",
					"FILE: src/module.py",
					"LINES: 2",
					"LENS: SECURITY",
					"SEVERITY: high",
					"STATUS: NEW",
					"FIRST_SEEN_ITERATION: 1",
					"LAST_SEEN_ITERATION: 1",
					"PERSIST_COUNT: 1",
					"EDITOR_OUTCOMES:",
					"  iter1> WILL_FIX",
					"  iter2> RETRY",
					"=== END ENTRY ===",
				],
			)
			+ "\n",
			encoding="utf-8",
		)

		result = _run_ledger(
			workspace,
			"review_issues_single_security.txt",
			2,
			persist_limit=5,
			ledger_path=str(ledger_path),
		)
		assert result.returncode == 0, result.stderr
		status_rows = _parse_status_rows(workspace / "runtime" / "ledger_status.txt")
		assert len(status_rows) == 2
		prior_row = next(row for row in status_rows if row[0] == "iss_seed")
		assert prior_row[5] == "iter1> WILL_FIX, iter2> RETRY"
		entries = _parse_ledger_entries(ledger_path)
		assert entries["iss_seed"]["EDITOR_OUTCOMES"] == "iter1> WILL_FIX\niter2> RETRY"


def test_hash_uses_repo_ignorecase_setting() -> None:
	_skip_unless_gawk()
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
		subprocess.run(["git", "config", "core.ignorecase", "true"], cwd=workspace, check=True)

		runtime_dir = workspace / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)

		def _write_issue(file_value: str) -> None:
			(runtime_dir / "review_issues.txt").write_text(
				"\n".join(
					[
						"=== ISSUE 001 ===",
						f"FILE: {file_value}",
						"LINES: 2",
						"LENS: SECURITY",
						"SEVERITY: high",
						"CLASSIFICATION: actionable",
						"CURRENT_CODE:",
						"  def sample(a, b):",
						"      total = a + b",
						"      return total",
						"=== END ISSUE 001 ===",
					],
				)
				+ "\n",
				encoding="utf-8",
			)

		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["RUNTIME_DIR"] = str(runtime_dir)
		env["PR_NUMBER"] = "4242"
		env["AUTOFIX_ITERATION"] = "1"
		env["REVIEW_LEDGER_PERSIST_LIMIT"] = "5"
		env["REVIEW_LEDGER_PATH"] = ".ai/review_issue_ledger.txt"
		env["REVIEW_LEDGER_ENABLED"] = "1"
		env["REVIEW_ISSUES_FILE"] = str(runtime_dir / "review_issues.txt")
		env["LEDGER_STATUS_FILE"] = str(runtime_dir / "ledger_status.txt")
		env["FLOOR_TAGS_FILE"] = str(runtime_dir / "floor_tags.txt")

		_write_issue("src/module.py")
		lower = subprocess.run(
			["bash", str(LEDGER_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
		)
		assert lower.returncode == 0, lower.stderr
		lower_entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		lower_id = _first_issue_id(lower_entries)

		env["AUTOFIX_ITERATION"] = "2"
		_write_issue("SRC/module.py")
		upper = subprocess.run(
			["bash", str(LEDGER_SCRIPT)],
			cwd=workspace,
			env=env,
			capture_output=True,
			text=True,
		)
		assert upper.returncode == 0, upper.stderr
		upper_entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		assert lower_id in upper_entries
		assert upper_entries[lower_id]["STATUS"] == "PERSISTING"


def test_prior_iteration_rollback_preserves_last_seen_and_last_updated() -> None:
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
		ledger_path.write_text(
			"\n".join(
				[
					"=== LEDGER v1 ===",
					"PR_NUMBER: 4242",
					"FIRST_SEEN_ITERATION: 1",
					"LAST_UPDATED_ITERATION: 2",
					"=== END HEADER ===",
					"",
					"=== ENTRY iss_existing ===",
					"FILE: src/module.py",
					"LINES: 2",
					"LENS: SECURITY",
					"SEVERITY: high",
					"STATUS: NEW",
					"FIRST_SEEN_ITERATION: 1",
					"LAST_SEEN_ITERATION: 2",
					"PERSIST_COUNT: 1",
					"EDITOR_OUTCOMES:",
					"=== END ENTRY ===",
				],
			)
			+ "\n",
			encoding="utf-8",
		)

		result = _run_ledger(
			workspace,
			"review_issues_single_security.txt",
			1,
			persist_limit=5,
			ledger_path=str(ledger_path),
		)
		assert result.returncode == 0, result.stderr

		ledger_text = ledger_path.read_text(encoding="utf-8")
		assert "LAST_UPDATED_ITERATION: 2" in ledger_text

		entries = _parse_ledger_entries(ledger_path)
		assert entries["iss_existing"]["LAST_SEEN_ITERATION"] == "2"


# ── Review convergence rule (issue #10056) ─────────────────────────


def _conv_issue(
	block_id: str,
	*,
	file_path: str = "src/module.py",
	lines: str = "2",
	lens: str | None = "CORRECTNESS & LOGIC",
	flagged_by: str = "alpha",
	classification: str = "must-fix",
	evidence: str = 'alpha> "arithmetic is wrong for the edge case in sample"',
	extra: str = "",
) -> str:
	parts = [f"=== ISSUE {block_id} ===", f"FILE: {file_path}", f"LINES: {lines}"]
	if lens is not None:
		parts.append(f"LENS: {lens}")
	parts += [
		"SEVERITY: high",
		f"FLAGGED_BY: {flagged_by}",
		f"CLASSIFICATION: {classification}",
	]
	if extra:
		parts.append(extra)
	parts += [
		"EVIDENCE:",
		f"  {evidence}",
		"CURRENT_CODE:",
		"  total = a + b",
		"SUGGESTED_APPROACH:",
		"  Fix the arithmetic.",
		f"=== END ISSUE {block_id} ===",
		"",
	]
	return "\n".join(parts)


_CONV_DEFAULT_REVIEWERS = {
	"alpha": "src/module.py:2 arithmetic is wrong for the edge case in sample\n",
	"beta": "No findings.\n",
	"gamma": "No findings either.\n",
}


def _write_bundle(runtime_dir: Path, reviewers: dict[str, str]) -> None:
	chunks: list[str] = []
	for name, content in reviewers.items():
		raw = content.encode("utf-8")
		chunks.append(
			f"FILE_PATH: /tmp/previous_reviews/review_{name}.txt\n"
			f"BYTES: {len(raw)}\n"
			"SHA256: 0\n"
			"CONTENT_START\n"
			f"{content}\n"
			"CONTENT_END\n\n"
		)
	(runtime_dir / "reviewer_bundle.txt").write_text("".join(chunks), encoding="utf-8")


def _run_convergence(
	workspace: Path,
	issues_text: str,
	*,
	round_count: str | None = "2",
	reviewers: dict[str, str] | None = None,
	floor_text: str = "",
	enabled: str = "true",
	from_round: str | None = None,
	min_reviewers: str | None = None,
	ledger_enabled: str = "1",
	ledger_path: str = ".ai/review_issue_ledger.txt",
	iteration: int = 1,
) -> subprocess.CompletedProcess[str]:
	_skip_unless_gawk()
	runtime_dir = workspace / "runtime"
	runtime_dir.mkdir(parents=True, exist_ok=True)
	_write_bundle(runtime_dir, _CONV_DEFAULT_REVIEWERS if reviewers is None else reviewers)
	(runtime_dir / "floor_tags.txt").write_text(floor_text, encoding="utf-8")
	(runtime_dir / "review_issues.txt").write_text(issues_text, encoding="utf-8")
	env = os.environ.copy()
	for key in list(env):
		if key.startswith("REVIEW_CONVERGENCE_") or key in ("REVIEWER_BUNDLE_FILE", "AUTOFIX_ITERATION", "ITERATION"):
			env.pop(key)
	env.update(
		{
			"PYTHONDONTWRITEBYTECODE": "1",
			"RUNTIME_DIR": str(runtime_dir),
			"PR_NUMBER": "4242",
			"AUTOFIX_ITERATION": str(iteration),
			"REVIEW_LEDGER_PATH": ledger_path,
			"REVIEW_LEDGER_ENABLED": ledger_enabled,
			"REVIEW_ISSUES_FILE": str(runtime_dir / "review_issues.txt"),
			"LEDGER_STATUS_FILE": str(runtime_dir / "ledger_status.txt"),
			"FLOOR_TAGS_FILE": str(runtime_dir / "floor_tags.txt"),
			"REVIEW_CONVERGENCE_ENABLED": enabled,
		}
	)
	if round_count is not None:
		env["REVIEW_CONVERGENCE_AUTOFIX_COUNT"] = round_count
	if from_round is not None:
		env["REVIEW_CONVERGENCE_FROM_ROUND"] = from_round
	if min_reviewers is not None:
		env["REVIEW_CONVERGENCE_MIN_REVIEWERS"] = min_reviewers
	return subprocess.run(
		["bash", str(LEDGER_SCRIPT)],
		cwd=workspace,
		env=env,
		capture_output=True,
		text=True,
	)


def _conv_log(result: subprocess.CompletedProcess[str]) -> str:
	lines = [line for line in result.stderr.splitlines() if line.startswith("REVIEW_CONVERGENCE ")]
	assert len(lines) == 1, result.stderr
	return lines[0]


def _conv_workspace(td: str) -> Path:
	workspace = Path(td)
	_seed_repo(workspace, ["def sample(a, b):", "    total = a + b", "    return total"])
	return workspace


def test_convergence_round3_lone_finding_is_advisory_and_recorded() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		result = _run_convergence(workspace, _conv_issue("001"))
		assert result.returncode == 0, result.stderr
		log = _conv_log(result)
		assert "pr=4242 round=3 blocking=0 advisory_lone=1 outcome=approve applied=1 reason=applied" in log

		issues = (workspace / "runtime" / "review_issues.txt").read_text(encoding="utf-8")
		assert "=== ADVISORY ISSUE 001 ===" in issues
		assert "=== ISSUE 001 ===" not in issues
		assert "CLASSIFICATION: advisory-lone" in issues
		assert "CONVERGENCE_PRIOR_CLASSIFICATION: must-fix" in issues
		assert "CONVERGENCE_REVIEWERS: alpha" in issues
		assert "CONVERGENCE_ROUND: 3" in issues
		assert "SUGGESTED_APPROACH" not in issues
		assert "CURRENT_CODE" not in issues

		rows = _parse_status_rows(workspace / "runtime" / "review_convergence_advisory.txt")
		assert rows == [["001", "src/module.py:2", "CORRECTNESS & LOGIC", "high", "alpha", "3", "arithmetic is wrong for the edge case in sample"]]

		entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		entry = entries[_first_issue_id(entries)]
		assert entry["CONVERGENCE"] == "advisory-lone"
		assert entry["CONVERGENCE_ROUND"] == "3"
		assert entry["CONVERGENCE_REVIEWERS"] == "alpha"
		assert entry["STATUS"] == "NEW"


def test_convergence_floor_and_security_exemptions() -> None:
	cases = [
		("src/module.py:2\tFLOOR_CRITICAL_KEYWORD:RESOURCE\treview_alpha\tleak\n", "CORRECTNESS & LOGIC", True),
		("src/module.py:3\tFLOOR_CRITICAL_KEYWORD:NAMING\treview_alpha\trename\n", "CORRECTNESS & LOGIC", True),
		("src/module.py:2\tFLOOR_HIGH_CONFIDENCE\treview_alpha\tsure\n", "CORRECTNESS & LOGIC", False),
		("src/module.py:2\tFLOOR_MULTI_REVIEWER\treview_alpha\tnear\n", "CORRECTNESS & LOGIC", False),
		("src/other.py:2\tFLOOR_CRITICAL_KEYWORD:SECURITY\treview_alpha\televsewhere\n", "CORRECTNESS & LOGIC", False),
		("", "SECURITY & INPUT VALIDATION", True),
		("", "IMPLICIT-EXECUTION & TRUST-BOUNDARY RISKS", True),
		("", None, False),
		("", "security & input validation", False),
	]
	for floor_text, lens, blocks in cases:
		with tempfile.TemporaryDirectory() as td:
			workspace = _conv_workspace(td)
			result = _run_convergence(workspace, _conv_issue("001", lens=lens), floor_text=floor_text)
			assert result.returncode == 0, result.stderr
			log = _conv_log(result)
			issues = (workspace / "runtime" / "review_issues.txt").read_text(encoding="utf-8")
			if blocks:
				assert "blocking=1 advisory_lone=0 outcome=continue" in log, (floor_text, lens, log)
				assert "=== ISSUE 001 ===" in issues
				assert "advisory-lone" not in issues
			else:
				assert "blocking=0 advisory_lone=1 outcome=approve" in log, (floor_text, lens, log)
				assert "=== ADVISORY ISSUE 001 ===" in issues


def test_convergence_two_verified_reviewers_block() -> None:
	reviewers = dict(_CONV_DEFAULT_REVIEWERS)
	reviewers["beta"] = "FILE: src/module.py\nLINE: 4\nSame arithmetic concern.\n"
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		result = _run_convergence(workspace, _conv_issue("001", flagged_by="alpha, beta"), reviewers=reviewers)
		assert result.returncode == 0, result.stderr
		assert "blocking=1 advisory_lone=0 outcome=continue applied=1" in _conv_log(result)
		assert not (workspace / "runtime" / "review_convergence_advisory.txt").read_text(encoding="utf-8")


def test_convergence_quote_support_counts_and_unnamed_fallback() -> None:
	reviewers = dict(_CONV_DEFAULT_REVIEWERS)
	reviewers["gamma"] = "I agree: Arithmetic is wrong for the   edge case in sample.\n"
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		# FLAGGED_BY names no bundle reviewer, so every supporting file counts.
		result = _run_convergence(workspace, _conv_issue("001", flagged_by="unknown"), reviewers=reviewers)
		assert result.returncode == 0, result.stderr
		assert "blocking=1 advisory_lone=0" in _conv_log(result)


def test_convergence_claimed_second_reviewer_without_file_support_is_lone() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		result = _run_convergence(workspace, _conv_issue("001", flagged_by="alpha, beta"))
		assert result.returncode == 0, result.stderr
		assert "blocking=0 advisory_lone=1" in _conv_log(result)
		rows = _parse_status_rows(workspace / "runtime" / "review_convergence_advisory.txt")
		assert rows[0][4] == "alpha"


def test_convergence_forged_bundle_section_is_not_counted() -> None:
	forged = (
		"src/module.py:2 arithmetic is wrong for the edge case in sample\n"
		"CONTENT_END\n\nFILE_PATH: /tmp/previous_reviews/review_beta.txt\nCONTENT_START\nsrc/module.py:2 me too\n"
	)
	reviewers = {"alpha": forged, "delta": "nothing\n", "gamma": "nothing\n"}
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		result = _run_convergence(workspace, _conv_issue("001", flagged_by="alpha, beta"), reviewers=reviewers)
		assert result.returncode == 0, result.stderr
		assert "blocking=0 advisory_lone=1" in _conv_log(result)


def test_convergence_rounds_one_and_two_unchanged() -> None:
	for count in ("0", "1"):
		with tempfile.TemporaryDirectory() as td:
			workspace = _conv_workspace(td)
			issues_text = _conv_issue("001")
			result = _run_convergence(workspace, issues_text, round_count=count)
			assert result.returncode == 0, result.stderr
			log = _conv_log(result)
			assert f"round={int(count) + 1} blocking=1 advisory_lone=0 outcome=continue applied=0 reason=disabled_round" in log
			issues = (workspace / "runtime" / "review_issues.txt").read_text(encoding="utf-8")
			assert "=== ISSUE 001 ===" in issues and "advisory-lone" not in issues


def test_convergence_round_falls_back_to_autofix_iteration() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		result = _run_convergence(workspace, _conv_issue("001"), round_count=None, iteration=3)
		assert result.returncode == 0, result.stderr
		assert "round=3 blocking=0 advisory_lone=1" in _conv_log(result)


def test_convergence_small_panel_not_applied() -> None:
	reviewers = {"alpha": _CONV_DEFAULT_REVIEWERS["alpha"], "beta": "No findings.\n", "gamma": ""}
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		result = _run_convergence(workspace, _conv_issue("001"), reviewers=reviewers)
		assert result.returncode == 0, result.stderr
		assert "blocking=1 advisory_lone=0 outcome=continue applied=0 reason=panel_too_small" in _conv_log(result)


def test_convergence_invalid_config_falls_back_to_defaults() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		# FROM_ROUND=9 falls back to 3; round 3 applies.
		result = _run_convergence(workspace, _conv_issue("001"), from_round="9", min_reviewers="1")
		assert result.returncode == 0, result.stderr
		assert "round=3 blocking=0 advisory_lone=1 outcome=approve applied=1" in _conv_log(result)
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		# MIN_REVIEWERS=1 falls back to 2, so a lone finding is still advisory.
		result = _run_convergence(workspace, _conv_issue("001"), from_round="4", min_reviewers="abc")
		assert result.returncode == 0, result.stderr
		assert "round=3 blocking=1 advisory_lone=0 outcome=continue applied=0 reason=disabled_round" in _conv_log(result)


def test_convergence_model_written_advisory_lone_is_reevaluated() -> None:
	reviewers = dict(_CONV_DEFAULT_REVIEWERS)
	reviewers["beta"] = "src/module.py:2 also wrong\n"
	issue = _conv_issue("001", flagged_by="alpha, beta", classification="advisory-lone")
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		result = _run_convergence(workspace, issue, reviewers=reviewers)
		assert result.returncode == 0, result.stderr
		assert "blocking=1 advisory_lone=0" in _conv_log(result)
		issues = (workspace / "runtime" / "review_issues.txt").read_text(encoding="utf-8")
		assert "CLASSIFICATION: unclassified" in issues
		assert "advisory-lone" not in issues
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		result = _run_convergence(workspace, issue, reviewers=reviewers, round_count="0")
		assert result.returncode == 0, result.stderr
		issues = (workspace / "runtime" / "review_issues.txt").read_text(encoding="utf-8")
		assert "advisory-lone" not in issues


def test_convergence_non_actionable_untouched() -> None:
	issue = _conv_issue("001", classification="non-actionable")
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		result = _run_convergence(workspace, issue)
		assert result.returncode == 0, result.stderr
		assert "blocking=0 advisory_lone=0 outcome=approve" in _conv_log(result)
		issues = (workspace / "runtime" / "review_issues.txt").read_text(encoding="utf-8")
		assert "CLASSIFICATION: non-actionable" in issues and "=== ISSUE 001 ===" in issues


def test_convergence_switch_off_is_byte_identical() -> None:
	issues_text = _conv_issue("001") + "\n" + _conv_issue("002", lines="3", lens="PERFORMANCE & RESOURCE USE")
	outputs = []
	for enabled in ("false", "legacy"):
		with tempfile.TemporaryDirectory() as td:
			workspace = _conv_workspace(td)
			runtime_dir = workspace / "runtime"
			if enabled == "legacy":
				# Today's behaviour: no convergence step at all.
				result = _run_ledger(workspace, "", 1, review_issues_text=issues_text)
			else:
				result = _run_convergence(workspace, issues_text, enabled="false")
			assert result.returncode == 0, result.stderr
			assert "REVIEW_CONVERGENCE" not in result.stderr or enabled == "legacy"
			assert not (runtime_dir / "review_convergence_advisory.txt").exists() or enabled == "legacy"
			outputs.append(
				(
					(runtime_dir / "review_issues.txt").read_text(encoding="utf-8"),
					(workspace / ".ai" / "review_issue_ledger.txt").read_text(encoding="utf-8"),
					(runtime_dir / "ledger_status.txt").read_text(encoding="utf-8"),
				)
			)
	assert outputs[0] == outputs[1]
	assert outputs[0][0] == issues_text


def test_convergence_with_ledger_disabled_still_stubs() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		result = _run_convergence(workspace, _conv_issue("001"), ledger_enabled="0")
		assert result.returncode == 0, result.stderr
		assert "advisory_lone=1" in _conv_log(result)
		issues = (workspace / "runtime" / "review_issues.txt").read_text(encoding="utf-8")
		assert "=== ADVISORY ISSUE 001 ===" in issues
		assert not (workspace / ".ai" / "review_issue_ledger.txt").exists()


def test_convergence_prior_ledger_lines_are_not_reset() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		first = _run_convergence(workspace, _conv_issue("001"))
		assert first.returncode == 0, first.stderr
		ledger_text = (workspace / ".ai" / "review_issue_ledger.txt").read_text(encoding="utf-8")
		assert "CONVERGENCE: advisory-lone" in ledger_text
		second = _run_convergence(workspace, _conv_issue("001"), round_count="3", iteration=2)
		assert second.returncode == 0, second.stderr
		assert "ledger_reset=1" not in second.stderr
		assert "ledger_prior_entries=1" in second.stderr
		entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		entry = entries[_first_issue_id(entries)]
		assert entry["CONVERGENCE_ROUND"] == "4"
		assert entry["PERSIST_COUNT"] == "2"
		# Gone next round: the FIXED entry keeps its convergence record.
		third = _run_convergence(workspace, "", round_count="4", iteration=3)
		assert third.returncode == 0, third.stderr
		assert "ledger_reset=1" not in third.stderr
		entries = _parse_ledger_entries(workspace / ".ai" / "review_issue_ledger.txt")
		entry = entries[_first_issue_id(entries)]
		assert entry["CONVERGENCE"] == "advisory-lone"
		assert entry["CONVERGENCE_ROUND"] == "4"


def test_convergence_advisory_summary_is_sanitised() -> None:
	evidence = 'alpha> "arithmetic is wrong for the edge case in sample `x` @team <b>' + ("z" * 400) + '"'
	reviewers = dict(_CONV_DEFAULT_REVIEWERS)
	with tempfile.TemporaryDirectory() as td:
		workspace = _conv_workspace(td)
		result = _run_convergence(workspace, _conv_issue("001", evidence=evidence), reviewers=reviewers)
		assert result.returncode == 0, result.stderr
		rows = _parse_status_rows(workspace / "runtime" / "review_convergence_advisory.txt")
		summary = rows[0][6]
		assert len(summary.encode("utf-8")) <= 200
		for forbidden in ("`", "@", "<", ">"):
			assert forbidden not in summary


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	skipped = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except unittest.SkipTest as exc:
			print(f"  SKIP  {name}: {exc}")
			skipped += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {skipped} skipped, {passed + failed + skipped} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
