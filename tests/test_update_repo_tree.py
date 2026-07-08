#!/usr/bin/env python3
"""Tests for the repo-tree marker updater."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools" / "repo_tree"))

import update_repo_tree as repo_tree  # noqa: E402


def _write_text(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _build_repo(
	root: Path,
	*,
	agents_text: str,
	workflow_names: list[str] | None = None,
	template_names: list[str] | None = None,
) -> None:
	workflow_names = workflow_names or ["zeta.yml", "alpha.yml"]
	template_names = template_names or ["zz-template.yml", "aa-template.yml"]
	_write_text(
		root / "tools" / "repo_tree" / "config.yaml",
		"""trees:
  - file: agents.md
    marker_id: workflows
    source_glob: ".github/workflows/*.yml"
  - file: agents.md
    marker_id: workflow_templates
    source_glob: "workflow-templates/*.yml"
""",
	)
	_write_text(root / "agents.md", agents_text)
	for workflow_name in workflow_names:
		_write_text(root / ".github" / "workflows" / workflow_name, f"name: {workflow_name}\n")
	for template_name in template_names:
		_write_text(root / "workflow-templates" / template_name, f"name: {template_name}\n")


def _run_tool(root: Path, *args: str) -> tuple[int, str, str]:
	stdout_buffer = io.StringIO()
	stderr_buffer = io.StringIO()
	with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
		status = repo_tree.main(list(args), repo_root=root)
	return status, stdout_buffer.getvalue(), stderr_buffer.getvalue()


def test_write_updates_only_marker_interiors_and_sorts_globs() -> None:
	with tempfile.TemporaryDirectory() as td:
		repo_root = Path(td)
		_build_repo(
			repo_root,
			agents_text=(
				"Header\n\n"
				"<!-- TREE:START id=workflows -->\n"
				"old workflow content\n"
				"<!-- TREE:END id=workflows -->\n\n"
				"Between\n\n"
				"<!-- TREE:START id=workflow_templates -->\n"
				"old template content\n"
				"<!-- TREE:END id=workflow_templates -->\n"
				"Footer\n"
			),
		)

		status, stdout_text, stderr_text = _run_tool(repo_root, "--write")
		assert status == 0
		assert stdout_text == ""
		assert stderr_text == ""

		expected = (
			"Header\n\n"
			"<!-- TREE:START id=workflows -->\n"
			"```\n"
			".github/workflows/alpha.yml\n"
			".github/workflows/zeta.yml\n"
			"```\n"
			"<!-- TREE:END id=workflows -->\n\n"
			"Between\n\n"
			"<!-- TREE:START id=workflow_templates -->\n"
			"```\n"
			"workflow-templates/aa-template.yml\n"
			"workflow-templates/zz-template.yml\n"
			"```\n"
			"<!-- TREE:END id=workflow_templates -->\n"
			"Footer\n"
		)
		assert (repo_root / "agents.md").read_text(encoding="utf-8") == expected


def test_check_passes_after_write() -> None:
	with tempfile.TemporaryDirectory() as td:
		repo_root = Path(td)
		_build_repo(
			repo_root,
			agents_text=(
				"<!-- TREE:START id=workflows -->\n"
				"placeholder\n"
				"<!-- TREE:END id=workflows -->\n\n"
				"<!-- TREE:START id=workflow_templates -->\n"
				"placeholder\n"
				"<!-- TREE:END id=workflow_templates -->\n"
			),
		)

		write_status, _stdout_text, _stderr_text = _run_tool(repo_root, "--write")
		assert write_status == 0
		check_status, stdout_text, stderr_text = _run_tool(repo_root, "--check")
		assert check_status == 0
		assert stdout_text == ""
		assert stderr_text == ""


def test_check_emits_diff_on_drift() -> None:
	with tempfile.TemporaryDirectory() as td:
		repo_root = Path(td)
		_build_repo(
			repo_root,
			agents_text=(
				"<!-- TREE:START id=workflows -->\n"
				"stale\n"
				"<!-- TREE:END id=workflows -->\n\n"
				"<!-- TREE:START id=workflow_templates -->\n"
				"stale\n"
				"<!-- TREE:END id=workflow_templates -->\n"
			),
		)

		status, stdout_text, stderr_text = _run_tool(repo_root, "--check")
		assert status == 1
		assert stdout_text == ""
		assert "--- agents.md" in stderr_text
		assert "+++ agents.md (generated)" in stderr_text
		assert "::error::FAIL: agents.md is out of date; run make generate" in stderr_text


def test_write_excludes_directories_from_glob_matches() -> None:
	with tempfile.TemporaryDirectory() as td:
		repo_root = Path(td)
		_build_repo(
			repo_root,
			agents_text=(
				"<!-- TREE:START id=workflows -->\n"
				"placeholder\n"
				"<!-- TREE:END id=workflows -->\n\n"
				"<!-- TREE:START id=workflow_templates -->\n"
				"placeholder\n"
				"<!-- TREE:END id=workflow_templates -->\n"
			),
		)
		_write_text(
			repo_root / "tools" / "repo_tree" / "config.yaml",
			"""trees:
  - file: agents.md
    marker_id: workflows
    source_glob: ".github/workflows/*"
  - file: agents.md
    marker_id: workflow_templates
    source_glob: "workflow-templates/*.yml"
""",
		)
		(repo_root / ".github" / "workflows" / "nested-dir").mkdir(parents=True)

		status, stdout_text, stderr_text = _run_tool(repo_root, "--write")
		assert status == 0
		assert stdout_text == ""
		assert stderr_text == ""

		updated_text = (repo_root / "agents.md").read_text(encoding="utf-8")
		assert ".github/workflows/alpha.yml\n" in updated_text
		assert ".github/workflows/zeta.yml\n" in updated_text
		assert ".github/workflows/nested-dir\n" not in updated_text


def test_write_duplicate_marker_ids_fail_with_exit_code_two() -> None:
	with tempfile.TemporaryDirectory() as td:
		repo_root = Path(td)
		_build_repo(
			repo_root,
			agents_text=(
				"<!-- TREE:START id=workflows -->\n"
				"old\n"
				"<!-- TREE:END id=workflows -->\n\n"
				"<!-- TREE:START id=workflows -->\n"
				"old again\n"
				"<!-- TREE:END id=workflows -->\n\n"
				"<!-- TREE:START id=workflow_templates -->\n"
				"old\n"
				"<!-- TREE:END id=workflow_templates -->\n"
			),
		)

		before_text = (repo_root / "agents.md").read_text(encoding="utf-8")
		status, stdout_text, stderr_text = _run_tool(repo_root, "--write")
		assert status == 2
		assert stdout_text == ""
		assert "duplicate TREE:START marker id=workflows" in stderr_text
		assert (repo_root / "agents.md").read_text(encoding="utf-8") == before_text


def test_duplicate_marker_ids_fail_with_exit_code_two() -> None:
	with tempfile.TemporaryDirectory() as td:
		repo_root = Path(td)
		_build_repo(
			repo_root,
			agents_text=(
				"<!-- TREE:START id=workflows -->\n"
				"old\n"
				"<!-- TREE:END id=workflows -->\n\n"
				"<!-- TREE:START id=workflows -->\n"
				"old again\n"
				"<!-- TREE:END id=workflows -->\n\n"
				"<!-- TREE:START id=workflow_templates -->\n"
				"old\n"
				"<!-- TREE:END id=workflow_templates -->\n"
			),
		)

		status, _stdout_text, stderr_text = _run_tool(repo_root, "--check")
		assert status == 2
		assert "duplicate TREE:START marker id=workflows" in stderr_text


def test_missing_marker_pair_fails_with_exit_code_two() -> None:
	with tempfile.TemporaryDirectory() as td:
		repo_root = Path(td)
		_build_repo(
			repo_root,
			agents_text=(
				"<!-- TREE:START id=workflows -->\n"
				"old\n"
				"<!-- TREE:END id=workflows -->\n"
			),
		)

		status, _stdout_text, stderr_text = _run_tool(repo_root, "--check")
		assert status == 2
		assert "missing TREE marker pair(s): workflow_templates" in stderr_text


def test_check_rejects_invalid_marker_ids_in_config() -> None:
	with tempfile.TemporaryDirectory() as td:
		repo_root = Path(td)
		_build_repo(
			repo_root,
			agents_text=(
				"<!-- TREE:START id=workflows -->\n"
				"old\n"
				"<!-- TREE:END id=workflows -->\n\n"
				"<!-- TREE:START id=workflow_templates -->\n"
				"old\n"
				"<!-- TREE:END id=workflow_templates -->\n"
			),
		)
		_write_text(
			repo_root / "tools" / "repo_tree" / "config.yaml",
			"""trees:
  - file: agents.md
    marker_id: .invalid
    source_glob: ".github/workflows/*.yml"
""",
		)

		status, _stdout_text, stderr_text = _run_tool(repo_root, "--check")
		assert status == 1
		assert "marker_id contains invalid characters: .invalid" in stderr_text


def test_write_ignores_non_marker_tree_comment_lines() -> None:
	with tempfile.TemporaryDirectory() as td:
		repo_root = Path(td)
		_build_repo(
			repo_root,
			agents_text=(
				"<!-- TREE:START id=workflows -->\n"
				"placeholder\n"
				"<!-- TREE:END id=workflows -->\n\n"
				"<!-- TREE:START id=workflow_templates -->\n"
				"placeholder\n"
				"<!-- TREE:END id=workflow_templates -->\n\n"
				"<!-- TREE:START markers are described below -->\n"
			),
		)

		status, stdout_text, stderr_text = _run_tool(repo_root, "--write")
		assert status == 0
		assert stdout_text == ""
		assert stderr_text == ""
		assert "<!-- TREE:START markers are described below -->\n" in (
			(repo_root / "agents.md").read_text(encoding="utf-8")
		)


def test_malformed_marker_lines_fail_with_exit_code_two() -> None:
	with tempfile.TemporaryDirectory() as td:
		repo_root = Path(td)
		_build_repo(
			repo_root,
			agents_text=(
				"<!-- TREE:START id=workflows -->\n"
				"old\n"
				"<!-- TREE:END id=workflows -->\n\n"
				"<!-- TREE:START id=.invalid -->\n\n"
				"<!-- TREE:START id=workflow_templates -->\n"
				"old\n"
				"<!-- TREE:END id=workflow_templates -->\n"
			),
		)

		status, _stdout_text, stderr_text = _run_tool(repo_root, "--check")
		assert status == 2
		assert "malformed TREE marker line 5" in stderr_text


def test_unmatched_start_or_end_fails_with_exit_code_two() -> None:
	with tempfile.TemporaryDirectory() as td:
		repo_root = Path(td)
		_build_repo(
			repo_root,
			agents_text=(
				"<!-- TREE:START id=workflows -->\n"
				"old\n"
				"<!-- TREE:START id=workflow_templates -->\n"
				"old\n"
				"<!-- TREE:END id=workflow_templates -->\n"
			),
		)

		status, _stdout_text, stderr_text = _run_tool(repo_root, "--check")
		assert status == 2
		assert "found before closing TREE:START id=workflows" in stderr_text
