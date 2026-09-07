#!/usr/bin/env python3
"""Consumer-repo root-artifact cleanup must never delete a tracked file.

Three scripts remove workflow-generated root files (agents.md,
unattended_system_instructions.md, ai_pipeline.md,
probably_unnecessary_but_read_if_stuck.md) from a consumer checkout before
committing: scripts/review_conflict_resolve.sh, scripts/review_rb_judge.sh
and scripts/orchestrate_poll_process.sh. The staged workflow copies live out
of tree (SUPPORT_ROOT_DIR), so a root file that HEAD tracks is the
consumer's own content — CLAUDE.md §22.C / §24.F require a repo-owned
agents.md carrying DigitalOcean / Cloudflare resource IDs.

An unconditional `rm -f agents.md` deleted binance-blessings' tracked
agents.md inside the [ai-merge-resolve] commit b974f8b on PR #255 (both
merge parents still had it) and spawned judge fix-up issue #268 plus three
autofix rounds that kept restoring the file. These tests pin the guard.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARDED_SCRIPTS = (
	REPO_ROOT / "scripts" / "review_conflict_resolve.sh",
	REPO_ROOT / "scripts" / "review_rb_judge.sh",
	REPO_ROOT / "scripts" / "orchestrate_poll_process.sh",
)
ROOT_ARTIFACTS = (
	"unattended_system_instructions.md",
	"ai_pipeline.md",
	"agents.md",
	"probably_unnecessary_but_read_if_stuck.md",
)
BARE_RM = "rm -f unattended_system_instructions.md ai_pipeline.md agents.md probably_unnecessary_but_read_if_stuck.md"
LOOP_RE = re.compile(
	r"^(?P<indent>[ \t]*)for _root_artifact in [^\n]*\n(?:.*\n)*?(?P=indent)done\n(?P=indent)unset _root_artifact\n",
	re.MULTILINE,
)


def _extract_guard_loop(script: Path) -> str:
	src = script.read_text(encoding="utf-8")
	match = LOOP_RE.search(src)
	assert match is not None, f"{script.name}: root-artifact guard loop not found"
	return match.group(0)


@pytest.mark.parametrize("script", GUARDED_SCRIPTS, ids=lambda p: p.name)
def test_bare_root_artifact_rm_is_gone(script: Path) -> None:
	src = script.read_text(encoding="utf-8")
	assert BARE_RM not in src, (
		f"{script.name} still removes root files unconditionally; a tracked "
		"consumer agents.md would be deleted (binance-blessings PR #255)."
	)
	loop = _extract_guard_loop(script)
	assert "git ls-files --error-unmatch" in loop
	for name in ROOT_ARTIFACTS:
		assert name in loop, f"{script.name}: {name} missing from the guarded loop"


def _git(cwd: Path, *args: str) -> str:
	return subprocess.run(
		["git", *args], cwd=cwd, check=True, capture_output=True, text=True
	).stdout


@pytest.mark.parametrize("script", GUARDED_SCRIPTS, ids=lambda p: p.name)
def test_guard_loop_keeps_tracked_and_removes_untracked(script: Path, tmp_path: Path) -> None:
	"""Run the extracted loop in a scratch repo: tracked agents.md survives,
	an untracked ai_pipeline.md artifact is removed."""
	repo = tmp_path / "consumer"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "config", "user.email", "t@example.com")
	_git(repo, "config", "user.name", "t")
	(repo / "agents.md").write_text("# consumer agents\n| app | 123 |\n", encoding="utf-8")
	_git(repo, "add", "agents.md")
	_git(repo, "commit", "-q", "-m", "track agents.md")
	(repo / "ai_pipeline.md").write_text("staged artifact\n", encoding="utf-8")
	(repo / "unattended_system_instructions.md").write_text("artifact\n", encoding="utf-8")

	loop = _extract_guard_loop(script)
	env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
	result = subprocess.run(
		["bash", "-euo", "pipefail", "-c", loop],
		cwd=repo, env=env, capture_output=True, text=True,
	)
	assert result.returncode == 0, result.stderr
	assert (repo / "agents.md").exists(), "tracked agents.md was deleted"
	assert (repo / "agents.md").read_text(encoding="utf-8").startswith("# consumer agents")
	assert not (repo / "ai_pipeline.md").exists(), "untracked artifact survived"
	assert not (repo / "unattended_system_instructions.md").exists()
	assert "ROOT_ARTIFACT_CLEANUP_KEPT_TRACKED path=agents.md" in result.stdout
	assert _git(repo, "status", "--porcelain").strip() == "", "guard dirtied the tracked tree"
