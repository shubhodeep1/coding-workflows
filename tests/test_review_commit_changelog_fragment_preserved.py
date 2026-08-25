#!/usr/bin/env python3
"""Tests that the consumer-repo new-file cleanup preserves changelog fragments.

Background (tele-funtoken-msg-scoring#3763, review run 32732281452): the
reviewer consensus asked for the CLAUDE.md §20 changelog fragment the PR
was missing, and the editor created exactly one file —
``changelog.d/3763-uniswap-comp-admin-stats-aggregator.md``.  The write
persisted (``git status`` showed the untracked file), but the
consumer-repo branch of ``scripts/review_commit_changes.sh`` deletes
every newly created untracked path before staging ("editor may not
create new files"), so the fragment was removed, the tree was clean at
commit time, and the run fired a false ``EDITOR_CHANGES_LOST`` dead end
that blocked auto-merge.  Any review round whose entire fix is creating
a fragment reproduced this deterministically.

The fix exempts ``changelog.d/*.md`` from the cleanup, alongside the
existing ``.serena`` / ``scripts/`` / ``prompts/`` exemptions.  The
consumer staging pass (``git ls-files --others ... | xargs git add``)
then stages the surviving fragment, so the round commits normally.

These tests extract the cleanup block from the script and run it in a
throwaway git repo, mirroring the extraction style of the other
``review_commit_changes`` contract tests.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "review_commit_changes.sh"


def _script_text() -> str:
	return SCRIPT.read_text(encoding="utf-8")


def _cleanup_block(text: str) -> str:
	m = re.search(
		r'NEW_FILES_BEFORE_COMMIT_FILE="\$\(mktemp\)"\n.*?\nfi\nrm -f "\$\{NEW_FILES_BEFORE_COMMIT_FILE\}"\n',
		text,
		re.DOTALL,
	)
	assert m, "Failed to locate the new-file cleanup block in review_commit_changes.sh"
	return m.group(0)


def test_cleanup_block_exempts_changelog_fragments() -> None:
	block = _cleanup_block(_script_text())
	assert "changelog.d/*.md)" in block, (
		"The consumer-repo new-file cleanup must exempt changelog.d/*.md — "
		"deleting editor-created fragments makes every fragment-only review "
		"round a false EDITOR_CHANGES_LOST dead end"
	)
	# The exemption must sit inside the removal case statement, before the rm.
	assert block.index("changelog.d/*.md)") < block.index('rm -rf -- "${created_file}"')


def test_consumer_staging_does_not_exclude_changelog_fragments() -> None:
	# The exemption only helps if the untracked-files staging pass picks
	# the fragment up afterwards; assert no pathspec exclusion covers it.
	text = _script_text()
	m = re.search(r"git ls-files --others --exclude-standard -z -- (.*)\| xargs -0 -r git add --", text)
	assert m, "Failed to locate the consumer untracked-files staging pass"
	assert "changelog.d" not in m.group(1)


def _run_cleanup(tmp: Path, *, source_repo: bool) -> subprocess.CompletedProcess:
	block = _cleanup_block(_script_text())
	repo = tmp / "repo"
	repo.mkdir()
	subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
	(repo / "changelog.d").mkdir()
	(repo / "changelog.d" / "3763-example-fragment.md").write_text(
		"<!-- changelog: added -->\n- Example.\n", encoding="utf-8"
	)
	(repo / "stray_artifact.txt").write_text("leftover\n", encoding="utf-8")
	(repo / "scripts").mkdir()
	(repo / "scripts" / "helper.sh").write_text("#!/bin/sh\n", encoding="utf-8")
	script = "set -euo pipefail\n" + block
	return subprocess.run(
		["bash", "-c", script],
		cwd=repo,
		capture_output=True,
		text=True,
		env={
			"PATH": "/usr/bin:/bin:/usr/local/bin",
			"IS_WORKFLOW_SOURCE_REPO": "true" if source_repo else "false",
			"HOME": str(tmp),
		},
	)


def test_consumer_cleanup_preserves_fragment_and_removes_strays() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp = Path(tmpdir)
		proc = _run_cleanup(tmp, source_repo=False)
		assert proc.returncode == 0, (proc.stdout, proc.stderr)
		repo = tmp / "repo"
		assert (repo / "changelog.d" / "3763-example-fragment.md").exists(), proc.stdout
		assert not (repo / "stray_artifact.txt").exists(), proc.stdout
		assert (repo / "scripts" / "helper.sh").exists(), proc.stdout
		assert "Preserving editor-created changelog fragment: changelog.d/3763-example-fragment.md" in proc.stdout
		assert "- stray_artifact.txt" in proc.stdout


def test_consumer_cleanup_still_removes_non_fragment_files_in_changelog_dir() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp = Path(tmpdir)
		repo = tmp / "repo"
		repo.mkdir()
		subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
		(repo / "changelog.d").mkdir()
		(repo / "changelog.d" / "notes.txt").write_text("not a fragment\n", encoding="utf-8")
		block = _cleanup_block(_script_text())
		proc = subprocess.run(
			["bash", "-c", "set -euo pipefail\n" + block],
			cwd=repo,
			capture_output=True,
			text=True,
			env={
				"PATH": "/usr/bin:/bin:/usr/local/bin",
				"IS_WORKFLOW_SOURCE_REPO": "false",
				"HOME": str(tmp),
			},
		)
		assert proc.returncode == 0, (proc.stdout, proc.stderr)
		assert not (repo / "changelog.d" / "notes.txt").exists(), proc.stdout


def test_source_repo_cleanup_still_preserves_everything() -> None:
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp = Path(tmpdir)
		proc = _run_cleanup(tmp, source_repo=True)
		assert proc.returncode == 0, (proc.stdout, proc.stderr)
		repo = tmp / "repo"
		assert (repo / "changelog.d" / "3763-example-fragment.md").exists()
		assert (repo / "stray_artifact.txt").exists()
		assert "Preserving newly created files in workflow source repo:" in proc.stdout
