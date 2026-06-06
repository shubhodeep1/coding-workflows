#!/usr/bin/env python3
"""Regression tests for memory-helper git environment isolation.

The workspace shell context (the "Activate workspace shell context" step in the
implement / validate / review_autofix workflows) exports ``GIT_DIR`` and
``GIT_WORK_TREE`` into ``$GITHUB_ENV`` so subsequent steps' git commands operate
on the reusable workspace work tree.  The memory helper instead operates on a
dedicated ``/tmp`` clone selected via ``cwd``.  When ``GIT_WORK_TREE`` was
inherited, ``git clone`` aborted with ``fatal: working tree '<path>' already
exists`` (exit 128), failing the "Claim /approved command" step for runs
27057616161 / 27057622152.  ``_run_git`` must strip the repo-location git vars
so git resolves the repository from ``cwd``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "ai_memory_lib.py"

if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("ai_memory_lib", MODULE_PATH)
assert spec is not None and spec.loader is not None
ai_memory_lib = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ai_memory_lib
spec.loader.exec_module(ai_memory_lib)


@contextlib.contextmanager
def _patched_env(updates: dict[str, str]):
	original = {key: os.environ.get(key) for key in updates}
	try:
		os.environ.update(updates)
		yield
	finally:
		for key, value in original.items():
			if value is None:
				os.environ.pop(key, None)
			else:
				os.environ[key] = value


def _git_process_env() -> dict[str, str]:
	env = os.environ.copy()
	for var in ai_memory_lib._GIT_LOCATION_ENV_VARS:
		env.pop(var, None)
	return env


def test_git_env_strips_repo_location_vars() -> None:
	# Every repo-location variable must be removed so an inherited workspace
	# GIT_DIR/GIT_WORK_TREE cannot re-bind the subprocess to the host repo.
	updates = {var: f"/host/{var.lower()}" for var in ai_memory_lib._GIT_LOCATION_ENV_VARS}
	updates["PATH"] = os.environ.get("PATH", "/usr/bin")
	updates["GH_TOKEN"] = "sentinel-token"

	with _patched_env(updates):
		env = ai_memory_lib._git_env()

	for var in ai_memory_lib._GIT_LOCATION_ENV_VARS:
		assert var not in env, f"{var} leaked into git env"
	# Unrelated environment (credentials, PATH) must be preserved.
	assert env.get("GH_TOKEN") == "sentinel-token"
	assert "PATH" in env


def test_git_env_includes_dir_and_work_tree() -> None:
	# The two vars the workspace step actually exports must be covered.
	assert "GIT_DIR" in ai_memory_lib._GIT_LOCATION_ENV_VARS
	assert "GIT_WORK_TREE" in ai_memory_lib._GIT_LOCATION_ENV_VARS


def _git(cwd: Path, *args: str) -> None:
	subprocess.run(
		["git", "-c", "commit.gpgsign=false", "-c", "user.email=t@t", "-c", "user.name=t", *args],
		cwd=str(cwd),
		check=True,
		capture_output=True,
		env=_git_process_env(),
		text=True,
	)


def test_run_git_clone_survives_polluted_workspace_env() -> None:
	# End-to-end repro of the failing run: with GIT_DIR/GIT_WORK_TREE pointing at
	# an existing work tree, an un-sanitized `git clone` aborts with exit 128.
	# _run_git must succeed because it strips those vars.
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		src = root / "src"
		src.mkdir()
		_git(src, "init", "-q")
		(src / "a.txt").write_text("hi\n")
		_git(src, "add", "a.txt")
		_git(src, "commit", "-qm", "init")

		work_tree = root / "wt"
		work_tree.mkdir()  # existing dir is what triggers the "already exists" abort

		with _patched_env({"GIT_DIR": str(src / ".git"), "GIT_WORK_TREE": str(work_tree)}):
			dst = root / "clone"
			proc = ai_memory_lib._run_git(
				root, ["clone", "--no-tags", "--quiet", str(src), str(dst)], check=False
			)
		assert proc.returncode == 0, (
			f"clone failed under polluted env: rc={proc.returncode} stderr={proc.stderr!r}"
		)
		assert (dst / "a.txt").exists()


def test_run_git_check_true_preserves_real_clone_error_under_polluted_env() -> None:
	# The production callers use check=True.  Even with polluted workspace
	# GIT_DIR/GIT_WORK_TREE, the sanitized subprocess must surface the clone's
	# real failure instead of the inherited "working tree already exists" error.
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		src = root / "src"
		src.mkdir()
		_git(src, "init", "-q")
		(src / "a.txt").write_text("hi\n")
		_git(src, "add", "a.txt")
		_git(src, "commit", "-qm", "init")

		work_tree = root / "wt"
		work_tree.mkdir()
		dst = root / "clone"
		dst.mkdir()
		(dst / "junk.txt").write_text("junk\n")

		with _patched_env({"GIT_DIR": str(src / ".git"), "GIT_WORK_TREE": str(work_tree)}):
			try:
				ai_memory_lib._run_git(root, ["clone", "--no-tags", "--quiet", str(src), str(dst)])
			except ai_memory_lib.MemoryGitError as exc:
				message = str(exc)
			else:
				raise AssertionError("expected MemoryGitError from non-empty destination clone")

		stderr = message.partition("stderr:\n")[2]
		assert str(dst) in stderr
		assert str(work_tree) not in stderr


def main() -> int:
	passed = 0
	failed = 0
	for name, func in sorted(globals().items()):
		if not name.startswith("test_") or not callable(func):
			continue
		try:
			func()
			print(f"  PASS  {name}", flush=True)
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}", flush=True)
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total", flush=True)
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
