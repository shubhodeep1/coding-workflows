#!/usr/bin/env python3
"""Regression tests for git-environment isolation in ai_memory_lib.

The implement / review_autofix / validate workflows export GIT_DIR and
GIT_WORK_TREE so later workflow steps share the main checkout's object
store and a per-issue work tree.  Those variables leaked into the memory
branch git subprocesses, which clone an isolated repo addressed purely
via ``cwd``.  ``git clone`` then aborted with
"fatal: working tree '<workspace>' already exists." (exit 128), turning
the "Claim /approved command" step into a hard failure (e.g. issues
#3187 / #3189).

These tests pin that the memory git helpers strip the repo-pinning git
variables so each subprocess resolves its repository from ``cwd``.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "ai_memory_lib.py"

if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("ai_memory_lib", MODULE_PATH)
assert spec is not None and spec.loader is not None
ai_memory_lib = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ai_memory_lib
spec.loader.exec_module(ai_memory_lib)


def _git(cwd: Path, *args: str) -> None:
	subprocess.run(
		["git", *args],
		cwd=str(cwd),
		check=True,
		capture_output=True,
		text=True,
	)


def _init_source_repo(root: Path) -> Path:
	"""Create a tiny git repo with a single committed file."""
	repo = root / "source"
	repo.mkdir(parents=True, exist_ok=True)
	_git(repo, "init", "--quiet", "--initial-branch=main")
	# Keep the sandbox hermetic on machines with global gpg/ssh signing.
	_git(repo, "config", "commit.gpgsign", "false")
	_git(repo, "config", "tag.gpgsign", "false")
	_git(repo, "config", "user.email", "codex@users.noreply.github.com")
	_git(repo, "config", "user.name", "codex-bot")
	(repo / "marker.txt").write_text("hello\n", encoding="utf-8")
	_git(repo, "add", "marker.txt")
	_git(repo, "commit", "--quiet", "-m", "seed")
	return repo


@contextmanager
def _leaked_git_env(git_dir: Path, work_tree: Path) -> Iterator[None]:
	"""Emulate the workflow exporting GIT_DIR / GIT_WORK_TREE."""
	overrides = {
		"GIT_DIR": str(git_dir),
		"GIT_WORK_TREE": str(work_tree),
	}
	saved = {name: os.environ.get(name) for name in overrides}
	os.environ.update(overrides)
	try:
		yield
	finally:
		for name, value in saved.items():
			if value is None:
				os.environ.pop(name, None)
			else:
				os.environ[name] = value


def test_git_subprocess_env_strips_repo_pinning_vars() -> None:
	saved = {
		name: os.environ.get(name)
		for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_NAMESPACE", "PATH")
	}
	try:
		os.environ["GIT_DIR"] = "/tmp/leaked/.git"
		os.environ["GIT_WORK_TREE"] = "/tmp/leaked-worktree"
		os.environ["GIT_NAMESPACE"] = "leaked"
		env = ai_memory_lib._git_subprocess_env()
		assert "GIT_DIR" not in env
		assert "GIT_WORK_TREE" not in env
		assert "GIT_NAMESPACE" not in env
		# Unrelated variables must survive so auth / PATH still work.
		assert env.get("PATH") == os.environ.get("PATH")
	finally:
		for name, value in saved.items():
			if value is None:
				os.environ.pop(name, None)
			else:
				os.environ[name] = value


def test_clone_for_memory_branch_ignores_leaked_git_dir_and_work_tree() -> None:
	with tempfile.TemporaryDirectory(prefix="memory-git-env-") as td:
		root = Path(td)
		source = _init_source_repo(root)
		# An *existing* work-tree dir is what made `git clone` abort with
		# "working tree '...' already exists." before the fix.
		leaked_work_tree = root / "existing-work-tree"
		leaked_work_tree.mkdir()

		with _leaked_git_env(source / ".git", leaked_work_tree):
			clone_dir = ai_memory_lib._clone_for_memory_branch(source, "ai-memory")

		try:
			# The clone must have succeeded and carried the source content.
			assert (Path(clone_dir) / "marker.txt").read_text(encoding="utf-8") == "hello\n"
			head_branch = subprocess.run(
				["git", "rev-parse", "--abbrev-ref", "HEAD"],
				cwd=clone_dir,
				check=True,
				capture_output=True,
				text=True,
				env=ai_memory_lib._git_subprocess_env(),
			).stdout.strip()
			assert head_branch == "ai-memory"
		finally:
			subprocess.run(["rm", "-rf", str(clone_dir)], check=False)
