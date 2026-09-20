"""Regression test: workflow git variables must not reach the test suite.

Background
----------
The implement workflow exports ``GIT_DIR`` / ``GIT_WORK_TREE`` into
``$GITHUB_ENV``. When the codex editor ran ``pytest`` during the implement
run for issue #4092, ``tests/test_assemble_changelog.py`` inherited the pair,
its scratch-repo ``git init`` / ``git add -A`` / ``git commit -m base`` were
rebound to the real checkout, and commit ``37c72a5`` (author
``test <test@example.invalid>``) landed on ``ai/issue-4092`` with a
1,654-line revert of the branch's runtime helpers. PR #4093's review rounds
then failed with ``model_provider_broker_start: command not found``
(review run 34982425230).

``tests/conftest.py`` now strips the repo-pinning variables for the whole
session. This test pins that contract end to end: it launches a nested
pytest run of the changelog tests with ``GIT_DIR`` / ``GIT_WORK_TREE``
pointed at a sentinel repository and asserts the sentinel repository is
untouched afterwards and the nested run passed. Before the conftest fix the
nested run switched the sentinel repository's branch and two tests failed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import REPO_PINNING_GIT_ENV_VARS

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_TEST_FILE = "tests/test_assemble_changelog.py"
PROBE_TEST_SELECTION = "concurrent_prs or union_backstop"
SENTINEL_BRANCH = "sentinel"


def _clean_git_env(home: Path) -> dict[str, str]:
	env = {
		key: value
		for key, value in os.environ.items()
		if key not in REPO_PINNING_GIT_ENV_VARS
	}
	env["HOME"] = str(home)
	env["GIT_CONFIG_NOSYSTEM"] = "1"
	env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "sentinel"
	env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "sentinel@example.invalid"
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	# Keep user-installed test dependencies visible after HOME is isolated.
	env["PYTHONPATH"] = os.pathsep.join(sys.path)
	return env


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
	completed = subprocess.run(
		["git", *args],
		cwd=str(repo),
		env=env,
		capture_output=True,
		text=True,
		check=True,
	)
	return completed.stdout.strip()


def test_repo_pinning_git_env_vars_are_stripped_in_session() -> None:
	for variable_name in REPO_PINNING_GIT_ENV_VARS:
		assert variable_name not in os.environ, (
			f"{variable_name} leaked into the pytest session; tests/conftest.py must strip it"
		)


def test_nested_pytest_with_workflow_git_env_leaves_sentinel_repo_untouched(tmp_path: Path) -> None:
	home = tmp_path / "home"
	home.mkdir()
	env = _clean_git_env(home)

	sentinel_repo = tmp_path / "sentinel-repo"
	sentinel_repo.mkdir()
	_git(sentinel_repo, env, "init", "-q", "-b", SENTINEL_BRANCH)
	(sentinel_repo / "README.md").write_text("sentinel\n", encoding="utf-8")
	_git(sentinel_repo, env, "add", "-A")
	_git(sentinel_repo, env, "commit", "-q", "-m", "sentinel")
	head_before = _git(sentinel_repo, env, "rev-parse", "HEAD")

	# The workflow shape: the object store lives in one place, the work tree
	# in another, and both are pinned through the environment.
	detached_work_tree = tmp_path / "work-tree"
	detached_work_tree.mkdir()
	nested_env = dict(env)
	nested_env["GIT_DIR"] = str(sentinel_repo / ".git")
	nested_env["GIT_WORK_TREE"] = str(detached_work_tree)

	completed = subprocess.run(
		[
			sys.executable,
			"-m",
			"pytest",
			"-q",
			"-p",
			"no:cacheprovider",
			PROBE_TEST_FILE,
			"-k",
			PROBE_TEST_SELECTION,
		],
		cwd=str(REPO_ROOT),
		env=nested_env,
		capture_output=True,
		text=True,
		check=False,
		timeout=300,
	)
	assert completed.returncode == 0, (
		"nested pytest run failed under GIT_DIR/GIT_WORK_TREE\n"
		f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
	)

	assert _git(sentinel_repo, env, "rev-parse", "HEAD") == head_before
	assert _git(sentinel_repo, env, "symbolic-ref", "--short", "HEAD") == SENTINEL_BRANCH
	assert _git(sentinel_repo, env, "rev-list", "--count", "HEAD") == "1"
	assert _git(sentinel_repo, env, "branch", "--list", "--format=%(refname:short)") == SENTINEL_BRANCH
