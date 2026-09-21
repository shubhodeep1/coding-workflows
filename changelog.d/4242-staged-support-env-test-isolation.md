<!-- changelog: fixed -->
- **Self-repo implement runs no longer fail after a successful edit because the editor's own test run wrote into the workflow's staged-support ledger.**

Implement runs 35614385686, 35628923735, 35642366131 and 35656715219 (issues #4227 and #4242, project #4139) each produced a complete change set and then died in the post-editor `reinstall` with `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=scripts/helper.sh`. `implement.yml` exports `STAGED_SUPPORT_EDITOR_HEAD_LEDGER` into the job environment, the codex editor inherits it, and when the editor validated its change with `pytest tests/test_implement_post_codex_recovery.py` the staged-support round-trip test ran `scripts/implement_staged_support_workspace.sh restore` with an environment copied from `os.environ`. The helper prefers that inherited variable over its ledger-relative default, so the test appended its fixture path `scripts/helper.sh` to the live run's editor-head ledger, failed its own ledger assertion, and left an entry the workflow could not reinstall. `tests/conftest.py` now strips the four staged-support runtime variables for the whole pytest session, the same way it already strips `GIT_DIR` / `GIT_WORK_TREE`, and `tests/test_pytest_git_env_isolation.py` pins it with a nested pytest run against a sentinel ledger.

| The numbers that matter | Value |
| --- | --- |
| Failed implement runs on the same defect | 4 (35614385686, 35628923735, 35642366131, 35656715219) |
| Variables stripped per session | `STAGED_SUPPORT_LEDGER`, `STAGED_SUPPORT_BASE_DIR`, `STAGED_SUPPORT_EDITOR_HEAD_LEDGER`, `IMPLEMENT_STAGED_SUPPORT_RUN_DIR` |
| Stall recovery cost before the re-issue | 2 retries, 1 stall-judge run, 127 minutes in `ai:implementing` |

What this means for operators: a self-repo implement run whose editor runs the staged-support tests completes instead of failing after the edit, so the stall poller stops retrying and re-issuing the same deterministic failure. The fix lives in the branch's `tests/conftest.py`, so an in-flight integration branch picks it up on its next `chore: sync main` merge.

### For contributors

Tests that exercise `scripts/implement_staged_support_workspace.sh` or `scripts/implement_commit_changes.sh` may still set the ledger variables explicitly; the session fixture only removes values inherited from the launching workflow. `WORKFLOW_RUNTIME_ENV_VARS` in `tests/conftest.py` is the combined list.
