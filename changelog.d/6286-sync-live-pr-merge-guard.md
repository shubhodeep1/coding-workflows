<!-- changelog: fixed -->
- **The §21 merged-PR guard and `/audit-plans` that coding-workflows sessions run now match the copies consumers receive.** `.claude/hooks/pr_merge_status_guard.py` had fallen behind its `workflow-templates/` twin after #6133. `.claude/commands/audit-plans.md` had kept its pre-#6176 wording. Six tests failed on `main` as a result.

When it reads push refspecs, the merged-PR guard now:
- ignores redirect targets and file-descriptor digits;
- keeps the working directory after `cd dir || exit`;
- treats `GIT_DIR+=` and `GIT_WORK_TREE+=` as unknown without applying their unresolved right-hand sides;
- reads every positional as a refspec when `--repo` is given;
- checks the checked-out branch for `git push origin HEAD`;
- resolves the push source to a commit SHA before checking it.

`/audit-plans` now treats in-session `/implement-plan-claude` projects as legacy work that may still be in flight. It names `/implement-plan-claude` (Claude-engine orchestrator hand-off) and `/implement-plan-ai` (default-engine hand-off) as the follow-ups; its conflict gate, archival rules and report format are unchanged. The guard correction ships to consumer repos on their next sync.

What this means for operators: the `Merged-PR commit guard hook tests` and `/audit-plans command contract tests` steps in `tests-hooks-and-orchestrator` pass on `main` again, and the review autofix stops trying to repair this drift inside unrelated pull requests.
