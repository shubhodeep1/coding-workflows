#!/usr/bin/env python3
"""Resolve the repository root from scripts and tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
MAX_PARENT_STEPS = 10


def _has_git_marker(candidate: Path) -> bool:
	git_marker = candidate / ".git"
	if git_marker.exists():
		return True

	# Some CI workspaces expose git metadata via GIT_DIR/GIT_WORK_TREE rather
	# than a .git entry inside the writable work tree copy. Relative
	# GIT_WORK_TREE values are ambiguous outside git itself, so ignore them.
	git_work_tree = os.environ.get("GIT_WORK_TREE", "").strip()
	git_dir = os.environ.get("GIT_DIR", "").strip()
	if not git_work_tree or not git_dir:
		return False

	try:
		git_work_tree_path = Path(git_work_tree)
		if not git_work_tree_path.is_absolute():
			return False
		git_work_tree_path = git_work_tree_path.resolve()
		if not git_work_tree_path.is_dir():
			return False

		git_dir_path = Path(git_dir)
		if not git_dir_path.is_absolute():
			git_dir_path = (git_work_tree_path / git_dir_path).resolve()
		else:
			git_dir_path = git_dir_path.resolve()
		if not git_dir_path.is_dir():
			return False

		return candidate.resolve() == git_work_tree_path and (git_dir_path / "HEAD").is_file()
	except OSError:
		return False


def _is_repo_root(candidate: Path) -> bool:
	return (candidate / "CLAUDE.md").is_file() and _has_git_marker(candidate)


def repo_root_from(start: Path) -> Path:
	resolved_start = start.resolve()
	current = resolved_start if resolved_start.is_dir() else resolved_start.parent

	for _depth in range(MAX_PARENT_STEPS + 1):
		if _is_repo_root(current):
			return current
		if current.parent == current:
			break
		current = current.parent

	raise RuntimeError(
		f"Could not resolve repository root from {resolved_start}: "
		f"no parent within {MAX_PARENT_STEPS} levels contains CLAUDE.md and .git markers"
	)


def repo_root() -> Path:
	return repo_root_from(SCRIPT_DIR)


def main() -> int:
	try:
		print(repo_root())
	except RuntimeError as exc:
		print(str(exc), file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
