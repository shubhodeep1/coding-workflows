<!-- changelog: fixed -->
- **Implementation commits on an orchestrator integration branch no longer revert the branch's own copies of the runtime helpers.** In this repository the implement workflow installs `main`'s support scripts over the checkout before the editor runs, and the commit step now puts every staging-only modification back before it commits.

The "Stage workflow support files" step of `.github/workflows/implement.yml` installs `SCRIPT_REF`'s copies of `scripts/*.sh`, `scripts/*.py`, `prompts/*` and `ai-memory/schemas/*` into the checkout so the job runs the freshest tooling. Consumer repos exclude those files at commit time, but in `shubhodeep1/coding-workflows` they are tracked, and when the issue targeted `orchestrator/project-3965` the implementation commit carried `main`'s versions and silently dropped the branch's security-pass fixes from PRs #4029, #4052, #4057 and #4071. PR #4079 lost 1,117 lines across eight helpers that way and its own review editor then failed with `model_provider_broker_start: command not found` on every hourly retry; PR #4071 had spent eight autofix rounds restoring the same files. The staging step now records every tracked file it overwrote, and `scripts/implement_commit_changes.sh` restores untouched copies to `HEAD`, re-bases editor edits onto the branch's version with a 3-way merge, and fails the commit closed when that merge conflicts.

| The numbers that matter | Value |
| --- | --- |
| Helpers reverted in PR #4079 | 8 files, 1,117 deletions |
| Failed review-autofix rounds on PR #4079 before this fix | 10 (hourly, run 34669207742 and earlier) |
| Autofix rounds PR #4071 needed to restore the same files | 8 |
| Ledger location | `${RUNTIME_DIR}/staged_support_overwrites.txt` |

What this means for operators: an implementation PR against an integration branch now contains only what the editor changed. Look for `IMPLEMENT_STAGED_SUPPORT_LEDGER` and `IMPLEMENT_STAGED_SUPPORT_RESTORE restored=<n> rebased=<n> conflicts=<n>` in the implement job log; a conflict fails the commit step with `IMPLEMENT_STAGED_SUPPORT_REBASE_CONFLICT path=…` and the file must be edited against the branch's own version.

### For contributors

The job still executes the `SCRIPT_REF` copies of the helpers for the rest of the run, so `main`-side fixes to the implement tooling keep applying to wedged integration branches; only the committed content changes. Consumer repositories never set `STAGED_SUPPORT_LEDGER` and are unaffected. `tests/test_implement_post_codex_recovery.py` covers the restore, re-base, conflict and ledger contracts.
