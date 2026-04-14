# Orchestrator Integration Branch Rebuild Runbook

Use this runbook when the poller reports recurring integration-branch sync conflicts and the integration branch is intentionally treated as superseded or needs a clean rebuild.

## When to use this

- Tracking issue comments repeatedly show `Integration sync conflict`.
- Tracking issue state has `sync.status = superseded-by-main` and you want to resume integration-branch based delivery.
- Final integration PR is stuck in `merge_conflict` due to long-lived divergence.

## Preconditions

- `<default_branch>` contains the desired project output (or the commits you want to preserve are known).
- You have permission to push/delete `orchestrator/project-<tracking_issue>`.
- No critical in-flight PR depends on the old integration branch tip.

## Rebuild steps

1. Capture the current branch tip for rollback:

```bash
git fetch origin
git branch backup/orchestrator-project-<tracking_issue> origin/orchestrator/project-<tracking_issue>
```

2. Recreate integration branch from default branch:

```bash
git checkout <default_branch>
git pull --ff-only origin <default_branch>
git push origin --delete orchestrator/project-<tracking_issue>
git checkout -b orchestrator/project-<tracking_issue>
git push -u origin orchestrator/project-<tracking_issue>
```

3. Trigger the poller (`ai-orchestrate-poll`) or wait for next cycle.

4. Confirm tracking state updates:
- `sync.status` returns to `active` or stops emitting conflict warnings.
- `final_merge_status` no longer remains `conflict` if final PR is recreated cleanly.

## If rebuild was premature

1. Restore old tip:

```bash
git push -f origin backup/orchestrator-project-<tracking_issue>:orchestrator/project-<tracking_issue>
```

2. Add a tracking issue comment documenting why restore was required.

## Operational notes

- Poller conflict warnings are deduplicated by conflict-path fingerprint; changed conflict sets emit a new warning.
- `superseded-by-main` is a durable skip state. To force sync attempts again, update state only through approved operational procedures (or by rebuilding the branch as above).
