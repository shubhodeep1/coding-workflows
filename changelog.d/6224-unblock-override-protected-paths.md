<!-- changelog: security -->
- **Bulk-delete overrides no longer cover workflow and automation files.** The unblock judge refuses destructive overrides for paths under `.github/`, `.claude/` or `workflow-templates/` in any repository.

Both implementation deletion guards ignore even previously issued bulk-delete overrides when the staged deletion set contains a protected automation path. Other approved deletions can still use a one-shot override, while protected deletions remain subject to the normal threshold. Scope overrides for consumer-owned workflow edits are unchanged.
