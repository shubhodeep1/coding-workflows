#!/usr/bin/env python3
"""Focused tests for scripts/repo_root.py."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "repo_root.py"

if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("repo_root_helper", MODULE_PATH)
assert spec is not None and spec.loader is not None
repo_root_helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = repo_root_helper
spec.loader.exec_module(repo_root_helper)


@contextlib.contextmanager
def _temporary_cwd(path: Path):
	previous = Path.cwd()
	os.chdir(path)
	try:
		yield
	finally:
		os.chdir(previous)


@contextlib.contextmanager
def _temporary_env(**replacements):
	missing = object()
	originals = {name: os.environ.get(name, missing) for name in replacements}
	try:
		for name, value in replacements.items():
			if value is None:
				os.environ.pop(name, None)
			else:
				os.environ[name] = value
		yield
	finally:
		for name, value in originals.items():
			if value is missing:
				os.environ.pop(name, None)
			else:
				os.environ[name] = value


def _run_cli(script_path: Path, *, cwd: Path) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return subprocess.run(
		[sys.executable, str(script_path)],
		cwd=str(cwd),
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)


def test_repo_root_resolves_real_repo_even_when_cwd_changes() -> None:
	with tempfile.TemporaryDirectory(prefix="repo-root-cwd-") as tmpdir:
		with _temporary_cwd(Path(tmpdir)):
			assert repo_root_helper.repo_root() == REPO_ROOT


def test_repo_root_from_nested_directory_resolves_repo_root() -> None:
	assert repo_root_helper.repo_root_from(REPO_ROOT / "scripts") == REPO_ROOT


def test_repo_root_from_git_env_fallback_accepts_real_git_dir() -> None:
	with tempfile.TemporaryDirectory(prefix="repo-root-env-ok-") as tmpdir:
		tmp_path = Path(tmpdir)
		root = tmp_path / "workspace"
		nested = root / "scripts" / "nested"
		git_dir = tmp_path / "git-meta"
		nested.mkdir(parents=True, exist_ok=True)
		git_dir.mkdir(parents=True, exist_ok=True)
		(root / "CLAUDE.md").write_text("# test\n", encoding="utf-8")
		(git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

		with _temporary_env(GIT_WORK_TREE=str(root), GIT_DIR=str(git_dir)):
			assert repo_root_helper.repo_root_from(nested) == root


def test_repo_root_from_git_env_fallback_rejects_non_git_dir() -> None:
	with tempfile.TemporaryDirectory(prefix="repo-root-env-bad-") as tmpdir:
		tmp_path = Path(tmpdir)
		root = tmp_path / "workspace"
		nested = root / "scripts" / "nested"
		bad_git_dir = tmp_path / "not-git"
		nested.mkdir(parents=True, exist_ok=True)
		bad_git_dir.mkdir(parents=True, exist_ok=True)
		(root / "CLAUDE.md").write_text("# test\n", encoding="utf-8")

		with _temporary_env(GIT_WORK_TREE=str(root), GIT_DIR=str(bad_git_dir)):
			try:
				repo_root_helper.repo_root_from(nested)
			except RuntimeError:
				return

		raise AssertionError("Expected repo_root_from() to reject a non-git GIT_DIR fallback")


def test_repo_root_from_markerless_tree_raises_clear_runtime_error() -> None:
	with tempfile.TemporaryDirectory(prefix="repo-root-missing-") as tmpdir:
		start = Path(tmpdir) / "a" / "b" / "c"
		start.mkdir(parents=True, exist_ok=True)
		try:
			repo_root_helper.repo_root_from(start)
		except RuntimeError as exc:
			message = str(exc)
		else:
			raise AssertionError("Expected repo_root_from() to raise RuntimeError for a markerless tree")

		assert "Could not resolve repository root" in message
		assert "CLAUDE.md" in message
		assert ".git" in message
		assert str(start.resolve()) in message


def test_repo_root_cli_prints_resolved_root() -> None:
	result = _run_cli(MODULE_PATH, cwd=REPO_ROOT)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == str(REPO_ROOT)
	assert result.stderr == ""


def test_repo_root_cli_exits_nonzero_when_markers_are_absent() -> None:
	with tempfile.TemporaryDirectory(prefix="repo-root-cli-") as tmpdir:
		sandbox = Path(tmpdir)
		script_copy = sandbox / "scripts" / MODULE_PATH.name
		script_copy.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(MODULE_PATH, script_copy)

		result = _run_cli(script_copy, cwd=sandbox)
		assert result.returncode != 0
		assert result.stdout == ""
		assert "Could not resolve repository root" in result.stderr


def main() -> int:
	test_funcs = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
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
