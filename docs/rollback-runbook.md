# Rollback Runbook

## When to Rollback

- Critical errors across multiple consumer repos after a stable promotion
- Workflow failures that block the issue-to-PR pipeline
- Memory corruption or namespace collision issues

## Rollback Procedure

### 1. Identify the last known-good release

```bash
git tag --sort=-version:refname | head -10
```

### 2. Repoint stable to previous release

```bash
GOOD_TAG="v1.0.0"  # Replace with actual known-good tag
git tag -f stable "${GOOD_TAG}"
git push -f origin stable
```

### 3. Verify rollback

- Trigger a test issue on a canary repo
- Confirm full lifecycle works: clarify -> plan -> approve -> implement -> PR

### 4. Announce

- Update CHANGELOG.md with rollback note
- Notify consumer repo maintainers

## Disabling a Problematic Phase

To quickly disable a single phase without full rollback, consumer repos can comment out or remove the specific wrapper workflow file temporarily.

## Replaying a Failed Phase

If a phase failed mid-execution:

1. Check the workflow run logs for the exact failure point
2. If safe to retry, re-trigger via the appropriate issue comment command:
   - `/reclarify` — restart clarification
   - `/answer` — restart planning
   - `/approved` — restart implementation

## Memory Recovery

If memory writes partially failed:

1. Check the `ai-memory` branch for inconsistent state
2. Use `ai_memory_lib.py` functions to inspect/repair records
3. Run memory maintenance manually via `workflow_dispatch`
