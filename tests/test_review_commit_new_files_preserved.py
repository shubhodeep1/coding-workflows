#!/usr/bin/env python3
"""Tests that the consumer-repo new-file cleanup keeps editor-created files.

Background (tele-funtoken-msg-scoring#4287, review runs 34196277121 and
34198075113): the reviewer consensus asked for a new
``db/contracts/settings.yml`` collection contract.  The editor created it
(``git status`` at editor exit showed ``?? db/contracts/settings.yml``),
but the consumer-repo branch of ``scripts/review_commit_changes.sh``
deleted every newly created untracked path before staging ("editor may
not create new files"), so the tree was clean at commit time, the run
fired ``EDITOR_CHANGES_LOST``, the re-dispatch reproduced the same
deletion, and once the retry budget was exhausted auto-merge was blocked.
The ``changelog.d/*.md`` carve-out (#3763) had fixed one instance of the
same failure narrowly.

The fix reconciles new files against ``PRE_EDITOR_UNTRACKED_FILE`` (the
list of paths that were already untracked before the editor ran, written
by the "Apply fixes with editor model" step for both repo kinds):

- a path that was untracked before the editor ran is a stray and is
  removed as before;
- a pipeline-owned artifact path (``.ai/``, ``pre_assembled_static.txt``,
  fetched support files, ...) is removed even when new;
- any other new path is editor output and is preserved for the untracked
  files ``git add`` pass;
- without the snapshot the legacy delete-all behaviour is kept.

Every removed path is recorded as ``<path>\\t<reason>`` in
``REVIEW_REMOVED_NEW_FILES_FILE`` so the changes-lost PR comment can name
what was dropped.

These tests extract the cleanup block from the script and run it in a
throwaway git repo, mirroring ``test_review_commit_changelog_fragment_preserved``.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "review_commit_changes.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
EDITOR_SCRIPT = REPO_ROOT / "scripts" / "review_apply_fixes.sh"


def _git_test_env(home: Path | str) -> dict[str, str]:
	return {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(home)}


def _cleanup_block() -> str:
	text = SCRIPT.read_text(encoding="utf-8")
	m = re.search(
		r'NEW_FILES_BEFORE_COMMIT_FILE="\$\(mktemp\)"\n.*?\nfi\nrm -f "\$\{NEW_FILES_BEFORE_COMMIT_FILE\}"\n',
		text,
		re.DOTALL,
	)
	assert m, "Failed to locate the new-file cleanup block in review_commit_changes.sh"
	return m.group(0)


def _seed_repo(tmp: Path) -> tuple[Path, dict[str, str]]:
	repo = tmp / "repo"
	repo.mkdir()
	test_env = _git_test_env(tmp)
	subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=test_env)
	# Editor-created review output (new since the snapshot).
	(repo / "db" / "contracts").mkdir(parents=True)
	(repo / "db" / "contracts" / "settings.yml").write_text("collection: settings\n", encoding="utf-8")
	(repo / "tests").mkdir()
	(repo / "tests" / "test_reseed.py").write_text("def test_ok():\n\tassert True\n", encoding="utf-8")
	(repo / "changelog.d").mkdir()
	(repo / "changelog.d" / "4287-example-fragment.md").write_text(
		"<!-- changelog: fixed -->\n- Example.\n", encoding="utf-8"
	)
	# Pre-existing stray (present before the editor ran).
	(repo / "stray_artifact.txt").write_text("leftover\n", encoding="utf-8")
	# Pipeline-owned artifacts that appear during the editor phase.
	(repo / "pre_assembled_static.txt").write_text("prompt\n", encoding="utf-8")
	(repo / ".ai" / "review_runtime" / "pr-4287" / "round-1").mkdir(parents=True)
	(repo / ".ai" / "review_runtime" / "pr-4287" / "round-1" / "marker.json").write_text("{}\n", encoding="utf-8")
	# Infrastructure carve-out that must survive regardless.
	(repo / "scripts").mkdir()
	(repo / "scripts" / "helper.sh").write_text("#!/bin/sh\n", encoding="utf-8")
	return repo, test_env


def _run_cleanup(
	repo: Path,
	test_env: dict[str, str],
	*,
	snapshot: list[str] | None,
	runtime_dir: Path,
) -> subprocess.CompletedProcess:
	env = {**test_env, "IS_WORKFLOW_SOURCE_REPO": "false", "RUNTIME_DIR": str(runtime_dir)}
	if snapshot is not None:
		snapshot_file = runtime_dir / "pre_editor_untracked.txt"
		snapshot_file.write_text("".join(f"{line}\n" for line in sorted(snapshot)), encoding="utf-8")
		env["PRE_EDITOR_UNTRACKED_FILE"] = str(snapshot_file)
	return subprocess.run(
		["bash", "-c", "set -euo pipefail\n" + _cleanup_block()],
		cwd=repo,
		capture_output=True,
		text=True,
		env=env,
	)


def test_consumer_cleanup_keeps_editor_created_files_and_removes_strays_and_artifacts() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp = Path(tmpdir)
		repo, test_env = _seed_repo(tmp)
		runtime_dir = tmp / "runtime"
		runtime_dir.mkdir()
		proc = _run_cleanup(repo, test_env, snapshot=["stray_artifact.txt"], runtime_dir=runtime_dir)
		assert proc.returncode == 0, (proc.stdout, proc.stderr)

		# Editor output survives.
		assert (repo / "db" / "contracts" / "settings.yml").exists(), proc.stdout
		assert (repo / "tests" / "test_reseed.py").exists(), proc.stdout
		assert (repo / "changelog.d" / "4287-example-fragment.md").exists(), proc.stdout
		assert (repo / "scripts" / "helper.sh").exists(), proc.stdout
		assert "Preserving editor-created file: db/contracts/settings.yml" in proc.stdout
		assert "Preserving editor-created file: tests/test_reseed.py" in proc.stdout

		# Strays and pipeline artifacts are gone.
		assert not (repo / "stray_artifact.txt").exists(), proc.stdout
		assert not (repo / "pre_assembled_static.txt").exists(), proc.stdout
		assert not (repo / ".ai" / "review_runtime" / "pr-4287" / "round-1" / "marker.json").exists(), proc.stdout
		assert "- stray_artifact.txt (untracked before the editor ran)" in proc.stdout
		assert "- pre_assembled_static.txt (pipeline artifact)" in proc.stdout

		# Removals are recorded with their reasons.
		removed = (runtime_dir / "review_removed_new_files.txt").read_text(encoding="utf-8")
		removed_rows = {line.split("\t")[0]: line.split("\t")[1] for line in removed.splitlines() if line}
		assert removed_rows["stray_artifact.txt"] == "untracked before the editor ran"
		assert removed_rows["pre_assembled_static.txt"] == "pipeline artifact"
		assert removed_rows[".ai/review_runtime/pr-4287/round-1/marker.json"] == "pipeline artifact"
		assert "db/contracts/settings.yml" not in removed_rows


def test_consumer_cleanup_without_snapshot_keeps_legacy_delete_all() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp = Path(tmpdir)
		repo, test_env = _seed_repo(tmp)
		runtime_dir = tmp / "runtime"
		runtime_dir.mkdir()
		proc = _run_cleanup(repo, test_env, snapshot=None, runtime_dir=runtime_dir)
		assert proc.returncode == 0, (proc.stdout, proc.stderr)
		assert "pre-editor untracked snapshot unavailable" in proc.stdout
		# Legacy policy: new files are not kept, carve-outs still apply.
		assert not (repo / "db" / "contracts" / "settings.yml").exists(), proc.stdout
		assert not (repo / "tests" / "test_reseed.py").exists(), proc.stdout
		assert not (repo / "stray_artifact.txt").exists(), proc.stdout
		assert (repo / "changelog.d" / "4287-example-fragment.md").exists(), proc.stdout
		assert (repo / "scripts" / "helper.sh").exists(), proc.stdout
		assert "- db/contracts/settings.yml (legacy policy (no pre-editor snapshot))" in proc.stdout
		removed = (runtime_dir / "review_removed_new_files.txt").read_text(encoding="utf-8")
		assert "db/contracts/settings.yml\tlegacy policy (no pre-editor snapshot)" in removed.splitlines()


def test_consumer_cleanup_exports_removed_list_path_to_github_env() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp = Path(tmpdir)
		repo, test_env = _seed_repo(tmp)
		runtime_dir = tmp / "runtime"
		runtime_dir.mkdir()
		github_env = tmp / "github_env"
		github_env.write_text("", encoding="utf-8")
		env = {
			**test_env,
			"IS_WORKFLOW_SOURCE_REPO": "false",
			"RUNTIME_DIR": str(runtime_dir),
			"GITHUB_ENV": str(github_env),
		}
		proc = subprocess.run(
			["bash", "-c", "set -euo pipefail\n" + _cleanup_block()],
			cwd=repo,
			capture_output=True,
			text=True,
			env=env,
		)
		assert proc.returncode == 0, (proc.stdout, proc.stderr)
		assert f"REVIEW_REMOVED_NEW_FILES_FILE={runtime_dir / 'review_removed_new_files.txt'}" in github_env.read_text(
			encoding="utf-8"
		).splitlines()


def test_workflow_captures_pre_editor_untracked_snapshot_for_both_repo_kinds() -> None:
	text = WORKFLOW.read_text(encoding="utf-8")
	m = re.search(
		r'PRE_EDITOR_UNTRACKED_FILE="\$\{RUNTIME_DIR\}/pre_editor_untracked\.txt"\n'
		r'\s+git ls-files --others --exclude-standard \| sort -u > "\$\{PRE_EDITOR_UNTRACKED_FILE\}" \|\| true\n'
		r'\s+echo "PRE_EDITOR_UNTRACKED_FILE=\$\{PRE_EDITOR_UNTRACKED_FILE\}" >> "\$GITHUB_ENV"\n',
		text,
	)
	assert m, "The editor step must write PRE_EDITOR_UNTRACKED_FILE and export it via GITHUB_ENV"
	# The snapshot must sit outside the source-repo-only block so consumer
	# repos get it too; the source-repo block starts right after it.
	source_repo_guard = text.index('if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" = "true" ]; then\n            PRE_EDITOR_STATE_FILE=')
	assert m.start() < source_repo_guard
	# The changes-lost comment names the dropped paths.
	assert 'if [ -s "${REVIEW_REMOVED_NEW_FILES_FILE:-}" ]; then' in text
	assert "The commit step removed these newly created paths before staging" in text


def test_editor_prompt_allows_convention_required_new_files() -> None:
	text = EDITOR_SCRIPT.read_text(encoding="utf-8")
	assert "(do not create new files)" not in text
	assert "Do not create new files unless absolutely required to fix a broken import or dependency." not in text
	assert "a repository convention documented in CLAUDE.md / AGENTS.md requires it" in text
	assert "db/contracts/" in text and "changelog.d/" in text
