<!-- changelog: fixed -->
- **A manifest-only merge conflict no longer fails an orchestrator project.** On `orchestrator/project-*` integration branches, `scripts/review_conflict_prepare.sh` now resolves `.ai/.workspace_source_manifest.txt` deterministically when it is the only unmerged path, instead of refusing with `unhandled reason=integration_sync`.

Tracking issue #6664 failed at final PR #6667 after `main` untracked the generated manifest (#6515): the integration sync hit a modify/delete conflict on that one file, preparation refused to touch it on an integration-sync branch, the resolver sandbox cannot see `.ai/` anyway, and the judge escalation ended the project with "Manual intervention required". The exclusion exists so that resolving the manifest cannot change the working set a following resolver run sees. When the manifest is the only conflicted path there is no resolver run, so preparation now applies the same set-merge or gitignored-deletion handling it already uses on `ai/issue-*` branches and commits the two-parent `[ai-merge-resolve]` merge. With other unmerged paths present, integration-sync branches still refuse exactly as before.

| The numbers that matter | Value |
| --- | --- |
| Conflicted paths for the new behaviour to apply | exactly 1 (`.ai/.workspace_source_manifest.txt`) |
| Project that hit it | #6664 (final PR #6667, 18:29 UTC 2026-10-07) |
| Tests | `test_manifest_only_conflict_on_integration_sync_branch_is_resolved`, updated `test_manifest_union_integration_sync_fails_before_resolver` |

What this means for operators: an orchestrator project whose only integration conflict is the generated manifest heals itself on the next sync tick; no `/judge_resume` or manual merge is needed for that case.

### For contributors

`review_conflict_prepare.sh` reclassifies the branch as `integration-sync-manifest-only` before the existing `case`, so the original `orchestrator/project-*)` arm and the set-algebra pipeline are unchanged; `tests/test_conflict_manifest_union_contract.py` drives both shapes through the live union block.
