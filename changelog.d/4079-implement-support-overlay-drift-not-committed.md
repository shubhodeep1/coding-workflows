<!-- changelog: fixed -->
- **AI implementation PRs cut from an integration branch no longer revert the branch's own helper scripts to their `main` versions.** The implement phase now leaves workflow-support overlay drift out of the commit on `shubhodeep1/coding-workflows`.

The implement workflow installs the `main` copies of the runtime helper scripts (`scripts/codex_helpers.sh`, `scripts/gh_helpers.sh`, `scripts/memory_helpers.sh`, `scripts/targeted_file_context.py` and the rest of the fetched set) over the checked-out tree before Codex runs. On this repository those files are tracked, so when the target branch is an `orchestrator/project-*` integration branch that has diverged from `main` in any of them, the overlay made them look modified and the commit step staged that drift as part of the AI implementation. PR #4079 (issue #4075, base `orchestrator/project-3965`) reverted eight helper scripts this way, dropping the `model_provider_broker_*` functions from `scripts/codex_helpers.sh`; every subsequent AI review run on the PR then failed with `model_provider_broker_start: command not found` from both the consolidator and the editor. `.github/workflows/implement.yml` now records the blob of every tracked path that is already dirty before the first Codex attempt, and `scripts/implement_commit_changes.sh` plus the preflight guard exclude any of those paths whose content Codex never changed. Files Codex did edit are still committed, with a warning that the edit sits on the overlay copy.

| The numbers that matter | Value |
| --- | --- |
| Helper scripts reverted by PR #4079 | 8 (`-1117` lines, `main`-identical content) |
| Failed AI review runs on PR #4079 before the fix | 11 (16:33Z 11 Sep to 03:54Z 12 Sep) |
| Snapshot file | `RUNTIME_DIR/pre_codex_dirty_tracked.tsv` (`PRE_CODEX_DIRTY_TRACKED_FILE`) |
| Excluded-path manifest for the push step | `RUNTIME_DIR/support_overlay_excluded.tsv` |
| Regression test | `tests/test_implement_support_overlay_drift.py` |

What this means for operators: an `ai/issue-*` PR against an integration branch now contains only what Codex wrote, so its AI review runs execute the branch's own helper scripts and the `AI review/autofix produced no output — will retry` loop caused by a self-inflicted `command not found` no longer starts. Consumer repositories are unaffected, because their fetched helpers were already kept out of commits by the generated `scripts/.gitignore`.

### For contributors

The excluded overlay copies stay in the worktree because later steps of the same job source the `main` helper versions from them. The "Push branch" step parks exactly those paths with `git restore --source=HEAD` so its non-fast-forward `git rebase` fallback still starts on a clean tree, and re-materialises them from blobs the commit step stored with `git hash-object -w`; both of the step's `EXIT` traps chain that restore. The no-op handler's `remaining_changes` report also drops the excluded paths so overlay drift is never described as a stripped Codex edit. PR #4079 itself still carries the reverted files and needs its eight helper scripts restored from `orchestrator/project-3965` before its review can pass.
