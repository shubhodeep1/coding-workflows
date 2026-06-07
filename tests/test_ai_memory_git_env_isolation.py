#!/usr/bin/env python3
"""Regression tests for git-environment isolation in ai_memory_lib.

The implement / review_autofix / validate workflows export GIT_DIR and
GIT_WORK_TREE so later workflow steps share the main checkout's object
store and a per-issue work tree. Those variables leaked into memory-helper
git subprocesses, which operate on an isolated repo addressed purely via
``cwd``. ``git clone`` then aborted with
"fatal: working tree '<workspace>' already exists." (exit 128), and other
git commands could be rebound to the host repo.

These tests pin that the memory git helpers strip the repo-pinning git
variables so each subprocess resolves its repository from ``cwd``.
"""

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
	return ai_memory_lib._git_subprocess_env()


def _git(cwd: Path, *args: str) -> None:
	subprocess.run(
		["git", *args],
		cwd=str(cwd),
		check=True,
		capture_output=True,
		text=True,
		env=_git_process_env(),
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


@contextlib.contextmanager
def _leaked_git_env(git_dir: Path, work_tree: Path):
	"""Emulate the workflow exporting GIT_DIR / GIT_WORK_TREE."""
	with _patched_env({
		"GIT_DIR": str(git_dir),
		"GIT_WORK_TREE": str(work_tree),
	}):
		yield


def test_git_subprocess_env_strips_repo_pinning_vars() -> None:
	updates = {var: f"/host/{var.lower()}" for var in ai_memory_lib._GIT_LOCATION_ENV_VARS}
	updates["PATH"] = os.environ.get("PATH", "/usr/bin")
	updates["GH_TOKEN"] = "sentinel-token"

	with _patched_env(updates):
		env = ai_memory_lib._git_subprocess_env()

	for var in ai_memory_lib._GIT_LOCATION_ENV_VARS:
		assert var not in env, f"{var} leaked into git env"
	# Unrelated environment must be preserved.
	assert env.get("GH_TOKEN") == "sentinel-token"
	assert env.get("PATH") == updates["PATH"]


def test_git_subprocess_env_includes_dir_and_work_tree() -> None:
	assert "GIT_DIR" in ai_memory_lib._GIT_LOCATION_ENV_VARS
	assert "GIT_WORK_TREE" in ai_memory_lib._GIT_LOCATION_ENV_VARS


def test_run_git_clone_survives_polluted_workspace_env() -> None:
	# End-to-end repro of the failing run: with GIT_DIR/GIT_WORK_TREE pointing at
	# an existing work tree, an unsanitized `git clone` aborts with exit 128.
	# _run_git must succeed because it strips those vars.
	with tempfile.TemporaryDirectory(prefix="memory-git-env-") as td:
		root = Path(td)
		source = _init_source_repo(root)
		work_tree = root / "existing-work-tree"
		work_tree.mkdir()

		with _leaked_git_env(source / ".git", work_tree):
			dst = root / "clone"
			proc = ai_memory_lib._run_git(
				root,
				["clone", "--no-tags", "--quiet", str(source), str(dst)],
				check=False,
			)

		assert proc.returncode == 0, (
			f"clone failed under polluted env: rc={proc.returncode} stderr={proc.stderr!r}"
		)
		assert (dst / "marker.txt").read_text(encoding="utf-8") == "hello\n"


def test_run_git_check_true_preserves_real_clone_error_under_polluted_env() -> None:
	# With polluted workspace env, the sanitized subprocess must surface the
	# clone's real failure instead of the inherited work-tree collision error.
	with tempfile.TemporaryDirectory(prefix="memory-git-env-") as td:
		root = Path(td)
		source = _init_source_repo(root)
		work_tree = root / "existing-work-tree"
		work_tree.mkdir()
		dst = root / "clone"
		dst.mkdir()
		(dst / "junk.txt").write_text("junk\n", encoding="utf-8")

		with _leaked_git_env(source / ".git", work_tree):
			try:
				ai_memory_lib._run_git(root, ["clone", "--no-tags", "--quiet", str(source), str(dst)])
			except ai_memory_lib.MemoryGitError as exc:
				message = str(exc)
			else:
				raise AssertionError("expected MemoryGitError from non-empty destination clone")

		stderr = message.partition("stderr:\n")[2]
		assert str(dst) in stderr
		assert str(work_tree) not in stderr


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
			assert (Path(clone_dir) / "marker.txt").read_text(encoding="utf-8") == "hello\n"
			head_branch = subprocess.run(
				["git", "rev-parse", "--abbrev-ref", "HEAD"],
				cwd=clone_dir,
				check=True,
				capture_output=True,
				text=True,
				env=_git_process_env(),
			).stdout.strip()
			assert head_branch == "ai-memory"
		finally:
			shutil.rmtree(clone_dir, ignore_errors=True)


def test_resolve_origin_url_uses_host_repo_via_leaked_git_dir() -> None:
	# Repro of the implement-workflow "Claim /approved command" failure
	# (issues #3186 / #3188): repo_root is the per-issue work tree exported as
	# GIT_WORK_TREE, which is *not* itself a git directory; the host repo is
	# reachable only via the exported GIT_DIR. _resolve_origin_url must return
	# the host repo's configured origin, not the bare work-tree path (which is
	# not a clonable repository and makes the subsequent clone abort exit 128).
	with tempfile.TemporaryDirectory(prefix="memory-git-env-") as td:
		root = Path(td)
		source = _init_source_repo(root)
		upstream = root / "upstream.git"
		_git(root, "clone", "--bare", "--quiet", str(source), str(upstream))
		_git(source, "remote", "add", "origin", str(upstream))

		leaked_work_tree = root / "issue-work-tree"
		leaked_work_tree.mkdir()  # pure work tree — no .git inside

		with _leaked_git_env(source / ".git", leaked_work_tree):
			origin = ai_memory_lib._resolve_origin_url(leaked_work_tree)

		assert origin == str(upstream), (
			f"expected host origin {upstream}, got {origin!r}"
		)


def test_clone_for_memory_branch_resolves_origin_from_leaked_git_dir() -> None:
	# End-to-end: cloning the memory branch must succeed even when repo_root is
	# a work tree whose .git lives elsewhere (host repo via GIT_DIR). Before the
	# fix the origin fell back to the bare work-tree path and the clone failed.
	with tempfile.TemporaryDirectory(prefix="memory-git-env-") as td:
		root = Path(td)
		source = _init_source_repo(root)
		upstream = root / "upstream.git"
		_git(root, "clone", "--bare", "--quiet", str(source), str(upstream))
		_git(source, "remote", "add", "origin", str(upstream))

		leaked_work_tree = root / "issue-work-tree"
		leaked_work_tree.mkdir()

		with _leaked_git_env(source / ".git", leaked_work_tree):
			clone_dir = ai_memory_lib._clone_for_memory_branch(leaked_work_tree, "ai-memory")

		try:
			assert (Path(clone_dir) / "marker.txt").read_text(encoding="utf-8") == "hello\n"
			head_branch = subprocess.run(
				["git", "rev-parse", "--abbrev-ref", "HEAD"],
				cwd=clone_dir,
				check=True,
				capture_output=True,
				text=True,
				env=_git_process_env(),
			).stdout.strip()
			assert head_branch == "ai-memory"
		finally:
			shutil.rmtree(clone_dir, ignore_errors=True)


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
		except Exception as exc:  # noqa: BLE001
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
