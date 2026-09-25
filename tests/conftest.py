"""Session-wide pytest fixtures for the coding-workflows test suite.

Repo-pinning git environment isolation
--------------------------------------
The implement / review_autofix / validate workflows export ``GIT_DIR`` and
``GIT_WORK_TREE`` into ``$GITHUB_ENV`` ("Activate workspace shell context")
so every later step shares the source checkout's object store and a detached
per-issue work tree. Anything launched from those steps inherits the pair,
including the codex editor and every ``pytest`` it runs while validating its
own change.

Git resolves its repository from those variables *before* it looks at
``cwd``, so a test that builds a scratch repository under ``tmp_path`` with
``git init`` / ``git add -A`` / ``git commit`` and only sets ``cwd`` is
silently rebound to the real checkout: ``git init`` re-initialises the real
repository, ``git add -A`` stages the entire live work tree (including the
support scripts the staging step installed over the branch's tracked
files), and ``git commit`` lands on the real branch with the test's
throwaway identity.

That is exactly how commit ``37c72a5`` ("base", author
``test <test@example.invalid>``) reached PR #4093 during implement run for
issue #4092: ``tests/test_assemble_changelog.py`` copies ``os.environ`` for
its git helper, the copy carried the workflow's ``GIT_DIR`` /
``GIT_WORK_TREE``, and the resulting commit reverted 1,654 lines across 32
files, including eight runtime helpers (``scripts/codex_helpers.sh`` lost
``model_provider_broker_start``), so every later review round died with
``model_provider_broker_start: command not found`` (review run
34982425230).

Several tests already strip these variables one by one. This fixture makes
the guarantee suite-wide: the repo-pinning variables are removed from
``os.environ`` for the whole session, so every git subprocess a test spawns
resolves its repository from ``cwd`` (or from variables the test sets
itself). Tests that deliberately exercise ``GIT_DIR`` handling keep working
because they set the variables inside the test body.

Staged-support ledger isolation
-------------------------------
The same implement workflow exports the self-repo staged-support ledger
paths into ``$GITHUB_ENV`` ("Stage workflow support files"):
``STAGED_SUPPORT_LEDGER``, ``STAGED_SUPPORT_BASE_DIR``,
``STAGED_SUPPORT_EDITOR_HEAD_LEDGER`` and ``IMPLEMENT_STAGED_SUPPORT_RUN_DIR``.
``scripts/implement_staged_support_workspace.sh`` and
``scripts/implement_commit_changes.sh`` read ``STAGED_SUPPORT_EDITOR_HEAD_LEDGER``
from the environment before falling back to a path next to the ledger, so a
test that runs either helper against a scratch runtime directory but builds its
environment from ``os.environ`` appends its fixture paths to the *workflow's*
editor-head ledger instead of its own.

That is how implement runs 35614385686, 35628923735, 35642366131 and
35656715219 (issues #4227 / #4242, project #4139) failed: the editor ran
``tests/test_implement_post_codex_recovery.py`` to validate its change, the
round-trip test appended ``scripts/helper.sh`` to
``/tmp/codex-implement-<run>/staged_support_editor_head.txt``, and the
workflow's post-editor ``reinstall`` then died with
``IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=scripts/helper.sh`` after the
editor had already produced a complete change set. The stall poller retried
and re-issued the same failing run four times. The variables are stripped
here for the same reason as the git ones: a helper under test must only see
the runtime paths the test itself sets.
"""

from __future__ import annotations

import os

import pytest

# Variables that re-point git at a repository other than the one implied by
# the process working directory. Mirrors the per-test scrub lists already in
# use across tests/ (see test_pr_merge_status_guard.py,
# test_orchestrate_poll_process.py, test_detect_editor_changes_lost.py).
REPO_PINNING_GIT_ENV_VARS = (
	"GIT_DIR",
	"GIT_WORK_TREE",
	"GIT_INDEX_FILE",
	"GIT_COMMON_DIR",
	"GIT_OBJECT_DIRECTORY",
	"GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

# Self-repo staged-support runtime paths that implement.yml exports into
# $GITHUB_ENV. The workspace and commit helpers prefer these over their
# ledger-relative defaults, so an inherited value points a test's helper run
# at the live workflow's ledger (see the module docstring).
STAGED_SUPPORT_RUNTIME_ENV_VARS = (
	"STAGED_SUPPORT_LEDGER",
	"STAGED_SUPPORT_BASE_DIR",
	"STAGED_SUPPORT_EDITOR_HEAD_LEDGER",
	"IMPLEMENT_STAGED_SUPPORT_RUN_DIR",
)

# Every workflow-runtime variable the session fixture strips.
WORKFLOW_RUNTIME_ENV_VARS = REPO_PINNING_GIT_ENV_VARS + STAGED_SUPPORT_RUNTIME_ENV_VARS


@pytest.fixture(autouse=True, scope="session")
def _isolate_repo_pinning_git_environment():
	"""Strip workflow-runtime variables for the whole test session.

	Covers the repo-pinning git variables and the staged-support ledger
	paths. Runs once before the first test and restores the original values
	after the last one, so a caller that launched pytest with the variables
	set (the unattended workflows) gets its environment back unchanged.
	"""
	saved_values = {}
	for variable_name in WORKFLOW_RUNTIME_ENV_VARS:
		if variable_name in os.environ:
			saved_values[variable_name] = os.environ.pop(variable_name)
	try:
		yield
	finally:
		for variable_name, variable_value in saved_values.items():
			os.environ[variable_name] = variable_value
