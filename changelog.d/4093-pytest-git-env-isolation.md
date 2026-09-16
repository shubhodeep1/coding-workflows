<!-- changelog: fixed -->
- **The test suite no longer commits into the real repository when a workflow launches it with `GIT_DIR` / `GIT_WORK_TREE` set.** A session-wide fixture in `tests/conftest.py` strips the repo-pinning git variables before the first test runs.

The implement, review_autofix, and validate workflows export `GIT_DIR` and `GIT_WORK_TREE` into `$GITHUB_ENV`, and the codex editor inherits them when it runs `pytest` to validate its own change. Git honours those variables ahead of the working directory, so a test that builds a scratch repository under a temp dir and only sets `cwd` was rebound to the live checkout. During the implement run for issue #4092, `tests/test_assemble_changelog.py` did exactly that: its `git init` re-initialised the real repository, `git add -A` staged the whole live work tree (including the support scripts the staging step had installed over the branch's own files), and `git commit -m base` landed commit `37c72a5` on `ai/issue-4092` under the throwaway identity `test <test@example.invalid>`. That commit reverted 1,654 lines across 32 files, including eight runtime helpers; `scripts/codex_helpers.sh` lost `model_provider_broker_start`, and every review round of PR #4093 then died in the editor with `model_provider_broker_start: command not found`. The fixture removes `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY`, and `GIT_ALTERNATE_OBJECT_DIRECTORIES` for the whole session and restores them afterwards, so every git subprocess a test spawns resolves its repository from `cwd`.

| The numbers that matter | Value |
| --- | --- |
| Stray commit | `37c72a5` on `ai/issue-4092`, PR #4093 |
| Lines reverted by that commit | 1,654 across 32 files |
| Review runs lost to the missing helper | 34982425230 and the retries that followed |
| Regression test | `tests/test_pytest_git_env_isolation.py` |

What this means for operators: an implement run that lets the editor execute the repository's own tests can no longer produce a foreign-identity commit that silently reverts the branch, and the review editor that follows it keeps its broker helpers.

### For contributors

Individual tests already stripped these variables by hand (`test_pr_merge_status_guard.py`, `test_orchestrate_poll_process.py`, `test_detect_editor_changes_lost.py`, and others); those per-test scrubs stay and remain the right pattern for tests that also sanitise `BASH_ENV` or `WORKSPACE_PATH`. Tests that intentionally exercise `GIT_DIR` handling keep working because they set the variables inside the test body. The regression test launches a nested pytest run of the changelog tests with the variables pointed at a sentinel repository and asserts the sentinel's branch, head, and commit count are unchanged.
