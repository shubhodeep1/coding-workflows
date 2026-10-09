<!-- changelog: changed -->
- **A merge conflict on the merged-PR guard hook no longer always needs a manual merge.** When `.claude/hooks/pr_merge_status_guard.py` conflicts but each side's copy matches that side's `workflow-templates/` twin, and the twin merged cleanly, review preparation now stages the twin's merged content for the hook before the conflict resolver starts. The hook still never enters the model sandbox.

`scripts/review_conflict_prepare.sh` compares Git object IDs and index modes, so the result is byte- and mode-identical to the template, as the live/template parity test requires. A hook-only conflict is committed as the usual `[ai-merge-resolve]` merge and re-reviewed; on `orchestrator/project-*` branches the commit waits for the integration fingerprint check. Any other shape (template missing or conflicted, a side whose live copy differs from its template, a deletion, a symlink) is left alone and still fails with `conflict_resolver_sandbox_path_host_only`, as PR #6555's conflict did.

| The numbers that matter | Value |
| --- | --- |
| Refusal reasons logged | 9 (`Guard-hook template merge: skipped reason=...`) |
| New kill switch | `CONFLICT_GUARD_HOOK_TEMPLATE_MERGE_ENABLED`, default `true` |

What this means for operators: nothing to configure. The kill switch is defaulted inside the script and is not forwarded from a repository variable, because `review_autofix.yml` is close to the workflow file size guard.

### For contributors

`tests/test_conflict_guard_hook_template_merge.py` runs the live shell regions against real conflicted merges: a hook-only conflict is committed, a mixed conflict leaves only the other file for the resolver, an integration-sync conflict commits after the fingerprint check, and seven refusal cases leave the hook unmerged. CI and the release gate now also run `tests/test_review_conflict_resolve_sandbox_only.py`, which no workflow ran before.
